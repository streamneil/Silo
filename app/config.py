from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    host: str = "0.0.0.0"
    port: int = 8001
    timezone: str = "Asia/Shanghai"
    llm_base_url: str = "http://127.0.0.1:18080/v1"
    llm_model: str = "qwen3.8-27b-q8_0"
    llm_api_key: str = ""
    llm_timeout_seconds: float = 600.0
    asr_language: str = "zh"
    asr_base_url: str = "http://127.0.0.1:8000"
    asr_api_key: str = ""
    asr_timeout_seconds: float = 7200.0
    transcription_workers: int = 4
    keep_media: bool = False
    max_workers: int = 1
    admin_token: str = ""

    @classmethod
    def from_env(cls) -> Settings:
        project_root = Path(__file__).resolve().parents[1]
        data_dir = Path(os.getenv("SILO_DATA_DIR", project_root / "data")).expanduser().resolve()
        return cls(
            data_dir=data_dir,
            host=os.getenv("SILO_HOST", "0.0.0.0"),
            port=int(os.getenv("SILO_PORT", "8001")),
            timezone=os.getenv("SILO_TIMEZONE", "Asia/Shanghai"),
            llm_base_url=os.getenv("SILO_LLM_BASE_URL", "http://127.0.0.1:18080/v1").rstrip("/"),
            llm_model=os.getenv("SILO_LLM_MODEL", "qwen3.8-27b-q8_0"),
            llm_api_key=os.getenv("SILO_LLM_API_KEY", ""),
            llm_timeout_seconds=float(os.getenv("SILO_LLM_TIMEOUT_SECONDS", "600")),
            asr_language=os.getenv("SILO_ASR_LANGUAGE", "zh"),
            asr_base_url=os.getenv("SILO_ASR_BASE_URL", "http://127.0.0.1:8000").rstrip("/"),
            asr_api_key=os.getenv("SILO_ASR_API_KEY", ""),
            asr_timeout_seconds=float(os.getenv("SILO_ASR_TIMEOUT_SECONDS", "7200")),
            transcription_workers=max(1, int(os.getenv("SILO_TRANSCRIPTION_WORKERS", "4"))),
            keep_media=_env_bool("SILO_KEEP_MEDIA", False),
            max_workers=max(1, int(os.getenv("SILO_MAX_WORKERS", "1"))),
            admin_token=os.getenv("SILO_ADMIN_TOKEN", ""),
        )

    @property
    def db_path(self) -> Path:
        return self.data_dir / "silo.db"

    @property
    def cookie_path(self) -> Path:
        return self.data_dir / "private" / "douyin_cookies.txt"

    @property
    def corpus_dir(self) -> Path:
        return self.data_dir / "corpus"

    @property
    def versions_dir(self) -> Path:
        return self.data_dir / "versions"

    @property
    def work_dir(self) -> Path:
        return self.data_dir / "work"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def ensure_directories(self) -> None:
        for path in (
            self.data_dir,
            self.cookie_path.parent,
            self.corpus_dir,
            self.versions_dir,
            self.work_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
