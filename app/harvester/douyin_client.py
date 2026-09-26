from __future__ import annotations

import asyncio
import calendar
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from app.config import Settings

logger = logging.getLogger(__name__)

_SHARED_URL_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_SHARED_URL_TRAILING_PUNCTUATION = ".,;:!?，。；：！？、)]}）】》」』"
_COOKIE_ATTRIBUTE_NAMES = {
    "comment",
    "domain",
    "expires",
    "httponly",
    "max-age",
    "partitioned",
    "path",
    "samesite",
    "secure",
    "version",
}
_COOKIE_REQUIRED_NAMES = {"ttwid", "odin_tt"}
_COOKIE_INPUT_ERROR = (
    "Cookie 内容不完整。请在开发者工具的 Network 中打开一个 douyin.com 请求，"
    "复制 Request Headers 里的完整 cookie: 内容；"
    "不要复制 Response Headers 里的 set-cookie，也不要只复制单个 odin_tt"
)


class HarvestError(RuntimeError):
    pass


@dataclass
class CreatorHarvest:
    creator: dict[str, Any]
    videos: list[dict[str, Any]]
    complete: bool = True
    warning: str = ""


def _cookie_dict(raw: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for part in raw.split(";"):
        if "=" not in part:
            continue
        key, value = part.strip().split("=", 1)
        if (
            key
            and not any(ch.isspace() for ch in key)
            and key.lower() not in _COOKIE_ATTRIBUTE_NAMES
        ):
            parsed[key] = value
    return parsed


def _cookie_validation_error(cookies: dict[str, str]) -> str:
    names = {key.lower() for key in cookies}
    if len(cookies) < 3 or not _COOKIE_REQUIRED_NAMES.issubset(names):
        return _COOKIE_INPUT_ERROR
    return ""


def _first_url(value: Any) -> str | None:
    if isinstance(value, str) and value.startswith("http"):
        return value
    if isinstance(value, dict):
        urls = value.get("url_list") or value.get("urls")
        if isinstance(urls, list):
            return next(
                (item for item in urls if isinstance(item, str) and item.startswith("http")), None
            )
        for key in ("url", "uri", "play_url"):
            found = _first_url(value.get(key))
            if found:
                return found
    if isinstance(value, list):
        for item in value:
            found = _first_url(item)
            if found:
                return found
    return None


class DouyinHarvester:
    """Thin adapter over a pinned, actively maintained Douyin API client."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def has_cookie(self) -> bool:
        return self.settings.cookie_path.exists() and bool(
            self.settings.cookie_path.read_text(encoding="utf-8").strip()
        )

    def save_cookie(self, raw: str) -> None:
        cookies = _cookie_dict(raw)
        error = _cookie_validation_error(cookies)
        if error:
            raise ValueError(error)
        self.settings.cookie_path.parent.mkdir(parents=True, exist_ok=True)
        normalized = "; ".join(f"{key}={value}" for key, value in cookies.items())
        self.settings.cookie_path.write_text(normalized, encoding="utf-8")
        os.chmod(self.settings.cookie_path, 0o600)

    def load_cookies(self) -> dict[str, str]:
        if not self.has_cookie():
            raise HarvestError("尚未配置抖音 Cookie；请先在系统设置中保存登录 Cookie")
        cookies = _cookie_dict(self.settings.cookie_path.read_text(encoding="utf-8"))
        error = _cookie_validation_error(cookies)
        if error:
            raise HarvestError(f"已保存的{error}")
        return cookies

    @staticmethod
    def validate_url(url: str) -> str:
        for match in _SHARED_URL_PATTERN.finditer(url.strip()):
            candidate = match.group(0).rstrip(_SHARED_URL_TRAILING_PUNCTUATION)
            parsed = urlparse(candidate)
            host = (parsed.hostname or "").lower()
            if parsed.scheme in {"http", "https"} and (
                host == "douyin.com"
                or host.endswith(".douyin.com")
                or host == "iesdouyin.com"
                or host.endswith(".iesdouyin.com")
            ):
                return candidate
        raise HarvestError("请输入抖音博主主页链接，或粘贴包含主页链接的完整分享文案")

    async def harvest(self, input_url: str, max_videos: int = 0) -> CreatorHarvest:
        try:
            from core import DouyinAPIClient, URLParser
        except ImportError as exc:  # pragma: no cover - deployment guard
            raise HarvestError("采集依赖未安装，请重新执行安装脚本") from exc

        input_url = self.validate_url(input_url)
        cookies = self.load_cookies()
        try:
            async with DouyinAPIClient(cookies) as client:
                resolved_url = input_url
                if "/user/" not in urlparse(input_url).path:
                    resolved_url = await client.resolve_short_url(input_url)
                parsed = URLParser.parse(resolved_url or "")
                if not parsed or parsed.get("type") != "user" or not parsed.get("sec_uid"):
                    raise HarvestError("该链接未解析为博主主页，请复制博主主页的分享链接")
                sec_uid = str(parsed["sec_uid"])
                profile = await client.get_user_info(sec_uid)
                if not profile:
                    raise HarvestError("无法读取博主信息；抖音 Cookie 可能已过期")
                expected_count = int(profile.get("aweme_count") or 0)

                items: list[dict[str, Any]] = []
                seen: set[str] = set()
                cursor = 0
                seen_cursors: set[int] = set()
                login_required = False
                time_windows: list[str] = []
                for _page in range(500):
                    if _page and not max_videos:
                        await asyncio.sleep(3)
                    if _page and _page % 6 == 0 and not max_videos:
                        logger.info("Douyin page batch cooldown at page=%s", _page + 1)
                        await asyncio.sleep(20)
                    page = await client.get_user_post(sec_uid, cursor, 20)
                    login_required = login_required or bool(
                        (page.get("risk_flags") or {}).get("login_tip")
                    )
                    page_items = page.get("items") or page.get("aweme_list") or []
                    page_time_windows = page.get("time_list")
                    if isinstance(page_time_windows, list):
                        time_windows = [str(value) for value in page_time_windows if value]
                    if (
                        not page_items
                        and items
                        and expected_count > len(items)
                        and not login_required
                        and not max_videos
                    ):
                        for cooldown in (20, 40, 60):
                            logger.warning(
                                "Douyin pagination returned empty at cursor=%s; retry in %ss",
                                cursor,
                                cooldown,
                            )
                            await asyncio.sleep(cooldown)
                            page = await client.get_user_post(sec_uid, cursor, 20)
                            page_items = page.get("items") or page.get("aweme_list") or []
                            if page_items:
                                # A recovered page does not mean the temporary
                                # throttle window has fully cleared. Give the
                                # next cursor a clean window instead of causing
                                # an immediate second 403.
                                await asyncio.sleep(15)
                                break
                    for item in page_items:
                        aweme_id = str(item.get("aweme_id") or "")
                        if not aweme_id or aweme_id in seen:
                            continue
                        seen.add(aweme_id)
                        items.append(item)
                        if max_videos and len(items) >= max_videos:
                            break
                    if max_videos and len(items) >= max_videos:
                        break
                    if not page.get("has_more"):
                        raw_page = page.get("raw") if isinstance(page.get("raw"), dict) else {}
                        time_list = raw_page.get("time_list")
                        logger.info(
                            "Douyin pagination ended: page=%s total=%s cursor=%s "
                            "page_items=%s time_list_count=%s whale_cut_token=%s raw_keys=%s",
                            _page + 1,
                            len(items),
                            cursor,
                            len(page_items),
                            len(time_list) if isinstance(time_list, list) else 0,
                            bool(raw_page.get("whale_cut_token")),
                            ",".join(sorted(raw_page.keys())),
                        )
                        break
                    next_cursor = int(page.get("max_cursor") or 0)
                    if next_cursor in seen_cursors or next_cursor == cursor:
                        logger.warning("Douyin pagination stalled at cursor=%s", cursor)
                        break
                    seen_cursors.add(cursor)
                    cursor = next_cursor

                if expected_count > 0 and not items:
                    raise HarvestError(
                        "抖音主页可以读取，但作品列表返回为空。"
                        "登录 Cookie 可能已过期或触发了平台风控；"
                        "请在已登录的抖音网页版重新获取完整 Cookie，保存后再重试"
                    )

                if not max_videos and expected_count > len(items) and time_windows:
                    items = await self._recover_older_months(
                        client, sec_uid, items, seen, time_windows, expected_count
                    )

                # The web API increasingly returns a valid first page but an empty
                # second page. In that case use the maintained client's browser
                # collector, which scrolls the creator page and captures post API
                # responses. A formal full run must never silently call 23/669
                # items a complete harvest.
                complete = True
                warning = ""
                if not max_videos and expected_count > len(items):
                    if login_required:
                        complete = False
                        warning = (
                            f"抖音只公开返回了前 {len(items)} 条，当前 Cookie 未被识别为登录状态；"
                            "请在已登录抖音网页版的浏览器中更新 Cookie 后重试"
                        )
                    else:
                        items = await self._recover_with_browser(
                            client, sec_uid, items, seen, expected_count
                        )
                    if len(items) < expected_count:
                        complete = False
                        warning = (
                            f"主页标称 {expected_count} 条作品，但分页后只收集到 {len(items)} 条；"
                            "已保存本次发现结果并阻止生成不完整版本，请稍后继续处理"
                        )

                creator = self._normalize_creator(profile, sec_uid, input_url)
                videos = [self._normalize_video(item, sec_uid) for item in items]
                return CreatorHarvest(
                    creator=creator, videos=videos, complete=complete, warning=warning
                )
        except HarvestError:
            raise
        except Exception as exc:
            message = str(exc)
            if "403" in message or "cookie" in message.lower() or "login" in message.lower():
                raise HarvestError("抖音拒绝了采集请求，请更新 Cookie 后重试") from exc
            raise HarvestError(f"采集失败：{message[:300]}") from exc

    async def _recover_older_months(
        self,
        client: Any,
        sec_uid: str,
        items: list[dict[str, Any]],
        seen: set[str],
        time_windows: list[str],
        expected_count: int,
    ) -> list[dict[str, Any]]:
        """Use Douyin's month locator to audit gaps in normal pagination."""
        timestamps = [int(item.get("create_time") or 0) for item in items]
        valid_timestamps = [value for value in timestamps if value]
        if not valid_timestamps:
            return items
        oldest = datetime.fromtimestamp(min(valid_timestamps), self.settings.tz)
        candidates: list[tuple[int, int]] = []
        for value in time_windows:
            normalized = value.replace("年", "·").replace("月", "").replace("-", "·")
            parts = normalized.split("·")
            if len(parts) != 2:
                continue
            try:
                year, month = int(parts[0]), int(parts[1])
            except ValueError:
                continue
            if 1 <= month <= 12:
                candidates.append((year, month))
        candidates = sorted(set(candidates), reverse=True)
        if not candidates:
            return items
        logger.info(
            "Douyin month recovery starting: oldest=%s candidates=%s missing=%s",
            oldest.strftime("%Y-%m"),
            len(candidates),
            max(0, expected_count - len(items)),
        )
        for index, (year, month) in enumerate(candidates, start=1):
            if len(items) >= expected_count:
                break
            if index > 1:
                await asyncio.sleep(4)
            if index > 1 and index % 6 == 0:
                await asyncio.sleep(20)
            month_start = datetime(year, month, 1, tzinfo=self.settings.tz)
            month_end = datetime(
                year,
                month,
                calendar.monthrange(year, month)[1],
                23,
                59,
                59,
                tzinfo=self.settings.tz,
            )
            try:
                params = await client._build_user_page_params(  # noqa: SLF001
                    sec_uid, int(month_start.timestamp() * 1000), 50
                )
                params.update(
                    {
                        "forward_end_cursor": int(month_end.timestamp() * 1000),
                        "show_live_replay_strategy": "1",
                        "need_time_list": "0",
                        "time_list_query": "1",
                        "whale_cut_token": "",
                        "cut_version": "1",
                        "publish_video_strategy_type": "2",
                        "from_user_page": "1",
                    }
                )
                raw = await client._request_json(  # noqa: SLF001
                    "/aweme/v1/web/aweme/post/", params
                )
                page = client._normalize_paged_response(  # noqa: SLF001
                    raw, item_keys=["aweme_list"]
                )
            except Exception as exc:
                logger.warning("Douyin month recovery %s-%02d failed: %s", year, month, exc)
                continue
            recovered = 0
            for item in page.get("items") or []:
                aweme_id = str(item.get("aweme_id") or "")
                if not aweme_id or aweme_id in seen:
                    continue
                seen.add(aweme_id)
                items.append(item)
                recovered += 1
            logger.info(
                "Douyin month recovery: month=%s-%02d recovered=%s total=%s",
                year,
                month,
                recovered,
                len(items),
            )
        return items

    @staticmethod
    async def _recover_with_browser(
        client: Any,
        sec_uid: str,
        items: list[dict[str, Any]],
        seen: set[str],
        expected_count: int,
    ) -> list[dict[str, Any]]:
        logger.warning(
            "API pagination incomplete, starting browser recovery: expected=%s api_items=%s",
            expected_count,
            len(items),
        )
        browser_ids = await client.collect_user_post_ids_via_browser(
            sec_uid,
            # Passing a positive expected_count makes the upstream browser helper
            # ignore its idle-stop rule and spin through every scroll even when
            # Douyin is no longer loading anything. Silo validates completeness
            # itself below, so let the browser stop after stable idle rounds.
            expected_count=0,
            headless=True,
            max_scrolls=360,
            idle_rounds=12,
            wait_timeout_seconds=900,
        )
        browser_items = client.pop_browser_post_aweme_items()
        recovered = 0
        detail_failed = 0
        for index, aweme_id in enumerate(browser_ids, start=1):
            aweme_id = str(aweme_id)
            if not aweme_id or aweme_id in seen:
                continue
            item = browser_items.get(aweme_id)
            if not item:
                if index > 1:
                    await asyncio.sleep(0.2)
                item = await client.get_video_detail(aweme_id, suppress_error=True)
            if not item:
                detail_failed += 1
                continue
            author = item.get("author") or {}
            item_sec_uid = str(author.get("sec_uid") or "")
            if item_sec_uid and item_sec_uid != sec_uid:
                continue
            seen.add(aweme_id)
            items.append(item)
            recovered += 1
        logger.warning(
            "Browser recovery complete: ids=%s metadata=%s recovered=%s detail_failed=%s total=%s",
            len(browser_ids),
            len(browser_items),
            recovered,
            detail_failed,
            len(items),
        )
        return items

    @staticmethod
    def _normalize_creator(
        profile: dict[str, Any], sec_uid: str, source_url: str
    ) -> dict[str, Any]:
        avatar = _first_url(profile.get("avatar_larger") or profile.get("avatar_medium"))
        return {
            "creator_id": sec_uid,
            "nickname": profile.get("nickname") or "未知博主",
            "source_url": source_url,
            "avatar_url": avatar,
            "signature": profile.get("signature") or "",
            "platform_video_count": int(profile.get("aweme_count") or 0),
        }

    @staticmethod
    def _normalize_video(item: dict[str, Any], creator_id: str) -> dict[str, Any]:
        stats = item.get("statistics") or {}
        video = item.get("video") or {}
        play_addr = video.get("play_addr") or video.get("play_addr_265") or {}
        return {
            "aweme_id": str(item.get("aweme_id")),
            "creator_id": creator_id,
            "title": item.get("desc") or item.get("item_title") or "无标题",
            "publish_time": int(item.get("create_time") or 0),
            "duration_ms": int(item.get("duration") or video.get("duration") or 0),
            "like_count": int(stats.get("digg_count") or 0),
            "comment_count": int(stats.get("comment_count") or 0),
            "share_count": int(stats.get("share_count") or 0),
            "video_url": _first_url(play_addr),
            "cover_url": _first_url(video.get("cover") or video.get("origin_cover")),
            "raw_metadata": item,
        }

    def cookie_header(self) -> str:
        return "; ".join(f"{key}={value}" for key, value in self.load_cookies().items())

    def diagnostic(self) -> dict[str, Any]:
        cookies = (
            _cookie_dict(self.settings.cookie_path.read_text(encoding="utf-8"))
            if self.has_cookie()
            else {}
        )
        error = _cookie_validation_error(cookies) if cookies else "尚未保存 Cookie"
        return {
            "configured": not error,
            "path": str(self.settings.cookie_path),
            "cookie_count": len(cookies),
            "error": error,
        }

    @staticmethod
    def safe_metadata(metadata: dict[str, Any]) -> str:
        return json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
