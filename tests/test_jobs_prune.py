"""异步任务状态机与 90 天清理。"""

from __future__ import annotations

import time
from dataclasses import replace

import pytest

from app import db
from app.jobs import ANALYZE_LOCK, JobManager, JobQueueFull
from app.pipeline import analyze_batch
from app.prune import prune
from app.settings import load_settings


@pytest.fixture()
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NEWS_SOURCE_MODE", "demo")
    monkeypatch.setenv("LLM_API_KEY", "")
    return load_settings()


def test_job_manager_runs_and_records_result(settings):
    db.init_db(settings.db_path)
    manager = JobManager(settings)
    manager.start()
    try:
        job = manager.submit(limit=4, force=False)
        job_id = job["id"]
        deadline = time.time() + 10
        while time.time() < deadline:
            conn = db.connect(settings.db_path)
            try:
                current = db.get_analysis_job(conn, job_id)
            finally:
                conn.close()
            if current and current["status"] in ("succeeded", "failed"):
                break
            time.sleep(0.05)
        assert current is not None
        assert current["status"] == "succeeded", current
        assert current["result"]["analyzed"] == 4
    finally:
        manager.shutdown()


def test_job_queue_limit_rejects_extra_jobs(settings):
    settings = replace(settings, max_pending_jobs=1)
    db.init_db(settings.db_path)
    conn = db.connect(settings.db_path)
    try:
        db.create_analysis_job(
            conn, job_id="queued", payload={"limit": 1, "force": False, "concurrency": None}
        )
    finally:
        conn.close()
    manager = JobManager(settings)
    try:
        with pytest.raises(JobQueueFull):
            manager.submit(limit=2, force=False)
    finally:
        manager.shutdown()


def test_job_fails_fast_when_analyze_lock_is_held(settings):
    db.init_db(settings.db_path)
    manager = JobManager(settings)
    manager.start()
    assert ANALYZE_LOCK.acquire(blocking=False)
    try:
        job = manager.submit(limit=1, force=False)
        deadline = time.time() + 5
        current = None
        while time.time() < deadline:
            conn = db.connect(settings.db_path)
            try:
                current = db.get_analysis_job(conn, job["id"])
            finally:
                conn.close()
            if current and current["status"] == "failed":
                break
            time.sleep(0.05)
        assert current is not None and current["status"] == "failed", current
        assert "已有分析任务" in (current.get("error") or "")
    finally:
        ANALYZE_LOCK.release()
        manager.shutdown()


def test_mark_stale_jobs_failed_and_active_job_reuse(settings):
    db.init_db(settings.db_path)
    conn = db.connect(settings.db_path)
    try:
        payload = {"limit": 1, "force": False, "concurrency": None}
        db.create_analysis_job(conn, job_id="old", payload=payload)
        assert db.count_active_analysis_jobs(conn) == 1
        assert db.find_active_job(conn, payload) is not None
        assert db.mark_stale_jobs_failed(conn) == 1
        assert db.count_active_analysis_jobs(conn) == 0
        assert db.find_active_job(conn, payload) is None
    finally:
        conn.close()


def test_prune_dry_run_then_delete_all_derived_tables(settings):
    analyze_batch(settings, limit=1)
    conn = db.connect(settings.db_path)
    try:
        conn.execute("UPDATE analyzed_articles SET analyzed_at = '2000-01-01 00:00:00'")
        conn.commit()
        before = conn.execute("SELECT COUNT(*) FROM analyzed_articles").fetchone()[0]
        assert before == 1
        assert conn.execute("SELECT COUNT(*) FROM article_tags").fetchone()[0] > 0
        assert conn.execute("SELECT COUNT(*) FROM extractions").fetchone()[0] > 0
        assert conn.execute("SELECT COUNT(*) FROM article_checklists").fetchone()[0] > 0
    finally:
        conn.close()

    dry = prune(settings, days=90, dry_run=True)
    assert dry["deleted"]["analyzed_articles"] == 1
    conn = db.connect(settings.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM analyzed_articles").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM article_tags").fetchone()[0] > 0
    finally:
        conn.close()

    real = prune(settings, days=90)
    assert real["deleted"]["analyzed_articles"] == 1
    conn = db.connect(settings.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM analyzed_articles").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM article_tags").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM extractions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM article_checklists").fetchone()[0] == 0
    finally:
        conn.close()


def test_prune_deletes_finished_analysis_jobs(settings):
    db.init_db(settings.db_path)
    conn = db.connect(settings.db_path)
    try:
        db.create_analysis_job(
            conn, job_id="old-job", payload={"limit": 1, "force": False, "concurrency": None}
        )
        conn.execute(
            "UPDATE analysis_jobs SET status='succeeded', finished_at='2000-01-01 00:00:00'"
        )
        conn.commit()
    finally:
        conn.close()
    dry = prune(settings, days=90, dry_run=True)
    assert dry["deleted"]["analysis_jobs"] == 1
    real = prune(settings, days=90)
    assert real["deleted"]["analysis_jobs"] == 1
    conn = db.connect(settings.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM analysis_jobs").fetchone()[0] == 0
    finally:
        conn.close()


def test_retention_zero_means_keep_forever(settings):
    result = prune(settings, days=0)
    assert result["skipped"] is True
