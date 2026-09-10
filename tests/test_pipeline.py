import asyncio
import json
import threading
import time
import zipfile
from datetime import UTC, datetime

import pytest

import app.transcript as transcript_module
from app.artifact_integrity import (
    ArtifactIntegrityError,
    assert_no_unresolved_inline_code_tokens,
)
from app.config import Settings
from app.database import Database
from app.distiller import DistillationResult, Distiller
from app.exporter import golden_quote, readable_paragraphs, video_markdown
from app.harvester import CreatorHarvest
from app.llm import LLMClient, LLMError
from app.pipeline import Pipeline
from app.transcript import TranscriptError, TranscriptService


class FakeHarvester:
    def __init__(self, *, complete=True, platform_video_count=None):
        self.harvest_calls = 0
        self.requested_limits = []
        self.complete = complete
        self.platform_video_count = platform_video_count
        self.items = [
            {
                "aweme_id": "v1",
                "creator_id": "sec-demo",
                "title": "官方字幕作品",
                "publish_time": 1700000000,
                "like_count": 20,
                "video_url": "https://example.invalid/v1.mp4",
                "raw_metadata": {
                    "caption_info": {"text": "这是第一条视频的完整官方字幕文本，内容足够长。"}
                },
            },
            {
                "aweme_id": "v2",
                "creator_id": "sec-demo",
                "title": "ASR 作品",
                "publish_time": 1700000100,
                "like_count": 10,
                "video_url": "https://example.invalid/v2.mp4",
                "raw_metadata": {},
            },
        ]

    async def harvest(self, input_url, max_videos=0):
        self.harvest_calls += 1
        self.requested_limits.append(max_videos)
        items = self.items[:max_videos] if max_videos else self.items
        return CreatorHarvest(
            creator={
                "creator_id": "sec-demo",
                "nickname": "示例博主",
                "source_url": input_url,
                "platform_video_count": self.platform_video_count or len(items),
            },
            videos=items,
            complete=self.complete,
            warning="分页不完整" if not self.complete else "",
        )

    def cookie_header(self):
        return "cookie=test"


class FakeTranscripts:
    def __init__(self):
        self.transcribe_calls = 0
        self.image_calls = 0

    async def image_post_text(self, metadata, cookie_header=""):
        if not metadata.get("images"):
            return None
        self.image_calls += 1
        return "【作品说明】\n图文作品\n\n【图片 1/1】\n图片中的完整文字"

    async def official_subtitle(self, metadata, cookie_header=""):
        return metadata.get("caption_info", {}).get("text")

    async def download_video(self, url, destination, cookie_header=""):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"fake media")
        return destination

    def transcribe_details(self, media_path):
        self.transcribe_calls += 1
        return {
            "id": media_path.stem,
            "task": "transcribe",
            "model": "Paraformer-large",
            "language": "zh",
            "duration": 6.25,
            "text": "这是通过语音识别得到的完整字幕文本。",
            "segments": [
                {
                    "id": 0,
                    "start": 0.24,
                    "end": 6.25,
                    "text": "这是通过语音识别得到的完整字幕文本。",
                }
            ],
            "words": [{"word": "这是", "start": 0.24, "end": 0.72}],
            "processing_time": 0.5,
        }

    def video_frame_text(self, media_path, description="", duration_seconds=0):
        return {
            "id": media_path.stem,
            "task": "video_ocr",
            "model": "Apple-Vision",
            "language": "zh",
            "duration": duration_seconds,
            "text": f"【作品说明】\n{description}\n\n【静态视频画面文字】\n画面中的完整文字",
            "segments": [
                {
                    "id": 0,
                    "start": 0.0,
                    "end": duration_seconds,
                    "text": "画面中的完整文字",
                }
            ],
            "words": [],
        }


class FakeDistiller:
    def __init__(self):
        self.calls = 0

    async def distill(self, creator, videos, progress=None):
        self.calls += 1
        usable = [v for v in videos if v["transcript_status"] == "COMPLETED"]
        if progress:
            progress(len(usable), len(usable), "done")
        return DistillationResult(
            report_markdown="# 完整分析报告\n\n基于全部语料。",
            skill_markdown="---\nname: demo-style\ndescription: 示例\n---\n\n# 写作规则",
            analyzed_count=len(usable),
        )


