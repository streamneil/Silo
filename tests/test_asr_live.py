import os
from pathlib import Path

import pytest

from app.config import Settings
from app.transcript import TranscriptService


@pytest.mark.skipif(
    not os.getenv("SILO_LIVE_ASR_MEDIA"),
    reason="set SILO_LIVE_ASR_MEDIA to run against the deployed GPU ASR",
)
def test_live_gpu_asr_returns_complete_text_and_monotonic_timestamps(tmp_path):
    media_path = Path(os.environ["SILO_LIVE_ASR_MEDIA"]).expanduser().resolve()
    settings = Settings(
        data_dir=tmp_path,
        asr_base_url=os.getenv("SILO_ASR_BASE_URL", "http://127.0.0.1:8000"),
        asr_api_key=os.getenv("SILO_ASR_API_KEY", ""),
        asr_timeout_seconds=7200,
        asr_language="zh",
    )

    result = TranscriptService(settings).transcribe_details(media_path)

    assert result["text"].strip()
    assert result["language"]
    assert result["duration"] > 0
    assert result["segments"]
    assert result["words"]
    previous_end = 0.0
    for item in result["words"]:
        assert 0 <= item["start"] <= item["end"] <= result["duration"] + 0.5
        assert item["start"] >= previous_end - 0.1
        previous_end = item["end"]
