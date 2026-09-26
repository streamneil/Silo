import sys
import types
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.config import Settings
from app.harvester import DouyinHarvester
from app.harvester.douyin_client import HarvestError


def test_validate_url_extracts_homepage_from_douyin_share_text():
    shared = (
        "2- 长按复制此条消息，打开抖音搜索，查看TA的更多作品。 "
        "https://v.douyin.com/vKcepGgOn5s/ 0@2.com :5pm"
    )

    assert DouyinHarvester.validate_url(shared) == "https://v.douyin.com/vKcepGgOn5s/"


def test_validate_url_ignores_non_douyin_links_and_trailing_punctuation():
    shared = (
        "说明 https://example.com/not-douyin 然后打开主页（"
        "https://www.douyin.com/user/demo?from=share）。"
    )

    assert (
        DouyinHarvester.validate_url(shared)
        == "https://www.douyin.com/user/demo?from=share"
    )


def test_save_cookie_rejects_set_cookie_attributes_without_overwriting_valid_cookie(
    tmp_path,
):
    harvester = DouyinHarvester(Settings(data_dir=tmp_path))
    valid = "ttwid=one; odin_tt=two; passport_csrf_token=three"
    harvester.save_cookie(valid)

    with pytest.raises(ValueError, match="Request Headers"):
        harvester.save_cookie(
            "odin_tt=partial; Max-Age=31536000; Domain=.douyin.com; Path=/"
        )

    assert harvester.settings.cookie_path.read_text(encoding="utf-8") == valid


def test_invalid_saved_cookie_is_reported_without_breaking_diagnostics(tmp_path):
    harvester = DouyinHarvester(Settings(data_dir=tmp_path))
    harvester.settings.cookie_path.parent.mkdir(parents=True)
    harvester.settings.cookie_path.write_text(
        "odin_tt=partial; Max-Age=31536000; Domain=.douyin.com; Path=/",
        encoding="utf-8",
    )

    diagnostic = harvester.diagnostic()

    assert diagnostic["configured"] is False
    assert diagnostic["cookie_count"] == 1
    assert "Response Headers" in diagnostic["error"]
    with pytest.raises(HarvestError, match="Request Headers"):
        harvester.load_cookies()


class FakeBrowserClient:
    async def collect_user_post_ids_via_browser(self, *args, **kwargs):
        return ["1", "2", "3"]

    def pop_browser_post_aweme_items(self):
        return {
            "2": {"aweme_id": "2", "author": {"sec_uid": "creator"}},
        }

    async def get_video_detail(self, aweme_id, suppress_error=False):
        if aweme_id == "3":
            return {"aweme_id": "3", "author": {"sec_uid": "creator"}}
        return None


@pytest.mark.asyncio
async def test_browser_recovery_merges_metadata_and_details():
    items = [{"aweme_id": "1", "author": {"sec_uid": "creator"}}]
    recovered = await DouyinHarvester._recover_with_browser(
        FakeBrowserClient(), "creator", items, {"1"}, 3
    )
    assert [item["aweme_id"] for item in recovered] == ["1", "2", "3"]