class FlakyDistiller(FakeDistiller):
    def __init__(self):
        self.calls = 0

    async def distill(self, creator, videos, progress=None):
        self.calls += 1
        if self.calls == 1:
            raise LLMError("模型返回了空内容")
        usable = [v for v in videos if v["transcript_status"] == "COMPLETED"]
        return DistillationResult("# 报告", "---\nname: demo\n---\n\n# Skill", len(usable))


class ConcurrentFakeTranscripts(FakeTranscripts):
    def __init__(self):
        super().__init__()
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()

    def transcribe_details(self, media_path):
        with self.lock:
            self.transcribe_calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(0.05)
        with self.lock:
            self.active -= 1
        return {
            "model": "Paraformer-large",
            "language": "zh",
            "duration": 1.0,
            "text": f"{media_path.stem} 的并发语音识别结果。",
            "segments": [
                {
                    "id": 0,
                    "start": 0.0,
                    "end": 1.0,
                    "text": f"{media_path.stem} 的并发语音识别结果。",
                }
            ],
            "words": [],
        }


def test_artifact_integrity_rejects_unresolved_inline_code_tokens():
    assert_no_unresolved_inline_code_tokens("正常的 `中文概念`", "report")
    with pytest.raises(ArtifactIntegrityError, match="INLINECODE12"):
        assert_no_unresolved_inline_code_tokens("关键词：INLINECODE12", "report")


def test_readable_transcript_helpers_preserve_words_and_select_exact_quote():
    source = (
        "第一句只是普通介绍。真正重要的不是追逐流量，而是建立长期信任。"
        "这是后续补充说明，用来验证可读段落。"
    )
    readable = readable_paragraphs(source, target_chars=32)
    quote = golden_quote({"cleaned_transcript": source})

    assert "\n\n" in readable
    assert readable.replace("\n", "") == source
    assert quote == "真正重要的不是追逐流量，而是建立长期信任。"


@pytest.mark.asyncio
async def test_llm_disables_hidden_reasoning_for_corpus_jobs(tmp_path, monkeypatch):
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "完成"}}]}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, _url, headers=None, json=None):
            captured.update(json)
            return Response()

    monkeypatch.setattr("app.llm.httpx.AsyncClient", lambda **_kwargs: Client())
    text = await LLMClient(Settings(data_dir=tmp_path)).chat("系统", "任务")

    assert text == "完成"
    assert captured["chat_template_kwargs"] == {"enable_thinking": False}


@pytest.mark.asyncio
async def test_pipeline_transcribes_videos_concurrently(tmp_path):
    settings = Settings(data_dir=tmp_path, transcription_workers=4)
    settings.ensure_directories()
    db = Database(settings.db_path)
    harvester = FakeHarvester(platform_video_count=8)
    harvester.items = [
        {
            "aweme_id": f"concurrent-{index}",
            "creator_id": "sec-demo",
            "title": f"并发作品 {index}",
            "publish_time": 1700000000 + index,
            "like_count": index,
            "video_url": f"https://example.invalid/{index}.mp4",
            "raw_metadata": {},
        }
        for index in range(8)
    ]
    transcripts = ConcurrentFakeTranscripts()
    pipeline = Pipeline(
        settings,
        db,
        harvester=harvester,
        transcripts=transcripts,
        distiller=FakeDistiller(),
    )
    run_id = db.create_run(
        "https://v.douyin.com/concurrent/", datetime(2026, 9, 4, tzinfo=UTC).isoformat()
    )

    await pipeline.execute(run_id)

    assert db.run_detail(run_id)["status"] == "COMPLETED"
    assert transcripts.transcribe_calls == 8
    assert transcripts.max_active == 4


