"""backfill-relevance：只补语义评分三列，不重跑其它派生字段。"""

from __future__ import annotations

import sqlite3

import pytest

from app import db, search
from app.backfill_relevance import _missing_relevance, backfill_relevance
from app.settings import load_settings


@pytest.fixture()
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NEWS_SOURCE_MODE", "demo")
    monkeypatch.setenv("LLM_API_KEY", "")
    return load_settings()


def _save(conn, article_id: str, *, title: str, category: str, score=None) -> None:
    db.save_analysis(
        conn,
        article_id=article_id,
        source="svc",
        url=f"https://example.com/{article_id}",
        title=title,
        published_at="2026-03-01 09:00:00",
        published_at_utc="2026-03-01 01:00:00",
        content_length=120,
        tag_engine="rules",
        extract_engine="rules",
        tags=[],
        extractions=[],
        category=category,
        category_confidence=0.9,
        category_engine="rules",
        relevance_score=score,
        relevance_engine="rules" if score is not None else "",
        relevance_reason="已有评分" if score is not None else "",
        body="申报条件与截止时间说明",
    )


def test_backfill_only_fills_missing_rows(settings):
    db.init_db(settings.db_path)
    conn = db.connect(settings.db_path)
    try:
        _save(conn, "done", title="已有评分文章", category="project_application", score=0.42)
        _save(conn, "todo", title="专精特新中小企业申报通知", category="project_application")
        assert [r["article_id"] for r in _missing_relevance(conn, 10)] == ["todo"]
    finally:
        conn.close()

    result = backfill_relevance(settings, limit=10, workers=1)
    assert result["scanned"] == 1 and result["analyzed"] == 1
    assert result["errors"] == 0 and result["remaining"] == 0
    assert result["engines"] == {"rules": 1}

    conn = db.connect(settings.db_path)
    try:
        done = db.get_article(conn, "done")
        todo = db.get_article(conn, "todo")
        assert done["relevance_score"] == pytest.approx(0.42)
        assert done["relevance_reason"] == "已有评分"
        assert todo["relevance_score"] is not None
        assert todo["relevance_engine"] == "rules"
        assert "分类先验" in todo["relevance_reason"]
    finally:
        conn.close()

    second = backfill_relevance(settings, limit=10, workers=1)
    assert second["scanned"] == 0 and second["remaining"] == 0


def test_backfill_rejects_negative_limit(settings):
    with pytest.raises(ValueError):
        backfill_relevance(settings, limit=-1)


def test_save_analysis_none_does_not_clear_existing_relevance(settings):
    """重复分析未拿到分数（None）时，不能把已有语义分冲成 NULL。"""
    db.init_db(settings.db_path)
    conn = db.connect(settings.db_path)
    try:
        _save(conn, "done", title="已有评分文章", category="project_application", score=0.42)
        _save(conn, "done", title="已有评分文章", category="project_application", score=None)
        row = db.get_article(conn, "done")
        assert row["relevance_score"] == pytest.approx(0.42)
        assert row["relevance_reason"] == "已有评分"
    finally:
        conn.close()


def test_init_db_backfills_norms_for_legacy_rows(tmp_path):
    """旧库没有 norm 列时，init_db 迁移后必须回填 title_norm/body_norm。"""
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE analyzed_articles ("
            " article_id TEXT PRIMARY KEY, source TEXT NOT NULL, url TEXT NOT NULL,"
            " title TEXT NOT NULL, published_at TEXT, published_at_utc TEXT,"
            " content_length INTEGER NOT NULL DEFAULT 0, body TEXT,"
            " tag_engine TEXT, extract_engine TEXT, analyzed_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO analyzed_articles"
            " (article_id, source, url, title, body, content_length, tag_engine,"
            "  extract_engine, analyzed_at)"
            " VALUES ('old', 'svc', 'https://example.com/old', ' 旧标题 ', ' 正文内容 ', 4,"
            "         'rules', 'rules', '2026-01-01 00:00:00')"
        )
        conn.commit()
    finally:
        conn.close()

    db.init_db(path)

    conn = db.connect(path)
    try:
        row = conn.execute(
            "SELECT title_norm, body_norm FROM analyzed_articles WHERE article_id = 'old'"
        ).fetchone()
        assert row["title_norm"] == search._norm(" 旧标题 ")
        assert row["body_norm"] == search._norm(" 正文内容 ")
    finally:
        conn.close()
