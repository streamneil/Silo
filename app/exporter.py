from __future__ import annotations

import csv
import json
import re
import shutil
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.artifact_integrity import assert_no_unresolved_inline_code_tokens
from app.config import Settings
from app.database import utc_now


def safe_name(value: str, fallback: str = "creator") -> str:
    value = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "", value).strip().strip(".")
    return value[:80] or fallback


def _transcript_payload(video: dict[str, Any]) -> dict[str, Any]:
    value = video.get("transcript_payload_json")
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        payload = json.loads(str(value))
        return payload if isinstance(payload, dict) else {}
    except (TypeError, ValueError):
        return {}


def _timestamp(value: Any) -> str:
    seconds = max(0.0, float(value or 0))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{int(hours):02d}:{int(minutes):02d}:{seconds:06.3f}"


def timestamped_transcript(video: dict[str, Any]) -> str:
    segments = _transcript_payload(video).get("segments") or []
    lines = []
    for segment in segments:
        if not isinstance(segment, dict) or not str(segment.get("text") or "").strip():
            continue
        lines.append(
            f"[{_timestamp(segment.get('start'))} → {_timestamp(segment.get('end'))}] "
            f"{str(segment['text']).strip()}"
        )
    return "\n\n".join(lines)


def readable_paragraphs(text: str, target_chars: int = 260) -> str:
    """Turn a transcript into short paragraphs without changing its wording."""
    normalized = re.sub(r"[ \t]+", " ", str(text or "")).strip()
    if not normalized:
        return "（当前作品没有可读文本）"
    units = [
        part.strip()
        for part in re.split(r"(?<=[。！？!?；;])|\n+", normalized)
        if part.strip()
    ]
    paragraphs: list[str] = []
    current = ""
    for unit in units:
        if current and len(current) + len(unit) > target_chars:
            paragraphs.append(current)
            current = unit
        else:
            current += unit
    if current:
        paragraphs.append(current)
    return "\n\n".join(paragraphs)


def readable_transcript(
    video: dict[str, Any], timezone: str, version_label: str, *, is_new: bool
) -> str:
    published = datetime.fromtimestamp(
        int(video["publish_time"]), tz=ZoneInfo(timezone)
    )
    title = str(video.get("title") or "无标题").strip()
    body = readable_paragraphs(
        str(video.get("cleaned_transcript") or video.get("raw_transcript") or "")
    )
    status = "本版本新增" if is_new else "历史收录"
    return f"""{title}
{'=' * min(60, max(12, len(title)))}

版本：{version_label}
收录状态：{status}
发布时间：{published.isoformat()}
作品 ID：{video['aweme_id']}
点赞：{video.get('like_count', 0)}
原始链接：{video.get('video_url') or '未记录'}

正文
----

{body}
"""


def golden_quote(video: dict[str, Any]) -> str:
    """Select one exact, readable sentence from a transcript without model rewriting."""
    text = str(video.get("cleaned_transcript") or video.get("raw_transcript") or "")
    candidates = [
        part.strip(" \t\r\n\"“”'‘’")
        for part in re.split(r"(?<=[。！？!?；;])|\n+", text)
        if 10 <= len(part.strip()) <= 120
    ]
    if not candidates:
        return re.sub(r"\s+", " ", text).strip()[:120] or "（未提取到可读金句）"
    signals = (
        "不是",
        "而是",
        "本质",
        "真正",
        "只有",
        "一定",
        "永远",
        "不要",
        "千万",
        "为什么",
        "你会发现",
        "意味着",
        "最大的",
    )

    def score(item: tuple[int, str]) -> tuple[float, int]:
        index, sentence = item
        signal_score = sum(18 for signal in signals if signal in sentence)
        length_score = max(0, 32 - abs(len(sentence) - 46) * 0.5)
        return signal_score + length_score - index * 0.05, -index

    return max(enumerate(candidates), key=score)[1]