@pytest.mark.asyncio
async def test_pipeline_builds_complete_version_and_reuses_transcripts(tmp_path):
    settings = Settings(
        data_dir=tmp_path,
    )
    settings.ensure_directories()
    db = Database(settings.db_path)
    transcripts = FakeTranscripts()
    harvester = FakeHarvester()
    distiller = FakeDistiller()
    pipeline = Pipeline(
        settings,
        db,
        harvester=harvester,
        transcripts=transcripts,
        distiller=distiller,
    )
    cutoff = datetime(2026, 9, 4, tzinfo=UTC).isoformat()
    first = db.create_run("https://v.douyin.com/demo/", cutoff)
    await pipeline.execute(first)
    first_run = db.run_detail(first)
    assert first_run["status"] == "COMPLETED"
    assert first_run["new_count"] == 2
    assert first_run["transcript_count"] == 2
    version = db.one("SELECT * FROM corpus_versions WHERE run_id=?", (first,))
    assert version["version_label"] == "1.0.0+20260904"
    with zipfile.ZipFile(version["export_path"]) as archive:
        names = set(archive.namelist())
        assert "manifest.json" in names
        assert "SKILL.md" in names
        assert "creator_profile.md" in names
        assert "商品交付说明.md" in names
        assert len([name for name in names if name.startswith("corpus/")]) == 2
        readable_names = [name for name in names if name.startswith("readable_transcripts/")]
        assert len(readable_names) == 2
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["coverage"]["transcript_completed"] == 2
        assert manifest["version_label"] == "1.0.0+20260904"
        assert manifest["artifacts"]["readable_transcripts"] == "readable_transcripts/"
        quotes_name = manifest["artifacts"]["quotes"]
        assert quotes_name == "示例博主金句_1.0.0+20260904.txt"
        quotes = archive.read(quotes_name).decode()
        assert "版本：1.0.0+20260904" in quotes
        assert "本版本新增：2 条" in quotes
        assert "【本版本新增】" in quotes
        readable = archive.read(readable_names[0]).decode()
        assert "版本：1.0.0+20260904" in readable
        assert "正文\n----" in readable
        assert 'version: "1.0.0+20260904"' in archive.read("SKILL.md").decode()

    second = db.create_run("https://v.douyin.com/demo/", cutoff)
    await pipeline.execute(second)
    second_run = db.run_detail(second)
    assert second_run["status"] == "COMPLETED"
    assert second_run["run_type"] == "INCREMENTAL"
    assert second_run["new_count"] == 0
    assert transcripts.transcribe_calls == 2
    assert distiller.calls == 1
    assert db.one("SELECT COUNT(*) AS count FROM corpus_versions")["count"] == 1

    timestamped = db.one("SELECT * FROM creator_videos WHERE aweme_id='v2'")
    payload = json.loads(timestamped["transcript_payload_json"])
    assert payload["segments"][0]["start"] == 0.24
    markdown = video_markdown(timestamped, settings.timezone)
    assert "## 时间轴逐字稿" in markdown
    assert "[00:00:00.240 → 00:00:06.250]" in markdown
    assert 'transcript_model: "Paraformer-large"' in markdown

    resume = db.create_run(
        "https://v.douyin.com/demo/",
        cutoff,
        operation="RESUME",
        creator_id="sec-demo",
    )
    await pipeline.execute(resume)
    resume_run = db.run_detail(resume)
    assert resume_run["status"] == "COMPLETED"
    assert resume_run["run_type"] == "INCREMENTAL"
    assert transcripts.transcribe_calls == 2
    assert harvester.harvest_calls == 3
    assert harvester.requested_limits[-1] == 60
    assert distiller.calls == 1

    harvester.items.insert(
        0,
        {
            "aweme_id": "v3",
            "creator_id": "sec-demo",
            "title": "三天后的新增作品",
            "publish_time": 1700000200,
            "like_count": 30,
            "video_url": "https://example.invalid/v3.mp4",
            "raw_metadata": {"caption_info": {"text": "这是新增作品的完整官方字幕。"}},
        },
    )
    harvester.platform_video_count = 3
    incremental = db.create_run(
        "https://v.douyin.com/demo/",
        cutoff,
        creator_id="sec-demo",
    )
    await pipeline.execute(incremental)
    incremental_run = db.run_detail(incremental)
    assert incremental_run["status"] == "COMPLETED"
    assert incremental_run["run_type"] == "INCREMENTAL"
    assert incremental_run["new_count"] == 1
    assert incremental_run["transcript_count"] == 3
    assert harvester.requested_limits[-1] == 60
    incremental_version = db.one(
        "SELECT * FROM corpus_versions WHERE run_id=?", (incremental,)
    )
    assert incremental_version["version_label"] == "1.0.1+20260904"
    assert distiller.calls == 2
    with zipfile.ZipFile(incremental_version["export_path"]) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        quotes = archive.read(manifest["artifacts"]["quotes"]).decode()
        assert "版本：1.0.1+20260904" in quotes
        assert "本版本新增：1 条" in quotes
        assert "【本版本新增】这是通过语音识别得到的完整字幕文本。" in quotes
        assert "来源：三天后的新增作品" in quotes
        assert len(
            [name for name in archive.namelist() if name.startswith("readable_transcripts/")]
        ) == 3

    third = db.create_run(
        "https://v.douyin.com/demo/",
        cutoff,
        operation="REDISTILL",
        creator_id="sec-demo",
    )
    await pipeline.execute(third)
    third_run = db.run_detail(third)
    assert third_run["status"] == "COMPLETED"
    assert third_run["run_type"] == "REDISTILL"
    assert third_run["new_count"] == 0
    assert third_run["transcript_count"] == 3
    assert harvester.harvest_calls == 4
    third_version = db.one("SELECT * FROM corpus_versions WHERE run_id=?", (third,))
    assert third_version["version_label"] == "1.0.2+20260904"
    assert distiller.calls == 3

    db.fail_transcript("v2", "时间轴补录失败")
    preserved = db.one("SELECT * FROM creator_videos WHERE aweme_id='v2'")
    assert preserved["transcript_status"] == "COMPLETED"
    assert preserved["transcript_error"] == "时间轴补录失败"


