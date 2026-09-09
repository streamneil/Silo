from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime

from app.config import Settings
from app.database import Database, utc_now
from app.distiller import PROMPT_VERSION, Distiller
from app.exporter import Exporter
from app.harvester import DouyinHarvester
from app.llm import LLMError
from app.transcript import TranscriptError, TranscriptService, normalize_transcript

logger = logging.getLogger(__name__)


class Pipeline:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        harvester: DouyinHarvester | None = None,
        transcripts: TranscriptService | None = None,
        distiller: Distiller | None = None,
        exporter: Exporter | None = None,
    ):
        self.settings = settings
        self.db = db
        self.harvester = harvester or DouyinHarvester(settings)
        self.transcripts = transcripts or TranscriptService(settings)
        self.distiller = distiller or Distiller(settings, db)
        self.exporter = exporter or Exporter(settings)

    def _next_version_label(self, creator_id: str, cutoff_at: str) -> str:
        existing = self.db.one(
            "SELECT COUNT(*) AS count FROM corpus_versions WHERE creator_id=?",
            (creator_id,),
        )
        patch = int((existing or {}).get("count") or 0)
        cutoff = datetime.fromisoformat(cutoff_at).astimezone(self.settings.tz)
        return f"1.0.{patch}+{cutoff:%Y%m%d}"

    def _stage(self, run_id: str, stage: str, message: str) -> None:
        self.db.update_run(run_id, status="RUNNING", stage=stage)
        self.db.add_event(run_id, "INFO", stage, message)

    @staticmethod
    def _audio_for_upload(media_path):
        """Strip video before sending media over the WAN tunnel to the GPU."""
        afconvert = shutil.which("afconvert")
        if not afconvert:
            return media_path
        audio_path = media_path.with_suffix(".m4a")
        result = subprocess.run(
            [
                afconvert,
                str(media_path),
                "-o",
                str(audio_path),
                "-f",
                "m4af",
                "-d",
                "0",
            ],
            capture_output=True,
            check=False,
        )
        if result.returncode or not audio_path.exists() or not audio_path.stat().st_size:
            audio_path.unlink(missing_ok=True)
            logger.warning(
                "Audio extraction failed for %s; uploading original media: %s",
                media_path,
                result.stderr.decode("utf-8", errors="replace")[-300:],
            )
            return media_path
        return audio_path

    async def _download_media(self, url: str, destination, cookie_header: str) -> None:
        curl = shutil.which("curl")
        if not curl or not isinstance(self.transcripts, TranscriptService):
            await self.transcripts.download_video(url, destination, cookie_header)
            return
        destination.parent.mkdir(parents=True, exist_ok=True)
        headers = [
            "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Referer: https://www.douyin.com/",
        ]
        if cookie_header:
            headers.append(f"Cookie: {cookie_header}")
        process = await asyncio.create_subprocess_exec(
            curl,
            "--location",
            "--fail",
            "--silent",
            "--show-error",
            "--retry",
            "5",
            "--retry-all-errors",
            "--retry-delay",
            "2",
            "--connect-timeout",
            "20",
            "--max-time",
            "900",
            "--continue-at",
            "-",
            "--header",
            "@-",
            "--output",
            str(destination),
            url,
            stdin=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate(("\n".join(headers) + "\n").encode())
        if process.returncode or not destination.exists() or destination.stat().st_size < 1024:
            raise RuntimeError("视频下载失败：" + stderr.decode("utf-8", errors="replace")[-400:])

    async def _transcribe_video(
        self,
        run_id: str,
        creator_id: str,
        video: dict[str, object],
        cookie_header: str,
    ) -> None:
        reusable = (
            video["transcript_status"] == "COMPLETED"
            and video.get("raw_transcript")
            and (
                video.get("transcript_source") == "image-ocr"
                or bool(video.get("transcript_payload_json"))
            )
        )
        if reusable:
            self.exporter.write_canonical_video(creator_id, video)
            return

        aweme_id = str(video["aweme_id"])
        media_path = self.settings.work_dir / run_id / f"{aweme_id}.mp4"
        try:
            metadata = json.loads(str(video["raw_metadata_json"]))
            image_text = await self.transcripts.image_post_text(metadata, cookie_header)
            if image_text:
                raw_text, source, details = image_text, "image-ocr", None
            else:
                official = await self.transcripts.official_subtitle(metadata, cookie_header)
                for attempt in range(4):
                    try:
                        await self._download_media(
                            str(video.get("video_url") or ""), media_path, cookie_header
                        )
                        break
                    except Exception:
                        if attempt == 3:
                            raise
                        await asyncio.sleep(2 ** (attempt + 1))
                upload_path = await asyncio.to_thread(self._audio_for_upload, media_path)
                try:
                    details = await asyncio.to_thread(
                        self.transcripts.transcribe_details, upload_path
                    )
                    source = "asr-gpu"
                except TranscriptError as exc:
                    if "没有识别出文字" not in str(exc):
                        raise
                    details = await asyncio.to_thread(
                        self.transcripts.video_frame_text,
                        media_path,
                        str(metadata.get("desc") or video.get("title") or ""),
                        float(video.get("duration_ms") or 0) / 1000,
                    )
                    source = "video-ocr"
                if official:
                    details["official_reference_text"] = official
                raw_text = str(details["text"])
            cleaned = normalize_transcript(raw_text)
            self.db.save_transcript(
                aweme_id,
                source=source,
                raw=raw_text,
                cleaned=cleaned,
                path="",
                details=details,
            )
            refreshed = self.db.one("SELECT * FROM creator_videos WHERE aweme_id=?", (aweme_id,))
            path = self.exporter.write_canonical_video(creator_id, refreshed)
            self.db.execute(
                "UPDATE creator_videos SET transcript_path=? WHERE aweme_id=?",
                (str(path), aweme_id),
            )
            self.db.add_event(
                run_id,
                "INFO",
                "TRANSCRIBING",
                f"{aweme_id} 完成（{source}）",
            )
        except Exception as exc:
            error = str(exc)[:1000]
            self.db.fail_transcript(aweme_id, error)
            self.db.add_event(
                run_id,
                "ERROR",
                "TRANSCRIBING",
                f"{aweme_id} 失败：{error}",
            )
        finally:
            if not self.settings.keep_media:
                media_path.unlink(missing_ok=True)
                media_path.with_suffix(".m4a").unlink(missing_ok=True)

    async def _transcribe_videos(
        self,
        run_id: str,
        creator_id: str,
        videos: list[dict[str, object]],
        cookie_header: str,
    ) -> None:
        semaphore = asyncio.Semaphore(self.settings.transcription_workers)

        def priority(video: dict[str, object]) -> int:
            if video.get("transcript_status") != "COMPLETED" or not video.get("raw_transcript"):
                return 0
            if video.get("transcript_source") != "image-ocr" and not video.get(
                "transcript_payload_json"
            ):
                return 1
            return 2

        videos = sorted(videos, key=priority)

        async def limited(video: dict[str, object]) -> None:
            async with semaphore:
                await self._transcribe_video(run_id, creator_id, video, cookie_header)

        tasks = [asyncio.create_task(limited(video)) for video in videos]
        for completed, task in enumerate(asyncio.as_completed(tasks), start=1):
            await task
            self.db.update_run(
                run_id,
                progress_current=completed,
                progress_total=len(videos),
            )

    async def execute(self, run_id: str, max_videos: int = 0) -> None:
        run = self.db.one("SELECT * FROM harvest_runs WHERE run_id=?", (run_id,))
        if not run:
            raise RuntimeError(f"run not found: {run_id}")
        creator_id: str | None = run.get("creator_id")
        if run.get("operation") == "HARVEST" and creator_id:
            # Creator-space refreshes and retries should resume from stored assets.
            # Only a brand-new homepage submission needs the full first harvest path.
            run["operation"] = "RESUME"
        try:
            if run.get("operation") == "REDISTILL":
                creator_id = run.get("creator_id")
                if not creator_id:
                    raise RuntimeError("重新蒸馏任务缺少博主空间")
                cutoff_timestamp = int(datetime.fromisoformat(run["content_cutoff_at"]).timestamp())
                videos = self.db.videos_for_creator(creator_id, cutoff_timestamp)
                transcript_count = sum(
                    video["transcript_status"] == "COMPLETED" for video in videos
                )
                failed_count = len(videos) - transcript_count
                self.db.update_run(
                    run_id,
                    run_type="REDISTILL",
                    discovered_count=len(videos),
                    new_count=0,
                    transcript_count=transcript_count,
                    failed_count=failed_count,
                    progress_total=transcript_count,
                )
                if not transcript_count:
                    raise RuntimeError("该博主空间尚无完整逐字稿，无法重新蒸馏")
                await self._distill_and_export(
                    run_id, creator_id, videos, transcript_count, failed_count
                )
                return

            cutoff_timestamp = int(datetime.fromisoformat(run["content_cutoff_at"]).timestamp())
            incomplete_warning: str | None = None
            if run.get("operation") == "RESUME":
                creator_id = run.get("creator_id")
                creator = self.db.one("SELECT * FROM creators WHERE creator_id=?", (creator_id,))
                if not creator_id or not creator:
                    raise RuntimeError("续跑任务缺少博主空间")
                stored_count = len(self.db.videos_for_creator(creator_id, cutoff_timestamp))
                expected_count = int(creator.get("platform_video_count") or 0)
                refreshed_count = 0
                recent_refresh = False
                recent_runs = self.db.all(
                    """SELECT run_id,stage,started_at FROM harvest_runs
                       WHERE creator_id=? AND run_id<>?
                         AND stage IN (
                             'TRANSCRIBING','INTERRUPTED','DISTILLING','EXPORTING','FAILED'
                         )
                       ORDER BY started_at DESC LIMIT 10""",
                    (creator_id, run_id),
                )
                for previous in recent_runs:
                    age_seconds = (
                        datetime.now().astimezone().timestamp()
                        - datetime.fromisoformat(previous["started_at"]).timestamp()
                    )
                    if age_seconds >= 3600:
                        continue
                    refreshed = self.db.one(
                        """SELECT COUNT(*) AS count FROM creator_videos
                           WHERE creator_id=? AND last_seen_run_id=?""",
                        (creator_id, previous["run_id"]),
                    )
                    refreshed_count = int((refreshed or {}).get("count") or 0)
                    if refreshed_count == stored_count:
                        recent_refresh = True
                        break

                new_count = 0
                if recent_refresh:
                    self.db.add_event(
                        run_id,
                        "INFO",
                        "HARVESTING",
                        f"复用上一轮刚刷新的 {refreshed_count} 条媒体地址，直接继续转写",
                    )
                else:
                    self._stage(run_id, "HARVESTING", "正在快速检测最新作品")
                    known_ids = {
                        str(video["aweme_id"])
                        for video in self.db.videos_for_creator(creator_id, cutoff_timestamp)
                    }
                    scan_limit = max_videos or 60
                    harvest = await self.harvester.harvest(
                        run["input_url"], max_videos=scan_limit
                    )
                    harvested_creator_id = str(harvest.creator.get("creator_id") or "")
                    if harvested_creator_id != creator_id:
                        raise RuntimeError("刷新后的博主身份与原空间不一致，已停止续跑")
                    quick_ids = {str(video["aweme_id"]) for video in harvest.videos}
                    if not max_videos and len(harvest.videos) >= scan_limit and not (
                        quick_ids & known_ids
                    ):
                        self.db.add_event(
                            run_id,
                            "WARNING",
                            "HARVESTING",
                            "最新 60 条都不在现有语料库中，自动切换为全量分页以避免漏采",
                        )
                        harvest = await self.harvester.harvest(run["input_url"], max_videos=0)
                    self.db.upsert_creator(harvest.creator, run_id, run["content_cutoff_at"])
                    discovered = [
                        video
                        for video in harvest.videos
                        if int(video["publish_time"]) <= cutoff_timestamp
                    ]
                    new_count = sum(self.db.upsert_video(video, run_id) for video in discovered)
                    stored_count = len(self.db.videos_for_creator(creator_id, cutoff_timestamp))
                    self.db.add_event(
                        run_id,
                        "INFO",
                        "HARVESTING",
                        (
                            f"快速检测最新 {len(discovered)} 条作品；"
                            f"空间累计 {stored_count} 条，新增 {new_count} 条"
                        ),
                    )
                self.db.update_run(
                    run_id,
                    run_type="INCREMENTAL",
                    discovered_count=stored_count,
                    new_count=new_count,
                    progress_total=stored_count,
                )
                creator = self.db.one("SELECT * FROM creators WHERE creator_id=?", (creator_id,))
                expected_count = int((creator or {}).get("platform_video_count") or expected_count)
                if expected_count and stored_count < expected_count:
                    incomplete_warning = (
                        f"主页标称 {expected_count} 条作品，但当前登录状态下只发现 "
                        f"{stored_count} 条；已保存结果，未发现作品仍需补采"
                    )
            else:
                self._stage(run_id, "HARVESTING", "正在解析主页并收集作品列表")
                harvest = await self.harvester.harvest(run["input_url"], max_videos=max_videos)
                creator_id = harvest.creator["creator_id"]
                self.db.upsert_creator(harvest.creator, run_id, run["content_cutoff_at"])
                discovered = [
                    v for v in harvest.videos if int(v["publish_time"]) <= cutoff_timestamp
                ]
                new_count = sum(self.db.upsert_video(video, run_id) for video in discovered)
                self.db.update_run(
                    run_id,
                    discovered_count=len(discovered),
                    new_count=new_count,
                    progress_total=len(discovered),
                )
                self.db.add_event(
                    run_id,
                    "INFO",
                    "HARVESTING",
                    (
                        f"主页标称 {harvest.creator.get('platform_video_count', 0)} 条，"
                        f"本次发现 {len(discovered)} 条作品，新增 {new_count} 条"
                    ),
                )
                if not harvest.complete:
                    stored_count = len(self.db.videos_for_creator(creator_id, cutoff_timestamp))
                    expected_count = int(harvest.creator.get("platform_video_count") or 0)
                    if not expected_count or stored_count < expected_count:
                        incomplete_warning = (
                            f"主页标称 {expected_count} 条作品，当前公开作品流返回 "
                            f"{stored_count} 条；博主空间已保存这些唯一作品元数据"
                        )
                        self.db.add_event(
                            run_id,
                            "WARNING",
                            "HARVESTING",
                            f"{incomplete_warning}；先继续生成现有作品的逐字稿",
                        )
                    else:
                        self.db.add_event(
                            run_id,
                            "WARNING",
                            "HARVESTING",
                            (
                                "本次分页未单独覆盖平台标称数量，"
                                f"但空间累计已有 {stored_count} 条，"
                                "继续补齐逐字稿"
                            ),
                        )

            videos = self.db.videos_for_creator(creator_id, cutoff_timestamp)
            latest_version = self.db.one(
                """SELECT * FROM corpus_versions WHERE creator_id=?
                   ORDER BY created_at DESC LIMIT 1""",
                (creator_id,),
            )
            all_text_ready = bool(videos) and all(
                video.get("transcript_status") == "COMPLETED" for video in videos
            )
            if new_count == 0 and latest_version and all_text_ready:
                self.db.update_run(
                    run_id,
                    status="COMPLETED",
                    stage="DONE",
                    version_id=latest_version["version_id"],
                    transcript_count=len(videos),
                    failed_count=0,
                    progress_current=len(videos),
                    progress_total=len(videos),
                    completed_at=utc_now(),
                    error_message=None,
                )
                self.db.add_event(
                    run_id,
                    "INFO",
                    "DONE",
                    f"未发现新增作品；现有交付版本 {latest_version['version_label']} 仍有效",
                )
                return

            self._stage(run_id, "TRANSCRIBING", "正在生成每条视频的完整逐字稿")
            cookie_header = self.harvester.cookie_header()
            await self._transcribe_videos(run_id, creator_id, videos, cookie_header)

            videos = self.db.videos_for_creator(creator_id, cutoff_timestamp)
            transcript_count = sum(v["transcript_status"] == "COMPLETED" for v in videos)
            failed_count = len(videos) - transcript_count
            self.db.update_run(run_id, transcript_count=transcript_count, failed_count=failed_count)
            if not transcript_count:
                raise RuntimeError("没有任何视频成功生成逐字稿，无法创建完整语料版本")
            await self._distill_and_export(
                run_id,
                creator_id,
                videos,
                transcript_count,
                failed_count,
                coverage_warning=incomplete_warning,
            )
        except Exception as exc:
            logger.exception("Pipeline run %s failed", run_id)
            self.db.update_run(
                run_id,
                status="FAILED",
                stage="FAILED",
                error_message=str(exc)[:2000],
                completed_at=utc_now(),
            )
            self.db.add_event(run_id, "ERROR", "FAILED", str(exc)[:1000])

    async def _distill_and_export(
        self,
        run_id: str,
        creator_id: str,
        videos: list[dict[str, object]],
        transcript_count: int,
        failed_count: int,
        coverage_warning: str | None = None,
    ) -> None:
        self._stage(
            run_id,
            "DISTILLING",
            "正在并发分析全部完整逐字稿；每条结果会独立保存，可断点续跑",
        )
        creator = self.db.one("SELECT * FROM creators WHERE creator_id=?", (creator_id,))

        def progress(current: int, total: int, message: str) -> None:
            self.db.update_run(run_id, progress_current=current, progress_total=total)
            if current == total or current % 10 == 0:
                self.db.add_event(run_id, "INFO", "DISTILLING", message)

        for attempt in range(3):
            try:
                result = await self.distiller.distill(creator, videos, progress=progress)
                break
            except LLMError as exc:
                if attempt == 2:
                    raise
                delay = 5 * (attempt + 1)
                self.db.add_event(
                    run_id,
                    "WARNING",
                    "DISTILLING",
                    f"模型临时异常：{exc}；{delay} 秒后自动续试",
                )
                await asyncio.sleep(delay)
        self._stage(run_id, "EXPORTING", "正在生成带截止日期的完整交付包")
        run = self.db.one("SELECT * FROM harvest_runs WHERE run_id=?", (run_id,))
        version_label = self._next_version_label(creator_id, run["content_cutoff_at"])
        snapshot = self.exporter.create_snapshot(
            creator=creator,
            run=run,
            videos=videos,
            report=result.report_markdown,
            skill=result.skill_markdown,
            analyzed_count=result.analyzed_count,
            version_label=version_label,
        )
        coverage = snapshot["manifest"]["coverage"]
        self.db.execute(
            """INSERT INTO corpus_versions(
                version_id,creator_id,run_id,version_label,content_cutoff_at,
                earliest_video_at,latest_video_at,discovered_count,transcript_count,
                exception_count,manifest_path,export_path,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                snapshot["version_id"],
                creator_id,
                run_id,
                snapshot["version_label"],
                run["content_cutoff_at"],
                coverage["earliest_video_at"],
                coverage["latest_video_at"],
                coverage["discovered_videos"],
                coverage["transcript_completed"],
                coverage["exceptions"],
                snapshot["manifest_path"],
                snapshot["export_path"],
                utc_now(),
            ),
        )
        self.db.execute(
            """INSERT INTO creator_profiles(
                profile_id,creator_id,corpus_version_id,model,prompt_version,sample_count,
                report_markdown,skill_markdown,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                f"prof_{snapshot['version_id'][4:]}",
                creator_id,
                snapshot["version_id"],
                self.settings.llm_model,
                PROMPT_VERSION,
                result.analyzed_count,
                result.report_markdown,
                snapshot["skill_markdown"],
                utc_now(),
            ),
        )
        final_status = (
            "COMPLETED_WITH_ERRORS" if failed_count or coverage_warning else "COMPLETED"
        )
        completed_at = utc_now()
        self.db.update_run(
            run_id,
            status=final_status,
            stage="DONE",
            version_id=snapshot["version_id"],
            error_message=coverage_warning,
            completed_at=completed_at,
            progress_current=len(videos),
            progress_total=len(videos),
        )
        self.db.execute(
            """UPDATE creators SET last_successful_run_id=?,last_content_cutoff_at=?,updated_at=?
               WHERE creator_id=?""",
            (run_id, run["content_cutoff_at"], completed_at, creator_id),
        )
        message = f"审计交付包已生成：{snapshot['version_label']}，完整文本 {transcript_count} 条"
        if coverage_warning:
            message += f"；{coverage_warning}，已在版本清单中明确披露"
        self.db.add_event(
            run_id,
            "WARNING" if coverage_warning else "INFO",
            "DONE",
            message,
        )


class TaskRunner:
    def __init__(self, pipeline: Pipeline, max_workers: int = 1):
        self.pipeline = pipeline
        self.executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="silo")
        self.futures: dict[str, Future[None]] = {}

    def submit(self, run_id: str, max_videos: int = 0) -> None:
        future = self.executor.submit(
            lambda: asyncio.run(self.pipeline.execute(run_id, max_videos))
        )
        self.futures[run_id] = future

    def shutdown(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=False)
