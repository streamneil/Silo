from __future__ import annotations

import argparse
import configparser
from pathlib import Path

from app.config import Settings
from app.harvester import DouyinHarvester


def main() -> int:
    parser = argparse.ArgumentParser(description="Import a cookie from the legacy downloader")
    parser.add_argument("config", type=Path)
    args = parser.parse_args()
    config = configparser.RawConfigParser()
    if not config.read(args.config):
        raise SystemExit(f"Legacy config not found: {args.config}")
    raw = config.get("douyin-env", "cookie", fallback="").strip()
    if not raw:
        raise SystemExit("Legacy config has no cookie")
    settings = Settings.from_env()
    settings.ensure_directories()
    DouyinHarvester(settings).save_cookie(raw)
    print(f"Imported Douyin cookie to {settings.cookie_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
