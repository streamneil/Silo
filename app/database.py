from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS creators (
    creator_id TEXT PRIMARY KEY,
    nickname TEXT NOT NULL,
    source_url TEXT NOT NULL,
    avatar_url TEXT,
    signature TEXT,
    platform_video_count INTEGER DEFAULT 0,
    last_successful_run_id TEXT,
    last_content_cutoff_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS harvest_runs (
    run_id TEXT PRIMARY KEY,
    creator_id TEXT,
    input_url TEXT NOT NULL,
    run_type TEXT NOT NULL,
    operation TEXT NOT NULL DEFAULT 'HARVEST',
    previous_run_id TEXT,
    status TEXT NOT NULL,
    stage TEXT NOT NULL,
    content_cutoff_at TEXT NOT NULL,
    progress_current INTEGER DEFAULT 0,
    progress_total INTEGER DEFAULT 0,
    discovered_count INTEGER DEFAULT 0,
    new_count INTEGER DEFAULT 0,
    transcript_count INTEGER DEFAULT 0,
    failed_count INTEGER DEFAULT 0,
    error_message TEXT,
    version_id TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    FOREIGN KEY(creator_id) REFERENCES creators(creator_id)
);

CREATE TABLE IF NOT EXISTS run_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    level TEXT NOT NULL,
    stage TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES harvest_runs(run_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS creator_videos (
    aweme_id TEXT PRIMARY KEY,
    creator_id TEXT NOT NULL,
    title TEXT,
    publish_time INTEGER NOT NULL,
    duration_ms INTEGER DEFAULT 0,
    like_count INTEGER DEFAULT 0,
    comment_count INTEGER DEFAULT 0,
    share_count INTEGER DEFAULT 0,
    video_url TEXT,
    cover_url TEXT,
    raw_metadata_json TEXT NOT NULL,
    transcript_source TEXT,
    raw_transcript TEXT,
    cleaned_transcript TEXT,
    transcript_payload_json TEXT,
    transcript_model TEXT,
    transcript_language TEXT,
    transcript_duration_seconds REAL,
    transcript_generated_at TEXT,
    transcript_status TEXT NOT NULL DEFAULT 'PENDING',
    transcript_error TEXT,
    transcript_path TEXT,
    first_seen_run_id TEXT NOT NULL,
    last_seen_run_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(creator_id) REFERENCES creators(creator_id)
);

CREATE INDEX IF NOT EXISTS idx_videos_creator_publish
ON creator_videos(creator_id, publish_time DESC);
CREATE INDEX IF NOT EXISTS idx_videos_creator_likes
ON creator_videos(creator_id, like_count DESC);

CREATE TABLE IF NOT EXISTS transcript_revisions (
    revision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    aweme_id TEXT NOT NULL,
    transcript_source TEXT,
    raw_transcript TEXT NOT NULL,
    cleaned_transcript TEXT,
    transcript_payload_json TEXT,
    transcript_model TEXT,
    transcript_language TEXT,
    transcript_duration_seconds REAL,
    transcript_generated_at TEXT,
    archived_at TEXT NOT NULL,
    FOREIGN KEY(aweme_id) REFERENCES creator_videos(aweme_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS video_analyses (
    aweme_id TEXT NOT NULL,
    transcript_hash TEXT NOT NULL,
    model TEXT NOT NULL,
    analysis_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(aweme_id, transcript_hash, model),
    FOREIGN KEY(aweme_id) REFERENCES creator_videos(aweme_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS corpus_versions (
    version_id TEXT PRIMARY KEY,
    creator_id TEXT NOT NULL,
    run_id TEXT NOT NULL UNIQUE,
    version_label TEXT NOT NULL,
    content_cutoff_at TEXT NOT NULL,
    earliest_video_at TEXT,
    latest_video_at TEXT,
    discovered_count INTEGER NOT NULL,
    transcript_count INTEGER NOT NULL,
    exception_count INTEGER NOT NULL,
    manifest_path TEXT NOT NULL,
    export_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(creator_id) REFERENCES creators(creator_id),
    FOREIGN KEY(run_id) REFERENCES harvest_runs(run_id)
);

CREATE TABLE IF NOT EXISTS creator_profiles (
    profile_id TEXT PRIMARY KEY,
    creator_id TEXT NOT NULL,
    corpus_version_id TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    sample_count INTEGER NOT NULL,
    report_markdown TEXT NOT NULL,
    skill_markdown TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(creator_id) REFERENCES creators(creator_id),
    FOREIGN KEY(corpus_version_id) REFERENCES corpus_versions(version_id)
);
"""


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.RLock()
        self.initialize()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self) -> None:
        with self._write_lock, self.connection() as conn:
            conn.executescript(SCHEMA)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(harvest_runs)")}
            if "operation" not in columns:
                conn.execute(
                    "ALTER TABLE harvest_runs ADD COLUMN operation TEXT NOT NULL DEFAULT 'HARVEST'"
                )
            video_columns = {row[1] for row in conn.execute("PRAGMA table_info(creator_videos)")}
            transcript_columns = {
                "transcript_payload_json": "TEXT",
                "transcript_model": "TEXT",
                "transcript_language": "TEXT",
                "transcript_duration_seconds": "REAL",
                "transcript_generated_at": "TEXT",
            }
            for name, data_type in transcript_columns.items():
                if name not in video_columns:
                    conn.execute(f"ALTER TABLE creator_videos ADD COLUMN {name} {data_type}")

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        with self._write_lock, self.connection() as conn:
            conn.execute(sql, params)

    def one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    def all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.connection() as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def create_run(
        self,
        input_url: str,
        cutoff_at: str,
        *,
        operation: str = "HARVEST",
        creator_id: str | None = None,
    ) -> str:
        run_id = f"run_{uuid.uuid4().hex[:12]}"
        self.execute(
            """INSERT INTO harvest_runs(
                run_id,creator_id,input_url,run_type,operation,status,stage,
                content_cutoff_at,started_at
            ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                run_id,
                creator_id,
                input_url,
                "REDISTILL" if operation == "REDISTILL" else "FULL",
                operation,
                "QUEUED",
                "QUEUED",
                cutoff_at,
                utc_now(),
            ),
        )
        self.add_event(run_id, "INFO", "QUEUED", "任务已进入队列")
        return run_id

    def update_run(self, run_id: str, **fields: Any) -> None:
        allowed = {
            "creator_id",
            "run_type",
            "previous_run_id",
            "status",
            "stage",
            "progress_current",
            "progress_total",
            "discovered_count",
            "new_count",
            "transcript_count",
            "failed_count",
            "error_message",
            "version_id",
            "completed_at",
        }
        payload = {key: value for key, value in fields.items() if key in allowed}
        if not payload:
            return
        clause = ", ".join(f"{key}=?" for key in payload)
        self.execute(
            f"UPDATE harvest_runs SET {clause} WHERE run_id=?", (*payload.values(), run_id)
        )

    def add_event(self, run_id: str, level: str, stage: str, message: str) -> None:
        self.execute(
            "INSERT INTO run_events(run_id,level,stage,message,created_at) VALUES(?,?,?,?,?)",
            (run_id, level, stage, message[:1000], utc_now()),
        )

    def run_detail(self, run_id: str) -> dict[str, Any] | None:
        run = self.one(
            """SELECT r.*,c.nickname AS creator_nickname,
                      c.platform_video_count AS platform_video_count
               FROM harvest_runs r LEFT JOIN creators c ON c.creator_id=r.creator_id
               WHERE r.run_id=?""",
            (run_id,),
        )
        if run:
            run["events"] = self.all(
                """SELECT level,stage,message,created_at
                   FROM run_events WHERE run_id=? ORDER BY event_id""",
                (run_id,),
            )
        return run

    def upsert_creator(self, creator: dict[str, Any], run_id: str, cutoff_at: str) -> None:
        now = utc_now()
        self.execute(
            """INSERT INTO creators(
                creator_id,nickname,source_url,avatar_url,signature,platform_video_count,
                created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(creator_id) DO UPDATE SET
                nickname=excluded.nickname, source_url=excluded.source_url,
                avatar_url=excluded.avatar_url, signature=excluded.signature,
                platform_video_count=excluded.platform_video_count,
                updated_at=excluded.updated_at""",
            (
                creator["creator_id"],
                creator.get("nickname") or "未知博主",
                creator["source_url"],
                creator.get("avatar_url"),
                creator.get("signature"),
                int(creator.get("platform_video_count") or 0),
                now,
                now,
            ),
        )
        previous = self.one(
            """SELECT run_id FROM harvest_runs
               WHERE creator_id=? AND status IN ('COMPLETED','COMPLETED_WITH_ERRORS')
               ORDER BY completed_at DESC LIMIT 1""",
            (creator["creator_id"],),
        )
        self.update_run(
            run_id,
            creator_id=creator["creator_id"],
            run_type="INCREMENTAL" if previous else "FULL",
            previous_run_id=previous["run_id"] if previous else None,
        )

    def upsert_video(self, video: dict[str, Any], run_id: str) -> bool:
        existing = self.one(
            "SELECT aweme_id FROM creator_videos WHERE aweme_id=?", (video["aweme_id"],)
        )
        now = utc_now()
        self.execute(
            """INSERT INTO creator_videos(
                aweme_id,creator_id,title,publish_time,duration_ms,like_count,comment_count,
                share_count,video_url,cover_url,raw_metadata_json,first_seen_run_id,
                last_seen_run_id,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(aweme_id) DO UPDATE SET
                title=excluded.title, duration_ms=excluded.duration_ms,
                like_count=excluded.like_count, comment_count=excluded.comment_count,
                share_count=excluded.share_count, video_url=excluded.video_url,
                cover_url=excluded.cover_url, raw_metadata_json=excluded.raw_metadata_json,
                last_seen_run_id=excluded.last_seen_run_id, updated_at=excluded.updated_at""",
            (
                video["aweme_id"],
                video["creator_id"],
                video.get("title"),
                int(video.get("publish_time") or 0),
                int(video.get("duration_ms") or 0),
                int(video.get("like_count") or 0),
                int(video.get("comment_count") or 0),
                int(video.get("share_count") or 0),
                video.get("video_url"),
                video.get("cover_url"),
                json.dumps(video.get("raw_metadata") or {}, ensure_ascii=False),
                run_id,
                run_id,
                now,
                now,
            ),
        )
        return existing is None

    def videos_for_creator(
        self, creator_id: str, cutoff_timestamp: int | None = None
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM creator_videos WHERE creator_id=?"
        params: tuple[Any, ...] = (creator_id,)
        if cutoff_timestamp is not None:
            sql += " AND publish_time<=?"
            params += (cutoff_timestamp,)
        sql += " ORDER BY publish_time DESC, aweme_id DESC"
        return self.all(sql, params)

    def save_transcript(
        self,
        aweme_id: str,
        *,
        source: str,
        raw: str,
        cleaned: str,
        path: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        generated_at = utc_now()
        payload_json = (
            json.dumps(details, ensure_ascii=False, separators=(",", ":")) if details else None
        )
        model = str(details.get("model") or "") if details else ""
        language = str(details.get("language") or "") if details else ""
        duration = details.get("duration") if details else None
        with self._write_lock, self.connection() as conn:
            previous = conn.execute(
                "SELECT * FROM creator_videos WHERE aweme_id=?", (aweme_id,)
            ).fetchone()
            if (
                previous
                and previous["raw_transcript"]
                and (
                    previous["raw_transcript"] != raw
                    or previous["transcript_payload_json"] != payload_json
                )
            ):
                conn.execute(
                    """INSERT INTO transcript_revisions(
                       aweme_id,transcript_source,raw_transcript,cleaned_transcript,
                       transcript_payload_json,transcript_model,transcript_language,
                       transcript_duration_seconds,transcript_generated_at,archived_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        aweme_id,
                        previous["transcript_source"],
                        previous["raw_transcript"],
                        previous["cleaned_transcript"],
                        previous["transcript_payload_json"],
                        previous["transcript_model"],
                        previous["transcript_language"],
                        previous["transcript_duration_seconds"],
                        previous["transcript_generated_at"],
                        generated_at,
                    ),
                )
            conn.execute(
                """UPDATE creator_videos SET
                   transcript_source=?,raw_transcript=?,cleaned_transcript=?,
                   transcript_payload_json=?,transcript_model=?,transcript_language=?,
                   transcript_duration_seconds=?,transcript_generated_at=?,
                   transcript_status='COMPLETED',transcript_error=NULL,
                   transcript_path=?,updated_at=? WHERE aweme_id=?""",
                (
                    source,
                    raw,
                    cleaned,
                    payload_json,
                    model or None,
                    language or None,
                    float(duration) if duration is not None else None,
                    generated_at,
                    path,
                    generated_at,
                    aweme_id,
                ),
            )

    def fail_transcript(self, aweme_id: str, error: str) -> None:
        self.execute(
            """UPDATE creator_videos SET
               transcript_status=CASE
                   WHEN raw_transcript IS NOT NULL AND length(raw_transcript)>0
                   THEN 'COMPLETED' ELSE 'FAILED' END,
               transcript_error=?,updated_at=? WHERE aweme_id=?""",
            (error[:2000], utc_now(), aweme_id),
        )

    def save_video_analysis(
        self, aweme_id: str, digest: str, model: str, payload: dict[str, Any]
    ) -> None:
        self.execute(
            """INSERT OR REPLACE INTO video_analyses(
                aweme_id,transcript_hash,model,analysis_json,created_at
            ) VALUES(?,?,?,?,?)""",
            (aweme_id, digest, model, json.dumps(payload, ensure_ascii=False), utc_now()),
        )

    def get_video_analysis(self, aweme_id: str, digest: str, model: str) -> dict[str, Any] | None:
        row = self.one(
            """SELECT analysis_json FROM video_analyses
               WHERE aweme_id=? AND transcript_hash=? AND model=?""",
            (aweme_id, digest, model),
        )
        return json.loads(row["analysis_json"]) if row else None

    def list_creators(self) -> list[dict[str, Any]]:
        return self.all(
            """SELECT c.*,
               (SELECT COUNT(*) FROM creator_videos v WHERE v.creator_id=c.creator_id) video_count,
               (SELECT COUNT(*) FROM creator_videos v WHERE v.creator_id=c.creator_id
                  AND v.transcript_status='COMPLETED') transcript_count,
               (SELECT version_id FROM corpus_versions cv WHERE cv.creator_id=c.creator_id
                  ORDER BY cv.created_at DESC LIMIT 1) latest_version_id,
               (SELECT status FROM harvest_runs hr WHERE hr.creator_id=c.creator_id
                  ORDER BY hr.started_at DESC LIMIT 1) latest_run_status,
               (SELECT stage FROM harvest_runs hr WHERE hr.creator_id=c.creator_id
                  ORDER BY hr.started_at DESC LIMIT 1) latest_run_stage,
               (SELECT started_at FROM harvest_runs hr WHERE hr.creator_id=c.creator_id
                  ORDER BY hr.started_at DESC LIMIT 1) latest_run_started_at
               FROM creators c ORDER BY c.updated_at DESC"""
        )

    def list_creator_videos(
        self,
        creator_id: str,
        *,
        query: str = "",
        status: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        where = ["creator_id=?"]
        params: list[Any] = [creator_id]
        if status:
            where.append("transcript_status=?")
            params.append(status)
        if query:
            where.append(
                "(title LIKE ? OR cleaned_transcript LIKE ? OR "
                "raw_transcript LIKE ? OR aweme_id LIKE ?)"
            )
            term = f"%{query}%"
            params.extend([term, term, term, term])
        clause = " AND ".join(where)
        total = self.one(
            f"SELECT COUNT(*) AS count FROM creator_videos WHERE {clause}", tuple(params)
        )
        rows = self.all(
            f"""SELECT aweme_id,creator_id,title,publish_time,duration_ms,like_count,
                       cover_url,transcript_source,transcript_status,transcript_error,
                       length(coalesce(cleaned_transcript,'')) AS transcript_chars,
                       substr(coalesce(cleaned_transcript,''),1,180) AS transcript_preview
                FROM creator_videos WHERE {clause}
                ORDER BY publish_time DESC,aweme_id DESC LIMIT ? OFFSET ?""",
            (*params, limit, offset),
        )
        return {"items": rows, "total": int((total or {}).get("count") or 0)}