@pytest.mark.asyncio
async def test_pipeline_retries_transient_llm_failure(tmp_path, monkeypatch):
    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr("app.pipeline.asyncio.sleep", no_sleep)
    settings = Settings(data_dir=tmp_path)
    settings.ensure_directories()
    db = Database(settings.db_path)
    distiller = FlakyDistiller()
    pipeline = Pipeline(
        settings,
        db,
        harvester=FakeHarvester(),
        transcripts=FakeTranscripts(),
        distiller=distiller,
    )
    run_id = db.create_run(
        "https://v.douyin.com/demo/", datetime(2026, 9, 4, tzinfo=UTC).isoformat()
    )

    await pipeline.execute(run_id)

    assert distiller.calls == 2
    assert db.run_detail(run_id)["status"] == "COMPLETED"
    assert db.one("SELECT * FROM corpus_versions WHERE run_id=?", (run_id,))


@pytest.mark.asyncio
async def test_distiller_batches_concurrently_and_retries_empty_response(tmp_path):
    class BatchLLM:
        def __init__(self):
            self.active = 0
            self.max_active = 0
            self.failed_once = False

        async def json(self, _system, user):
            if "作品数据：\n" not in user:
                return {"persona": "示例"}
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            await asyncio.sleep(0.02)
            self.active -= 1
            if not self.failed_once:
                self.failed_once = True
                raise LLMError("模型返回了空内容")
            source = json.loads(user.split("作品数据：\n", 1)[1])
            return {
                "analyses": [
                    {
                        "aweme_id": item["aweme_id"],
                        "analysis": {"core_argument": item["aweme_id"]},
                    }
                    for item in source
                ]
            }

        async def chat(self, system, _user, **_kwargs):
            await asyncio.sleep(0.01)
            if "Skill" in system:
                return "---\nname: batch-style\ndescription: 批量测试\n---\n\n# Skill"
            return "# 分析报告"

    settings = Settings(data_dir=tmp_path, transcription_workers=8)
    settings.ensure_directories()
    db = Database(settings.db_path)
    run_id = db.create_run(
        "https://v.douyin.com/batch/", datetime(2026, 9, 4, tzinfo=UTC).isoformat()
    )
    creator = {
        "creator_id": "batch-demo",
        "nickname": "批量博主",
        "source_url": "https://v.douyin.com/batch/",
        "platform_video_count": 8,
    }
    db.upsert_creator(creator, run_id, datetime(2026, 9, 4, tzinfo=UTC).isoformat())
    for index in range(8):
        aweme_id = f"batch-{index}"
        db.upsert_video(
            {
                "aweme_id": aweme_id,
                "creator_id": "batch-demo",
                "title": f"作品 {index}",
                "publish_time": 1700000000 + index,
                "raw_metadata": {},
            },
            run_id,
        )
        db.save_transcript(
            aweme_id,
            source="asr-gpu",
            raw=f"第 {index} 条逐字稿，包含足够的测试文本。",
            cleaned=f"第 {index} 条逐字稿",
            path="",
            details={"model": "test", "segments": []},
        )
    llm = BatchLLM()
    result = await Distiller(settings, db, llm=llm).distill(
        creator, db.videos_for_creator("batch-demo")
    )

    assert result.analyzed_count == 8
    assert llm.failed_once is True
    assert llm.max_active >= 2
    assert db.one("SELECT COUNT(*) AS count FROM video_analyses")["count"] == 8


