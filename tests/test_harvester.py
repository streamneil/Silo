import pytest

from app.config import Settings
from app.harvester import DouyinHarvester


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
