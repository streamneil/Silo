from unittest.mock import AsyncMock

import pytest

from app.config import Settings
from app.llm import LLMClient


@pytest.mark.asyncio
async def test_json_repairs_an_incomplete_first_response(tmp_path, monkeypatch):
    client = LLMClient(Settings(data_dir=tmp_path))
    chat = AsyncMock(side_effect=['{"topic":"未闭合', '{"topic":"已修复"}'])
    monkeypatch.setattr(client, "chat", chat)

    assert await client.json("system", "user") == {"topic": "已修复"}
    assert chat.await_count == 2


def test_parse_json_object_accepts_fenced_output(tmp_path):
    client = LLMClient(Settings(data_dir=tmp_path))
    assert client._parse_json_object('```json\n{"ok": true}\n```') == {"ok": True}