@pytest.mark.asyncio
async def test_distiller_rejects_unresolved_inline_code_tokens(tmp_path):
    class PlaceholderLLM:
        async def json(self, _system, user):
            source = json.loads(user.split("作品数据：\n", 1)[1])
            return {
                "analyses": [
                    {
                        "aweme_id": item["aweme_id"],
                        "analysis": {"core_argument": "测试论点"},
                    }
                    for item in source
                ]
            }

        async def chat(self, system, _user, **_kwargs):
            if "Skill" in system:
                return "---\nname: safe-style\ndescription: 测试\n---\n\n# Skill"
            return "# 分析报告\n\n关键词：INLINECODE0"

    settings = Settings(data_dir=tmp_path)
    settings.ensure_directories()
    db = Database(settings.db_path)
    run_id = db.create_run(
        "https://v.douyin.com/placeholder/", datetime(2026, 9, 10, tzinfo=UTC).isoformat()
    )
    creator = {
        "creator_id": "placeholder-demo",
        "nickname": "占位符测试",
        "source_url": "https://v.douyin.com/placeholder/",
        "platform_video_count": 1,
    }
    db.upsert_creator(creator, run_id, datetime(2026, 9, 10, tzinfo=UTC).isoformat())
    db.upsert_video(
        {
            "aweme_id": "placeholder-video",
            "creator_id": creator["creator_id"],
            "title": "测试作品",
            "publish_time": 1700000000,
            "raw_metadata": {},
        },
        run_id,
    )
    db.save_transcript(
        "placeholder-video",
        source="test",
        raw="这是一条用于验证报告完整性的逐字稿。",
        cleaned="这是一条用于验证报告完整性的逐字稿。",
        path="",
        details={"model": "test", "segments": []},
    )

    with pytest.raises(LLMError, match="INLINECODE0"):
        await Distiller(settings, db, llm=PlaceholderLLM()).distill(
            creator, db.videos_for_creator(creator["creator_id"])
        )


@pytest.mark.asyncio
async def test_pipeline_exports_audited_version_for_partial_platform_harvest(tmp_path):
    settings = Settings(
        data_dir=tmp_path,
    )
    settings.ensure_directories()
    db = Database(settings.db_path)
    pipeline = Pipeline(
        settings,
        db,
        harvester=FakeHarvester(complete=False, platform_video_count=3),
        transcripts=FakeTranscripts(),
        distiller=FakeDistiller(),
    )
    run_id = db.create_run(
        "https://v.douyin.com/demo/", datetime(2026, 9, 4, tzinfo=UTC).isoformat()
    )
    await pipeline.execute(run_id)

    run = db.run_detail(run_id)
    assert run["status"] == "COMPLETED_WITH_ERRORS"
    assert run["discovered_count"] == 2
    assert "返回 2 条" in run["error_message"]
    assert len(db.videos_for_creator("sec-demo")) == 2
    assert run["transcript_count"] == 2
    version = db.one("SELECT * FROM corpus_versions WHERE run_id=?", (run_id,))
    assert version is not None
    with zipfile.ZipFile(version["export_path"]) as archive:
        manifest = json.loads(archive.read("manifest.json"))
    assert manifest["coverage"]["platform_claimed_videos"] == 3
    assert manifest["coverage"]["missing_platform_items"] == 1
    assert manifest["coverage"]["coverage_claim"] == "PUBLICLY_DISCOVERED_CORPUS"


