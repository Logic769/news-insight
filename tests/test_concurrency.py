"""并发分析：批内多篇并行、主线程批量提交不影响结果。"""

from __future__ import annotations

from dataclasses import replace

import pytest

from app import db
from app.pipeline import analyze_batch
from app.settings import load_settings


@pytest.fixture()
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NEWS_SOURCE_MODE", "demo")
    monkeypatch.setenv("LLM_API_KEY", "")
    return load_settings()


def test_concurrent_batch_analyzes_all(settings):
    result = analyze_batch(settings, limit=4, concurrency=4)
    assert result["mode"] == "demo"
    assert result["scanned"] == 4
    assert result["analyzed"] == 4
    assert result["errors"] == 0

    conn = db.connect(settings.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM analyzed_articles").fetchone()[0] == 4
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    finally:
        conn.close()


def test_small_write_batch_still_commits_everything(settings):
    settings = replace(settings, analyze_write_batch=2, analyze_concurrency=3)
    result = analyze_batch(settings, limit=4)
    assert result["analyzed"] == 4
    conn = db.connect(settings.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM analyzed_articles").fetchone()[0] == 4
        assert conn.execute("SELECT COUNT(*) FROM article_tags").fetchone()[0] > 0
        run = conn.execute("SELECT finished_at, analyzed FROM runs").fetchone()
        assert run is not None and run[0] and run[1] == 4
    finally:
        conn.close()


def test_save_analysis_commit_false_really_defers_commit(tmp_path):
    """批量写入必须真的由调用方统一提交，不能被 SAVEPOINT 提前提交。"""
    path = tmp_path / "defer.db"
    db.init_db(path)
    writer = db.connect(path)
    reader = db.connect(path)
    try:
        db.save_analysis(
            writer,
            article_id="a1",
            source="s",
            url="u",
            title="t",
            published_at=None,
            published_at_utc=None,
            content_length=0,
            tag_engine="",
            extract_engine="",
            tags=[],
            extractions=[],
            commit=False,
        )
        assert reader.execute("SELECT COUNT(*) FROM analyzed_articles").fetchone()[0] == 0
        writer.commit()
        assert reader.execute("SELECT COUNT(*) FROM analyzed_articles").fetchone()[0] == 1
    finally:
        writer.close()
        reader.close()
