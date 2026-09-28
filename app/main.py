from __future__ import annotations

import csv
import io
import json
import logging
import re
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime, time
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

from app.config import Settings
from app.database import Database
from app.exporter import video_markdown
from app.pipeline import Pipeline, TaskRunner

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


class RunRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2000)
    cutoff_date: str | None = None


class CookieRequest(BaseModel):
    cookie: str = Field(min_length=20, max_length=100000)


def _safe_download_name_part(value: Any, fallback: str) -> str:
    cleaned = re.sub(r'[\x00-\x1f\x7f/\\:*?"<>|]+', "_", str(value or ""))
    return cleaned.strip(" .") or fallback


def build_app(settings: Settings | None = None, pipeline: Pipeline | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.ensure_directories()
    db = pipeline.db if pipeline else Database(settings.db_path)
    pipeline = pipeline or Pipeline(settings, db)
    runner = TaskRunner(pipeline, settings.max_workers)
    harvester = pipeline.harvester

    interrupted = db.all("SELECT run_id FROM harvest_runs WHERE status IN ('QUEUED','RUNNING')")
    for row in interrupted:
        db.update_run(
            row["run_id"],
            status="FAILED",
            stage="INTERRUPTED",
            error_message="服务重启导致任务中断；重新提交后会复用已完成的逐字稿",
            completed_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        runner.shutdown()

    app = FastAPI(title="Silo 智仓", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.db = db
    app.state.pipeline = pipeline
    app.state.runner = runner

    def require_admin(x_silo_token: Annotated[str | None, Header()] = None) -> None:
        if settings.admin_token and x_silo_token != settings.admin_token:
            raise HTTPException(status_code=401, detail="管理令牌不正确")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(
            Path(__file__).parent / "templates" / "index.html",
            headers={"Cache-Control": "no-store, max-age=0"},
        )

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "silo"}

    @app.get("/api/diagnostics")
    async def diagnostics() -> dict[str, Any]:
        return {
            "douyin": harvester.diagnostic(),
            "transcript": pipeline.transcripts.diagnostic(),
            "llm": await pipeline.distiller.llm.diagnostic(),
        }

    @app.put("/api/settings/douyin-cookie", dependencies=[Depends(require_admin)])
    def save_cookie(request: CookieRequest) -> dict[str, Any]:
        try:
            harvester.save_cookie(request.cookie)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"saved": True, "cookie_count": harvester.diagnostic()["cookie_count"]}

    @app.post("/api/runs", dependencies=[Depends(require_admin)], status_code=202)
    def create_run(request: RunRequest) -> dict[str, str]:
        try:
            input_url = harvester.validate_url(request.url)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if request.cutoff_date:
            try:
                date = datetime.strptime(request.cutoff_date, "%Y-%m-%d").date()
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="截止日期格式应为 YYYY-MM-DD") from exc
            cutoff = datetime.combine(date, time.max, tzinfo=settings.tz)
        else:
            cutoff = datetime.now(settings.tz)
        run_id = db.create_run(input_url, cutoff.isoformat(timespec="seconds"))
        runner.submit(run_id)
        return {"run_id": run_id, "status": "QUEUED"}

    @app.get("/api/runs")
    def list_runs() -> dict[str, list[dict[str, Any]]]:
        return {"items": db.all("SELECT * FROM harvest_runs ORDER BY started_at DESC LIMIT 100")}

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str) -> dict[str, Any]:
        run = db.run_detail(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="任务不存在")
        return run

    @app.post("/api/runs/{run_id}/retry", dependencies=[Depends(require_admin)], status_code=202)
    def retry_run(run_id: str) -> dict[str, str]:
        run = db.one("SELECT * FROM harvest_runs WHERE run_id=?", (run_id,))
        if not run:
            raise HTTPException(status_code=404, detail="任务不存在")
        if run["status"] not in {"FAILED", "COMPLETED_WITH_ERRORS"}:
            raise HTTPException(status_code=409, detail="只有失败或部分完成的任务可以继续处理")
        if run.get("creator_id") and db.one(
            """SELECT run_id FROM harvest_runs
               WHERE creator_id=? AND status IN ('QUEUED','RUNNING') LIMIT 1""",
            (run["creator_id"],),
        ):
            raise HTTPException(status_code=409, detail="该博主已有任务正在执行，请等待完成")
        operation = run.get("operation") or "HARVEST"
        if run.get("creator_id") and run.get("stage") == "INTERRUPTED":
            operation = "RESUME"
        new_run_id = db.create_run(
            run["input_url"],
            run["content_cutoff_at"],
            operation=operation,
            creator_id=run.get("creator_id"),
        )
        runner.submit(new_run_id)
        return {"run_id": new_run_id, "status": "QUEUED"}

    @app.get("/api/creators")
    def creators() -> dict[str, list[dict[str, Any]]]:
        return {"items": db.list_creators()}

    def ensure_creator_idle(creator_id: str) -> None:
        active = db.one(
            """SELECT run_id FROM harvest_runs
               WHERE creator_id=? AND status IN ('QUEUED','RUNNING') LIMIT 1""",
            (creator_id,),
        )
        if active:
            raise HTTPException(status_code=409, detail="该博主已有任务正在执行，请等待完成")

    @app.get("/api/creators/{creator_id}")
    def creator_space(creator_id: str) -> dict[str, Any]:
        creator = next(
            (item for item in db.list_creators() if item["creator_id"] == creator_id), None
        )
        if not creator:
            raise HTTPException(status_code=404, detail="博主空间不存在")
        counts = (
            db.one(
                """SELECT
                 SUM(CASE WHEN transcript_status='PENDING' THEN 1 ELSE 0 END)
                   AS pending_transcript_count,
                 SUM(CASE WHEN transcript_status='FAILED' THEN 1 ELSE 0 END)
                   AS failed_transcript_count,
                 SUM(CASE WHEN json_type(raw_metadata_json,'$.images')='array'
                           AND json_array_length(raw_metadata_json,'$.images')>0
                          THEN 1 ELSE 0 END) AS image_post_count
               FROM creator_videos WHERE creator_id=?""",
                (creator_id,),
            )
            or {}
        )
        creator.update({key: int(value or 0) for key, value in counts.items()})
        return {
            "creator": creator,
            "runs": db.all(
                """SELECT * FROM harvest_runs WHERE creator_id=?
                   ORDER BY started_at DESC LIMIT 100""",
                (creator_id,),
            ),
            "versions": db.all(
                """SELECT * FROM corpus_versions WHERE creator_id=?
                   ORDER BY created_at DESC""",
                (creator_id,),
            ),
        }

    @app.post(
        "/api/creators/{creator_id}/refresh", dependencies=[Depends(require_admin)], status_code=202
    )
    def refresh_creator(creator_id: str) -> dict[str, str]:
        creator = db.one("SELECT * FROM creators WHERE creator_id=?", (creator_id,))
        if not creator:
            raise HTTPException(status_code=404, detail="博主不存在")
        ensure_creator_idle(creator_id)
        cutoff = datetime.now(settings.tz).isoformat(timespec="seconds")
        run_id = db.create_run(creator["source_url"], cutoff, creator_id=creator_id)
        runner.submit(run_id)
        return {"run_id": run_id, "status": "QUEUED"}

    @app.post(
        "/api/creators/{creator_id}/redistill",
        dependencies=[Depends(require_admin)],
        status_code=202,
    )
    def redistill_creator(creator_id: str) -> dict[str, str]:
        creator = db.one("SELECT * FROM creators WHERE creator_id=?", (creator_id,))
        if not creator:
            raise HTTPException(status_code=404, detail="博主不存在")
        ensure_creator_idle(creator_id)
        if not creator.get("last_successful_run_id"):
            raise HTTPException(status_code=409, detail="尚无完整语料版本，不能单独重新蒸馏")
        cutoff = creator.get("last_content_cutoff_at") or datetime.now(settings.tz).isoformat(
            timespec="seconds"
        )
        run_id = db.create_run(
            creator["source_url"],
            cutoff,
            operation="REDISTILL",
            creator_id=creator_id,
        )
        runner.submit(run_id)
        return {"run_id": run_id, "status": "QUEUED"}

    @app.get("/api/creators/{creator_id}/versions")
    def versions(creator_id: str) -> dict[str, list[dict[str, Any]]]:
        return {
            "items": db.all(
                "SELECT * FROM corpus_versions WHERE creator_id=? ORDER BY created_at DESC",
                (creator_id,),
            )
        }

    @app.get("/api/creators/{creator_id}/videos")
    def creator_videos(
        creator_id: str,
        q: str = Query(default="", max_length=200),
        status: str = Query(default="", pattern="^(|PENDING|COMPLETED|FAILED)$"),
        limit: int = Query(default=60, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        if not db.one("SELECT creator_id FROM creators WHERE creator_id=?", (creator_id,)):
            raise HTTPException(status_code=404, detail="博主不存在")
        return db.list_creator_videos(
            creator_id, query=q.strip(), status=status, limit=limit, offset=offset
        )

    @app.get("/api/creators/{creator_id}/corpus.zip")
    def download_current_corpus(creator_id: str) -> StreamingResponse:
        creator = db.one("SELECT * FROM creators WHERE creator_id=?", (creator_id,))
        if not creator:
            raise HTTPException(status_code=404, detail="博主不存在")
        videos = db.videos_for_creator(creator_id)
        completed = [video for video in videos if video["transcript_status"] == "COMPLETED"]
        if not completed:
            raise HTTPException(status_code=409, detail="该博主尚无可下载的完整逐字稿")
        source_counts: dict[str, int] = {}
        for video in completed:
            source = video.get("transcript_source") or "unknown"
            source_counts[source] = source_counts.get(source, 0) + 1
        missing_count = max(0, int(creator["platform_video_count"] or 0) - len(videos))
        output = io.BytesIO()
        index_buffer = io.StringIO(newline="")
        writer = csv.writer(index_buffer)
        writer.writerow(
            ["aweme_id", "title", "publish_time", "transcript_source", "transcript_chars"]
        )
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            for video in completed:
                writer.writerow(
                    [
                        video["aweme_id"],
                        video.get("title") or "",
                        video["publish_time"],
                        video.get("transcript_source") or "",
                        len(video.get("cleaned_transcript") or ""),
                    ]
                )
                archive.writestr(
                    f"corpus/{video['aweme_id']}.md",
                    video_markdown(video, settings.timezone),
                )
            archive.writestr("corpus_index.csv", index_buffer.getvalue())
            archive.writestr(
                "manifest.json",
                json.dumps(
                    {
                        "creator": {"id": creator_id, "nickname": creator["nickname"]},
                        "platform_video_count": creator["platform_video_count"],
                        "stored_video_count": len(videos),
                        "transcript_completed": len(completed),
                        "transcript_pending": sum(
                            video["transcript_status"] == "PENDING" for video in videos
                        ),
                        "transcript_failed": sum(
                            video["transcript_status"] == "FAILED" for video in videos
                        ),
                        "transcript_source_counts": source_counts,
                        "missing_platform_items": missing_count,
                        "coverage_claim": "WORKING_PARTIAL",
                        "is_formal_complete_version": False,
                        "note": (
                            "这是当前已完成文本的工作语料 ZIP，不是正式完整版本；"
                            "请以 manifest 的发现数、完成数和缺口数为准。"
                        ),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        output.seek(0)
        filename = f"silo-corpus-{datetime.now(settings.tz).date().isoformat()}.zip"
        return StreamingResponse(
            output,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    def video_or_404(aweme_id: str) -> dict[str, Any]:
        video = db.one("SELECT * FROM creator_videos WHERE aweme_id=?", (aweme_id,))
        if not video:
            raise HTTPException(status_code=404, detail="语料不存在")
        video.pop("raw_metadata_json", None)
        return video

    @app.get("/api/videos/{aweme_id}")
    def video_detail(aweme_id: str) -> dict[str, Any]:
        return video_or_404(aweme_id)

    @app.get("/api/videos/{aweme_id}/transcript.md")
    def download_transcript(aweme_id: str) -> PlainTextResponse:
        video = video_or_404(aweme_id)
        return PlainTextResponse(
            video_markdown(video, settings.timezone),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{aweme_id}.md"'},
        )

    def version_or_404(version_id: str) -> dict[str, Any]:
        version = db.one("SELECT * FROM corpus_versions WHERE version_id=?", (version_id,))
        if not version:
            raise HTTPException(status_code=404, detail="语料版本不存在")
        return version

    @app.get("/api/versions/{version_id}/download")
    def download_version(version_id: str) -> FileResponse:
        version = version_or_404(version_id)
        path = Path(version["export_path"])
        if not path.exists():
            raise HTTPException(status_code=404, detail="交付包文件不存在")
        creator = db.one(
            "SELECT nickname FROM creators WHERE creator_id=?",
            (version["creator_id"],),
        )
        nickname = _safe_download_name_part(
            (creator or {}).get("nickname"), "博主"
        )
        version_label = _safe_download_name_part(version["version_label"], "版本")
        filename = f"{nickname}-{version_label}.zip"
        return FileResponse(
            path,
            filename=filename,
            media_type="application/zip",
            headers={
                "Cache-Control": "no-store, max-age=0",
                "Pragma": "no-cache",
            },
        )

    @app.get("/api/versions/{version_id}/report")
    def report(version_id: str) -> dict[str, str]:
        version_or_404(version_id)
        profile = db.one(
            "SELECT report_markdown FROM creator_profiles WHERE corpus_version_id=?",
            (version_id,),
        )
        return {"markdown": profile["report_markdown"] if profile else ""}

    @app.get("/api/versions/{version_id}/skill")
    def skill(version_id: str) -> dict[str, str]:
        version_or_404(version_id)
        profile = db.one(
            "SELECT skill_markdown FROM creator_profiles WHERE corpus_version_id=?",
            (version_id,),
        )
        return {"markdown": profile["skill_markdown"] if profile else ""}

    return app


app = build_app()