def test_browser_page_data_builds_video_metadata():
    item = DouyinHarvester._browser_page_item(
        "123",
        "creator",
        {
            "title": "浏览器恢复的标题 - 抖音",
            "description": "浏览器恢复的标题 - 作者于20260925发布在抖音",
            "body_text": "发布时间：2026-09-25 13:45",
            "video_url": "https://example.com/video.mp4",
            "cover_url": "https://example.com/cover.jpg",
            "duration_seconds": 12.5,
        },
        "Asia/Shanghai",
    )

    assert item["desc"] == "浏览器恢复的标题"
    assert item["duration"] == 12500
    assert item["author"]["nickname"] == "作者"
    assert item["video"]["play_addr"]["url_list"] == [
        "https://example.com/video.mp4"
    ]
    assert item["video"]["cover"]["url_list"] == [
        "https://example.com/cover.jpg"
    ]
    expected = datetime(2026, 9, 25, 13, 45, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert item["create_time"] == int(expected.timestamp())
    assert DouyinHarvester._browser_item_matches_creator(item, "作者") is True
    assert DouyinHarvester._browser_item_matches_creator(item, "另一位博主") is False


class FakeMonthClient:
    def __init__(self):
        self.params = None

    async def _build_user_page_params(self, sec_uid, max_cursor, count):
        return {"sec_user_id": sec_uid, "max_cursor": max_cursor, "count": count}

    async def _request_json(self, path, params):
        self.params = params
        return {
            "aweme_list": [
                {"aweme_id": "old", "create_time": 1580000000},
            ]
        }

    def _normalize_paged_response(self, raw, item_keys):
        return {"items": raw["aweme_list"]}


@pytest.mark.asyncio
async def test_month_recovery_queries_older_history_window(tmp_path):
    client = FakeMonthClient()
    harvester = DouyinHarvester(Settings(data_dir=tmp_path))
    items = [{"aweme_id": "new", "create_time": 1582970000}]

    recovered = await harvester._recover_older_months(
        client,
        "creator",
        items,
        {"new"},
        ["2020·02", "2020·01", "2019·12"],
        2,
    )

    assert [item["aweme_id"] for item in recovered] == ["new", "old"]
    assert client.params["time_list_query"] == "1"
    assert client.params["need_time_list"] == "0"
    assert client.params["count"] == 50


@pytest.mark.asyncio
async def test_quick_harvest_rejects_empty_first_page_for_nonempty_creator(
    tmp_path, monkeypatch
):
    class EmptyPostClient:
        def __init__(self, _cookies):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get_user_info(self, _sec_uid):
            return {"nickname": "测试博主", "aweme_count": 58}

        async def get_user_post(self, _sec_uid, _cursor, _count):
            return {}

    class FakeURLParser:
        @staticmethod
        def parse(_url):
            return {"type": "user", "sec_uid": "creator"}

    monkeypatch.setitem(
        sys.modules,
        "core",
        types.SimpleNamespace(DouyinAPIClient=EmptyPostClient, URLParser=FakeURLParser),
    )
    harvester = DouyinHarvester(Settings(data_dir=tmp_path))
    harvester.save_cookie("ttwid=test; odin_tt=test; passport_csrf_token=test")
    monkeypatch.setattr(
        harvester,
        "_recover_with_browser",
        lambda *_args, **_kwargs: _async_result([]),
    )

    with pytest.raises(HarvestError, match="Cookie"):
        await harvester.harvest(
            "https://www.douyin.com/user/creator",
            max_videos=60,
        )


async def _async_result(value):
    return value


@pytest.mark.asyncio
async def test_quick_harvest_recovers_empty_api_page_through_browser(
    tmp_path, monkeypatch
):
    class EmptyPostClient:
        def __init__(self, _cookies):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get_user_info(self, _sec_uid):
            return {"nickname": "测试博主", "aweme_count": 1}

        async def get_user_post(self, _sec_uid, _cursor, _count):
            return {}

    class FakeURLParser:
        @staticmethod
        def parse(_url):
            return {"type": "user", "sec_uid": "creator"}

    monkeypatch.setitem(
        sys.modules,
        "core",
        types.SimpleNamespace(DouyinAPIClient=EmptyPostClient, URLParser=FakeURLParser),
    )
    harvester = DouyinHarvester(Settings(data_dir=tmp_path))
    harvester.save_cookie("ttwid=test; odin_tt=test; passport_csrf_token=test")
    recovered_item = {
        "aweme_id": "new-video",
        "author": {"sec_uid": "creator"},
        "desc": "浏览器恢复作品",
        "create_time": 1,
        "video": {"play_addr": {"url_list": ["https://example.com/video.mp4"]}},
    }

    async def recover(*_args, **_kwargs):
        return [recovered_item]

    monkeypatch.setattr(harvester, "_recover_with_browser", recover)

    result = await harvester.harvest(
        "https://www.douyin.com/user/creator",
        max_videos=60,
    )

    assert [video["aweme_id"] for video in result.videos] == ["new-video"]
