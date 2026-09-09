from pathlib import Path

import httpx

from app.config import Settings
from app.transcript import TranscriptService, normalize_transcript, parse_subtitle_payload


def test_parse_webvtt_keeps_full_text_and_removes_timing():
    payload = """WEBVTT

00:00:00.000 --> 00:00:02.000
第一句话。

00:00:02.000 --> 00:00:04.000
第二句话！
"""
    assert parse_subtitle_payload(payload) == "第一句话。\n第二句话！"


def test_normalization_does_not_delete_spoken_words():
    source = "其实   我认为，\n\n\n这个事情不能这么做。"
    assert normalize_transcript(source) == "其实 我认为，\n\n这个事情不能这么做。"


def test_transcribe_calls_private_asr_and_validates_timestamps(tmp_path, monkeypatch):
    media_path = tmp_path / "video.mp4"
    media_path.write_bytes(b"video")
    seen = {}

    class FakeClient:
        def __init__(self, timeout):
            seen["timeout"] = timeout

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, url, **kwargs):
            seen["url"] = url
            seen["data"] = kwargs["data"]
            return httpx.Response(
                200,
                json={
                    "text": "完整 转写。",
                    "segments": [{"id": 0, "start": 0.0, "end": 1.0, "text": "完整转写。"}],
                    "words": [{"word": "完整转写。", "start": 0.0, "end": 1.0}],
                },
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr(httpx, "Client", FakeClient)
    settings = Settings(
        data_dir=Path(tmp_path),
        asr_base_url="http://127.0.0.1:8000",
        asr_timeout_seconds=123,
    )
    service = TranscriptService(settings)

    assert service.transcribe(media_path) == "完整 转写。"
    assert seen["url"] == "http://127.0.0.1:8000/v1/audio/transcriptions"
    assert seen["data"]["timestamps"] == "true"
    assert seen["timeout"] == 123
