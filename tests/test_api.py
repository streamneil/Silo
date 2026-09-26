import io
import json
import zipfile

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import build_app


def test_api_serves_console_and_validates_homepage(tmp_path):
    settings = Settings(
        data_dir=tmp_path,
    )
    with TestClient(build_app(settings)) as client:
        assert client.get("/api/health").json()["status"] == "ok"
        assert "Silo 智仓" in client.get("/").text
        response = client.post("/api/runs", json={"url": "https://example.com/user/123"})
        assert response.status_code == 400
        assert "抖音" in response.json()["detail"]


def test_api_extracts_and_stores_url_from_douyin_share_text(tmp_path):
    settings = Settings(data_dir=tmp_path)
    app = build_app(settings)
    app.state.runner.submit = lambda _run_id: None
    shared = (
        "2- 长按复制此条消息，打开抖音搜索，查看TA的更多作品。 "
        "https://v.douyin.com/vKcepGgOn5s/ 0@2.com :5pm"
    )

    with TestClient(app) as client:
        response = client.post("/api/runs", json={"url": shared})
        assert response.status_code == 202
        run = app.state.db.one(
            "SELECT input_url FROM harvest_runs WHERE run_id=?",
            (response.json()["run_id"],),
        )
        assert run["input_url"] == "https://v.douyin.com/vKcepGgOn5s/"


def test_api_lists_and_serves_saved_transcripts(tmp_path):
    settings = Settings(
        data_dir=tmp_path,
    )
    app = build_app(settings)
    db = app.state.db
    run_id = db.create_run("https://v.douyin.com/demo/", "2026-09-04T12:00:00+08:00")
    db.upsert_creator(
        {
            "creator_id": "sec-demo",
            "nickname": "测试博主",
            "source_url": "https://v.douyin.com/demo/",
            "platform_video_count": 1,
        },
        run_id,
        "2026-09-04T12:00:00+08:00",
    )
    db.upsert_video(
        {
            "aweme_id": "123",
            "creator_id": "sec-demo",
            "title": "可以搜索的标题",
            "publish_time": 1700000000,
            "raw_metadata": {},
        },
        run_id,
    )
    db.save_transcript("123", source="asr-whisper", raw="忠实原稿", cleaned="整理后的正文", path="")

    with TestClient(app) as client:
        listing = client.get("/api/creators/sec-demo/videos?q=整理后").json()
        assert listing["total"] == 1
        assert listing["items"][0]["transcript_chars"] == 6
        detail = client.get("/api/videos/123").json()
        assert detail["raw_transcript"] == "忠实原稿"
        assert "raw_metadata_json" not in detail
        download = client.get("/api/videos/123/transcript.md")
        assert download.status_code == 200
        assert "整理后的正文" in download.text

        space = client.get("/api/creators/sec-demo").json()
        assert space["creator"]["nickname"] == "测试博主"
        assert space["runs"][0]["run_id"] == run_id

        corpus = client.get("/api/creators/sec-demo/corpus.zip")
        assert corpus.status_code == 200
        with zipfile.ZipFile(io.BytesIO(corpus.content)) as archive:
            assert "corpus/123.md" in archive.namelist()
            manifest = json.loads(archive.read("manifest.json"))
            assert manifest["transcript_completed"] == 1
            assert manifest["is_formal_complete_version"] is False


def test_console_contains_artifact_markdown_preview_components(tmp_path):
    settings = Settings(data_dir=tmp_path)
    with TestClient(build_app(settings)) as client:
        response = client.get("/")
        html = response.text
        assert response.headers["cache-control"] == "no-store, max-age=0"
        assert "artifact-preview" in html
        assert "artifact-show-preview" in html
        assert "artifact-show-raw" in html
        assert "artifact-copy" in html
        assert "markdown-body" in html


def test_console_explains_cookie_setup_and_reports_refresh_results(tmp_path):
    settings = Settings(data_dir=tmp_path)
    with TestClient(build_app(settings)) as client:
        html = client.get("/").text
        assert "space-action-notice" in html
        assert "打开抖音网页版并登录" in html
        assert "Command + Option + I" in html
        assert "Request Headers" in html
        assert "Cookie 属于登录凭证" in html
        assert "如果提示 Cookie 或平台拒绝请求" in html
        assert "未发现新增作品" in html


def test_console_markdown_renderer_uses_emphasis_safe_inline_code_tokens(tmp_path):
    settings = Settings(data_dir=tmp_path)
    with TestClient(build_app(settings)) as client:
        html = client.get("/").text
        assert "SILOINLINECODE${inlineCodes.length}TOKEN" in html
        assert "SILOINLINECODE(\\d+)TOKEN" in html
        assert "__INLINE_CODE_${inlineCodes.length}__" not in html


def test_api_serves_version_report_and_skill(tmp_path):
    settings = Settings(data_dir=tmp_path)
    app = build_app(settings)
    db = app.state.db
    run_id = db.create_run("https://v.douyin.com/demo/", "2026-09-04T12:00:00+08:00")
    db.upsert_creator(
        {
            "creator_id": "sec-demo",
            "nickname": "测试博主",
            "source_url": "https://v.douyin.com/demo/",
        },
        run_id,
        "2026-09-04T12:00:00+08:00",
    )
    version_id = "ver_demo123"
    db.execute(
        """INSERT INTO corpus_versions(
            version_id,creator_id,run_id,version_label,content_cutoff_at,
            earliest_video_at,latest_video_at,discovered_count,transcript_count,
            exception_count,manifest_path,export_path,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            version_id,
            "sec-demo",
            run_id,
            "1.0.0+20260907",
            "2026-09-04T12:00:00+08:00",
            "2026-06-01T00:00:00+08:00",
            "2026-09-06T00:00:00+08:00",
            10,
            10,
            0,
            str(tmp_path / "manifest.json"),
            str(tmp_path / "export.zip"),
            "2026-09-07T10:00:00+08:00",
        ),
    )
    db.execute(
        """INSERT INTO creator_profiles(
            profile_id,creator_id,corpus_version_id,model,prompt_version,sample_count,
            report_markdown,skill_markdown,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            "prof_demo123",
            "sec-demo",
            version_id,
            "mock-llm",
            "silo-distill-v1",
            10,
            "# 测试博主深度分析报告\n\n## 1. 核心特征\n- 节奏紧凑",
            "---\nname: demo-style\n---\n# Demo Skill\n\n- [x] 规范执行",
            "2026-09-07T10:00:00+08:00",
        ),
    )
    with TestClient(app) as client:
        rep = client.get(f"/api/versions/{version_id}/report")
        assert rep.status_code == 200
        assert "# 测试博主深度分析报告" in rep.json()["markdown"]

        skl = client.get(f"/api/versions/{version_id}/skill")
        assert skl.status_code == 200
        assert "name: demo-style" in skl.json()["markdown"]
