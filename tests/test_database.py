from datetime import UTC, datetime

from app.database import Database


def test_database_tracks_full_then_incremental_run(tmp_path):
    db = Database(tmp_path / "silo.db")
    cutoff = datetime.now(UTC).isoformat()
    first = db.create_run("https://v.douyin.com/demo/", cutoff)
    creator = {
        "creator_id": "sec-demo",
        "nickname": "示例博主",
        "source_url": "https://v.douyin.com/demo/",
        "platform_video_count": 1,
    }
    db.upsert_creator(creator, first, cutoff)
    assert (
        db.one("SELECT run_type FROM harvest_runs WHERE run_id=?", (first,))["run_type"] == "FULL"
    )
    db.update_run(first, status="COMPLETED", completed_at=cutoff)

    second = db.create_run(creator["source_url"], cutoff)
    db.upsert_creator(creator, second, cutoff)
    run = db.one("SELECT * FROM harvest_runs WHERE run_id=?", (second,))
    assert run["run_type"] == "INCREMENTAL"
    assert run["previous_run_id"] == first


def test_video_upsert_is_idempotent(tmp_path):
    db = Database(tmp_path / "silo.db")
    cutoff = datetime.now(UTC).isoformat()
    run = db.create_run("https://v.douyin.com/demo/", cutoff)
    db.upsert_creator(
        {"creator_id": "sec", "nickname": "N", "source_url": "https://v.douyin.com/demo/"},
        run,
        cutoff,
    )
    video = {
        "aweme_id": "1",
        "creator_id": "sec",
        "title": "标题",
        "publish_time": 1,
        "raw_metadata": {},
    }
    assert db.upsert_video(video, run) is True
    assert db.upsert_video({**video, "like_count": 99}, run) is False
    assert db.one("SELECT like_count FROM creator_videos WHERE aweme_id='1'")["like_count"] == 99