def golden_quotes_text(
    creator: dict[str, Any],
    run: dict[str, Any],
    videos: list[dict[str, Any]],
    version_label: str,
    timezone: str,
) -> str:
    ordered = sorted(
        videos,
        key=lambda video: (
            video.get("first_seen_run_id") == run["run_id"],
            int(video.get("like_count") or 0),
            int(video.get("publish_time") or 0),
        ),
        reverse=True,
    )
    lines = [
        f"{creator['nickname']}金句",
        "=" * 36,
        "",
        f"版本：{version_label}",
        f"内容截止时间：{run['content_cutoff_at']}",
        f"完整逐字稿：{len(videos)} 条",
        f"本版本新增：{run.get('new_count', 0)} 条",
        "说明：每条金句均从本版本阅读稿中逐字抽取，未进行二次改写，可回到作品 ID 核验。",
        "",
    ]
    for index, video in enumerate(ordered, start=1):
        published = datetime.fromtimestamp(int(video["publish_time"]), tz=ZoneInfo(timezone))
        new_marker = "【本版本新增】" if video.get("first_seen_run_id") == run["run_id"] else ""
        lines.extend(
            [
                f"{index:03d}. {new_marker}{golden_quote(video)}",
                f"来源：{video.get('title') or '无标题'}",
                (
                    f"日期：{published:%Y-%m-%d} ｜ 作品 ID：{video['aweme_id']} "
                    f"｜ 点赞：{video.get('like_count', 0)}"
                ),
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def video_markdown(video: dict[str, Any], timezone: str) -> str:
    published = datetime.fromtimestamp(
        int(video["publish_time"]), tz=ZoneInfo(timezone)
    )
    title = video.get("title") or "无标题"
    timeline = timestamped_transcript(video)
    model = video.get("transcript_model") or "unknown"
    language = video.get("transcript_language") or "unknown"
    duration = video.get("transcript_duration_seconds")
    generated_at = video.get("transcript_generated_at") or "unknown"
    timeline_section = timeline or "当前资料没有可用时间戳（图文作品或旧版文本）。"
    return f"""---
aweme_id: {json.dumps(video["aweme_id"], ensure_ascii=False)}
title: {json.dumps(title, ensure_ascii=False)}
published_at: {published.isoformat()}
like_count: {video.get("like_count", 0)}
comment_count: {video.get("comment_count", 0)}
share_count: {video.get("share_count", 0)}
transcript_source: {video.get("transcript_source") or "unknown"}
transcript_model: {json.dumps(model, ensure_ascii=False)}
transcript_language: {json.dumps(language, ensure_ascii=False)}
duration_seconds: {json.dumps(duration, ensure_ascii=False)}
transcript_generated_at: {json.dumps(generated_at, ensure_ascii=False)}
has_timestamps: {str(bool(timeline)).lower()}
---

# {title}

## 时间轴逐字稿

{timeline_section}

## 原始转写稿

{video.get("raw_transcript") or ""}

## 规范化文本（未校对）

{video.get("cleaned_transcript") or ""}
"""


class Exporter:
    def __init__(self, settings: Settings):
        self.settings = settings

    def write_canonical_video(self, creator_id: str, video: dict[str, Any]) -> Path:
        published = datetime.fromtimestamp(int(video["publish_time"]), tz=self.settings.tz)
        directory = self.settings.corpus_dir / safe_name(creator_id) / "items"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{published:%Y-%m-%d}_{video['aweme_id']}.md"
        path.write_text(video_markdown(video, self.settings.timezone), encoding="utf-8")
        return path

    def create_snapshot(
        self,
        *,
        creator: dict[str, Any],
        run: dict[str, Any],
        videos: list[dict[str, Any]],
        report: str,
        skill: str,
        analyzed_count: int,
        version_label: str,
    ) -> dict[str, Any]:
        assert_no_unresolved_inline_code_tokens(report, "creator_profile.md")
        assert_no_unresolved_inline_code_tokens(skill, "SKILL.md")
        version_id = f"ver_{uuid.uuid4().hex[:12]}"
        creator_dir = self.settings.versions_dir / safe_name(creator["creator_id"])
        version_dir = creator_dir / f"{version_label}_{run['run_id']}"
        corpus_dir = version_dir / "corpus"
        readable_dir = version_dir / "readable_transcripts"
        corpus_dir.mkdir(parents=True, exist_ok=False)
        readable_dir.mkdir()

        completed = [v for v in videos if v.get("transcript_status") == "COMPLETED"]
        exceptions = [v for v in videos if v.get("transcript_status") != "COMPLETED"]
        readable_files: dict[str, str] = {}
        for video in completed:
            published = datetime.fromtimestamp(int(video["publish_time"]), tz=self.settings.tz)
            item_path = corpus_dir / f"{published:%Y-%m-%d}_{video['aweme_id']}.md"
            item_path.write_text(video_markdown(video, self.settings.timezone), encoding="utf-8")
            readable_filename = (
                f"{published:%Y-%m-%d}_{safe_name(video.get('title') or '无标题')}"
                f"_{video['aweme_id']}.txt"
            )
            readable_files[str(video["aweme_id"])] = readable_filename
            (readable_dir / readable_filename).write_text(
                readable_transcript(
                    video,
                    self.settings.timezone,
                    version_label,
                    is_new=video.get("first_seen_run_id") == run["run_id"],
                ),
                encoding="utf-8",
            )

        index_path = version_dir / "corpus_index.csv"
        with index_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                [
                    "视频ID",
                    "标题",
                    "发布时间",
                    "点赞",
                    "评论",
                    "分享",
                    "字幕来源",
                    "处理状态",
                    "审计语料文件",
                    "阅读版逐字稿文件",
                    "版本收录状态",
                ]
            )
            for video in videos:
                published = datetime.fromtimestamp(int(video["publish_time"]), tz=self.settings.tz)
                filename = f"{published:%Y-%m-%d}_{video['aweme_id']}.md"
                writer.writerow(
                    [
                        video["aweme_id"],
                        video.get("title"),
                        published.isoformat(),
                        video.get("like_count", 0),
                        video.get("comment_count", 0),
                        video.get("share_count", 0),
                        video.get("transcript_source"),
                        video.get("transcript_status"),
                        filename if video in completed else "",
                        (
                            f"readable_transcripts/{readable_files[str(video['aweme_id'])]}"
                            if video in completed
                            else ""
                        ),
                        (
                            "本版本新增"
                            if video.get("first_seen_run_id") == run["run_id"]
                            else "历史收录"
                        ),
                    ]
                )
        skill = self._stamp_skill(skill, version_label, run["content_cutoff_at"])
        (version_dir / "creator_profile.md").write_text(report, encoding="utf-8")
        (version_dir / "SKILL.md").write_text(skill, encoding="utf-8")
        quotes_filename = f"{safe_name(creator['nickname'])}金句_{version_label}.txt"
        (version_dir / quotes_filename).write_text(
            golden_quotes_text(
                creator,
                run,
                completed,
                version_label,
                self.settings.timezone,
            ),
            encoding="utf-8",
        )
        timestamps = [int(v["publish_time"]) for v in videos if int(v.get("publish_time") or 0) > 0]
        platform_claimed = int(creator.get("platform_video_count") or 0)
        missing_platform_items = max(0, platform_claimed - len(videos))
        manifest = {
            "creator_id": creator["creator_id"],
            "creator_name": creator["nickname"],
            "version_id": version_id,
            "version_label": version_label,
            "content_cutoff_at": run["content_cutoff_at"],
            "generated_at": utc_now(),
            "run_id": run["run_id"],
            "run_type": run["run_type"],
            "previous_run_id": run.get("previous_run_id"),
            "coverage": {
                "earliest_video_at": datetime.fromtimestamp(
                    min(timestamps), tz=self.settings.tz
                ).isoformat()
                if timestamps
                else None,
                "latest_video_at": datetime.fromtimestamp(
                    max(timestamps), tz=self.settings.tz
                ).isoformat()
                if timestamps
                else None,
                "discovered_videos": len(videos),
                "platform_claimed_videos": platform_claimed,
                "missing_platform_items": missing_platform_items,
                "coverage_claim": (
                    "PUBLICLY_DISCOVERED_CORPUS"
                    if missing_platform_items or exceptions
                    else "COMPLETE"
                ),
                "transcript_completed": len(completed),
                "exceptions": len(exceptions),
                "analyzed_transcripts": analyzed_count,
            },
            "increment": {
                "newly_discovered": run.get("new_count", 0),
                "previous_run_id": run.get("previous_run_id"),
            },
            "exceptions": [
                {
                    "aweme_id": video["aweme_id"],
                    "title": video.get("title"),
                    "status": video.get("transcript_status"),
                    "reason": video.get("transcript_error"),
                }
                for video in exceptions
            ],
            "artifacts": {
                "corpus": "corpus/",
                "readable_transcripts": "readable_transcripts/",
                "index": "corpus_index.csv",
                "report": "creator_profile.md",
                "skill": "SKILL.md",
                "quotes": quotes_filename,
                "delivery_guide": "商品交付说明.md",
            },
        }
        manifest_path = version_dir / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (version_dir / "MANIFEST.md").write_text(
            self._manifest_markdown(manifest), encoding="utf-8"
        )
        (version_dir / "商品交付说明.md").write_text(
            self._delivery_markdown(manifest), encoding="utf-8"
        )

        archive_path = version_dir.parent / f"{version_dir.name}.zip"
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in version_dir.rglob("*"):
                if path.is_file():
                    archive.write(path, path.relative_to(version_dir))
        return {
            "version_id": version_id,
            "version_label": version_label,
            "version_dir": str(version_dir),
            "manifest_path": str(manifest_path),
            "export_path": str(archive_path),
            "manifest": manifest,
            "skill_markdown": skill,
        }

    @staticmethod
    def _stamp_skill(skill: str, version_label: str, cutoff_at: str) -> str:
        if not skill.startswith("---\n"):
            return skill
        closing = skill.find("\n---", 4)
        if closing < 0:
            return skill
        frontmatter = skill[:closing]
        body = skill[closing:]
        if "\nversion:" not in frontmatter:
            frontmatter += f'\nversion: "{version_label}"'
        if "\ncorpus_cutoff:" not in frontmatter:
            frontmatter += f'\ncorpus_cutoff: "{cutoff_at}"'
        return frontmatter + body

    @staticmethod
    def _delivery_markdown(manifest: dict[str, Any]) -> str:
        coverage = manifest["coverage"]
        return f"""# {manifest["creator_name"]} 内容资料交付说明

## 本次交付

- 商品版本：`{manifest["version_label"]}`
- 内容截止时间：{manifest["content_cutoff_at"]}
- 实际收录作品：{coverage["discovered_videos"]} 条
- 完整文本：{coverage["transcript_completed"]} 条
- 风格分析覆盖：{coverage["analyzed_transcripts"]} 条
- 平台主页标称：{coverage["platform_claimed_videos"]} 条
- 当前公开作品流未返回：{coverage["missing_platform_items"]} 条
- 审计口径：`{coverage["coverage_claim"]}`

## 文件清单

- `corpus/`：每条作品一个 Markdown，含元数据、时间轴逐字稿、忠实原始稿和规范化文本。
- `readable_transcripts/`：每条作品一个独立 TXT 阅读版，去掉技术字段与时间轴并按短段落排版。
- `corpus_index.csv`：全部作品索引，可用 Excel、Numbers 等软件打开。
- `creator_profile.md`：基于本版本全部完整文本生成的分析报告。
- `SKILL.md`：带版本号与语料截止时间的可用创作 Skill。
- `{manifest["artifacts"]["quotes"]}`：逐篇从真实逐字稿抽取的金句，带版本、本次新增标记与作品 ID。
- `MANIFEST.md` / `manifest.json`：覆盖率、异常和增量记录，供买家核验。

## 交付口径

本包交付的是系统在当前登录状态下从公开作品流实际获取并整理的资料，不把平台标称但未返回的
{coverage["missing_platform_items"]} 条作品伪装成已收录。后续检测到新增作品时，会生成新的版本包，
旧版本仍可独立核验。资料包不附带原作者的版权或商业授权；发布或转售前请自行确认使用权限。
"""

    @staticmethod
    def _manifest_markdown(manifest: dict[str, Any]) -> str:
        coverage = manifest["coverage"]
        exception_lines = (
            "\n".join(
                f"- `{item['aweme_id']}` {item.get('title') or ''}："
                f"{item.get('reason') or item['status']}"
                for item in manifest["exceptions"]
            )
            or "- 无"
        )
        return f"""# {manifest["creator_name"]} 语料库版本说明

- 版本：{manifest["version_label"]}
- 内容截止时间：{manifest["content_cutoff_at"]}
- 生成时间：{manifest["generated_at"]}
- 运行类型：{manifest["run_type"]}
- 发现作品：{coverage["discovered_videos"]} 条
- 平台标称作品：{coverage["platform_claimed_videos"]} 条
- 当前公开作品流未返回：{coverage["missing_platform_items"]} 条
- 覆盖声明：{coverage["coverage_claim"]}
- 完整字幕：{coverage["transcript_completed"]} 条
- 本次新增：{manifest["increment"]["newly_discovered"]} 条
- 异常：{coverage["exceptions"]} 条
- 风格分析覆盖：{coverage["analyzed_transcripts"]} 条完整逐字稿

## 覆盖范围

- 本版本覆盖当前登录状态下公开作品流实际返回的全部作品；未返回作品不会被伪装成已收录。
- 最早作品：{coverage["earliest_video_at"] or "未知"}
- 最新作品：{coverage["latest_video_at"] or "未知"}

## 异常清单

{exception_lines}
"""

    @staticmethod
    def remove_tree(path: Path) -> None:
        if path.exists():
            shutil.rmtree(path)
