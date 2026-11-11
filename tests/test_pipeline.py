"""End-to-end run against the bundled fixtures (no network, no LLM)."""

from __future__ import annotations

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


def test_demo_batch_tags_and_extracts(settings):
    result = analyze_batch(settings, limit=4)
    assert result["mode"] == "demo"
    assert result["analyzed"] == 4 and result["errors"] == 0

    conn = db.connect(settings.db_path)
    try:
        # 绝对日期：2026年4月20日
        row = db.get_article(conn, "a1f0c3e2b7d94a51")
        assert row is not None
        assert row["extractions"]["project_application"]["deadline"]["value"] == "2026-04-20"
        assert row["extractions"]["project_application"]["funding_amount"]["value"] == "500万元"

        # 相对日期：自发布之日起30日内，基准取 published_at_utc=2026-03-05
        row2 = db.get_article(conn, "b2e1d4f3c8a95b62")
        assert row2["extractions"]["project_application"]["deadline"]["value"] == "2026-04-04"

        # 非申报文章不触发抽取
        row3 = db.get_article(conn, "c3a2e5f4d9b06c73")
        assert "project_application" not in row3["extractions"]
        assert {t["tag"] for t in row3["tags"]} & {"finance", "policy_release"}

        # 截止时间查询
        rows = db.list_extractions(conn, schema_id="project_application", field=None,
                                   due_before="2026-04-10", limit=10)
        assert [r["article_id"] for r in rows] == ["b2e1d4f3c8a95b62"]
    finally:
        conn.close()


def test_rerun_skips_already_analyzed(settings):
    analyze_batch(settings, limit=4)
    second = analyze_batch(settings, limit=4)
    assert second["analyzed"] == 0 and second["skipped"] == 4

    forced = analyze_batch(settings, limit=4, force=True)
    assert forced["analyzed"] == 4


def test_field_and_due_before_are_orthogonal(settings):
    """field 与 due_before 同时传入时不能变成 e.field=A AND e.field=B 的恒空条件。"""
    analyze_batch(settings, limit=4)
    conn = db.connect(settings.db_path)
    try:
        same = db.list_extractions(
            conn, schema_id="project_application", field="deadline",
            due_before="2026-04-10", limit=10,
        )
        assert [r["article_id"] for r in same] == ["b2e1d4f3c8a95b62"]

        other = db.list_extractions(
            conn, schema_id="project_application", field="materials",
            due_before="2026-04-10", limit=10,
        )
        assert other == []
    finally:
        conn.close()


def test_empty_body_is_not_persisted(settings, monkeypatch):
    """详情没拿到时按失败处理：不入库，next run 才能重试（否则结论被永久定档）。"""
    import app.pipeline as pipeline
    from app.news_client import NewsArticle

    article = NewsArticle(
        id="deadbeefdeadbeef",
        source="demo",
        url="https://example.com/a.html",
        title="只有标题的新闻",
        published_at="2026-03-05 09:30:00",
        published_at_utc="2026-03-05 01:30:00",
        timezone="Asia/Shanghai",
        content="",
        raw_content=None,
        content_length=0,
        updated_at=None,
    )

    class FakeClient:
        def __init__(self, _settings):
            pass

        def list_articles(self, limit, offset=0):
            return [article], "live"

        def hydrate_bodies(self, articles, mode):
            for art in articles:
                art.body_ok = False
            return articles

    monkeypatch.setattr(pipeline, "NewsClient", FakeClient)
    result = pipeline.analyze_batch(settings, limit=1)
    assert result["analyzed"] == 0
    assert result["errors"] == 1

    conn = db.connect(settings.db_path)
    try:
        assert db.is_analyzed(conn, article.id) is False
    finally:
        conn.close()


def test_one_bad_article_does_not_sink_batch(settings, monkeypatch):
    import app.pipeline as pipeline

    real = pipeline.tag_article
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return real(*args, **kwargs)

    monkeypatch.setattr(pipeline, "tag_article", flaky)
    result = pipeline.analyze_batch(settings, limit=4)
    assert result["errors"] == 1
    assert result["analyzed"] == 3