@pytest.mark.asyncio
async def test_pipeline_falls_back_to_video_ocr_when_asr_finds_no_speech(tmp_path):
    class SilentVideoTranscripts(FakeTranscripts):
        def transcribe_details(self, media_path):
            self.transcribe_calls += 1
            raise TranscriptError("ASR 服务返回 422：ASR 没有识别出文字")

    settings = Settings(data_dir=tmp_path)
    settings.ensure_directories()
    db = Database(settings.db_path)
    harvester = FakeHarvester(platform_video_count=1)
    harvester.items = [
        {
            "aweme_id": "silent-card",
            "creator_id": "sec-demo",
            "title": "静态文字视频",
            "publish_time": 1700000000,
            "duration_ms": 30000,
            "like_count": 12,
            "video_url": "https://example.invalid/silent-card.mp4",
            "raw_metadata": {"desc": "静态文字视频"},
        }
    ]
    transcripts = SilentVideoTranscripts()
    pipeline = Pipeline(
        settings,
        db,
        harvester=harvester,
        transcripts=transcripts,
        distiller=FakeDistiller(),
    )
    run_id = db.create_run(
        "https://v.douyin.com/demo/", datetime(2026, 9, 4, tzinfo=UTC).isoformat()
    )

    await pipeline.execute(run_id)

    video = db.one("SELECT * FROM creator_videos WHERE aweme_id='silent-card'")
    assert video["transcript_status"] == "COMPLETED"
    assert video["transcript_source"] == "video-ocr"
    assert "画面中的完整文字" in video["raw_transcript"]
    payload = json.loads(video["transcript_payload_json"])
    assert payload["segments"][0]["end"] == 30.0


@pytest.mark.asyncio
async def test_pipeline_uses_ocr_text_for_image_posts(tmp_path):
    settings = Settings(data_dir=tmp_path)
    settings.ensure_directories()
    db = Database(settings.db_path)
    harvester = FakeHarvester(platform_video_count=1)
    harvester.items = [
        {
            "aweme_id": "image-1",
            "creator_id": "sec-demo",
            "title": "图文作品",
            "publish_time": 1700000000,
            "like_count": 12,
            "video_url": "https://example.invalid/background-music.mp4",
            "raw_metadata": {
                "desc": "图文作品",
                "images": [{"url_list": ["https://example.invalid/slide.jpeg"]}],
            },
        }
    ]
    transcripts = FakeTranscripts()
    pipeline = Pipeline(
        settings,
        db,
        harvester=harvester,
        transcripts=transcripts,
        distiller=FakeDistiller(),
    )
    run_id = db.create_run(
        "https://v.douyin.com/demo/", datetime(2026, 9, 4, tzinfo=UTC).isoformat()
    )

    await pipeline.execute(run_id)

    video = db.one("SELECT * FROM creator_videos WHERE aweme_id='image-1'")
    assert video["transcript_status"] == "COMPLETED"
    assert video["transcript_source"] == "image-ocr"
    assert "图片中的完整文字" in video["raw_transcript"]
    assert transcripts.image_calls == 1
    assert transcripts.transcribe_calls == 0


@pytest.mark.asyncio
async def test_image_post_retries_an_interrupted_image_download(tmp_path, monkeypatch):
    class Response:
        content = b"image" * 100

        def raise_for_status(self):
            return None

    class Client:
        calls = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, _url, headers=None):
            self.calls += 1
            if self.calls < 3:
                raise RuntimeError("peer closed connection")
            return Response()

    client = Client()

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(transcript_module.httpx, "AsyncClient", lambda **_kwargs: client)
    monkeypatch.setattr(transcript_module.asyncio, "sleep", no_sleep)
    service = TranscriptService(Settings(data_dir=tmp_path))
    monkeypatch.setattr(service, "_ocr_image", lambda _path: "图片正文")

    text = await service.image_post_text(
        {"desc": "作品说明", "images": [{"url_list": ["https://example.invalid/1"]}]}
    )

    assert client.calls == 3
    assert "作品说明" in text
    assert "图片正文" in text
