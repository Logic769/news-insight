"""受控行业分类：词表、规则/LLM 分类、落库与回填、接口过滤。"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import backfill_industries as bi
from app import db
from app.llm import LLMError
from app.main import create_app
from app.settings import load_settings
from app.tagging import (
    FALLBACK_INDUSTRY,
    classify_industry,
    industry_with_llm,
    industry_with_rules,
    load_industries,
)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA_SQL)
    return conn


def _seed(
    conn: sqlite3.Connection,
    article_id: str,
    *,
    title: str,
    body: str = "",
    industry: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO analyzed_articles"
        " (article_id, source, url, title, published_at, content_length,"
        "  body, category, category_confidence, category_engine, industry,"
        "  tag_engine, extract_engine, analyzed_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            article_id,
            "svc",
            f"https://example.com/{article_id}",
            title,
            "2026-11-01 00:00:00",
            len(body),
            body,
            "other",
            0.9,
            "rules",
            industry,
            "rules",
            "rules",
            "2026-11-02 00:00:00",
        ),
    )
    conn.commit()


class _FakeLLM:
    def __init__(self, payload):
        self.payload = payload
        self.available = True

    def chat_json(self, *_args, **_kwargs):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


def test_load_industries_returns_ten_controlled_options(tmp_path):
    payload, industries = load_industries(load_settings().vocab_dir)
    assert payload["version"] == 1
    assert len(industries) == 10
    ids = [item.id for item in industries]
    assert FALLBACK_INDUSTRY in ids
    assert len(set(ids)) == 10

    _, missing = load_industries(tmp_path / "empty")
    assert missing == []


def test_industry_with_rules_prefers_title_and_falls_back_to_other():
    _, industries = load_industries(load_settings().vocab_dir)
    hit = industry_with_rules(industries, "关于光伏储能项目申报的通知", "", body_chars=2000)
    assert hit.industry_id == "new_energy"
    assert hit.engine == "rules"
    assert 0.3 < hit.confidence <= 0.95

    fallback = industry_with_rules(industries, "今天天气不错", "没有行业词", body_chars=2000)
    assert fallback.industry_id == FALLBACK_INDUSTRY
    assert fallback.confidence == 0.0


def test_industry_with_llm_drops_out_of_vocabulary_id():
    _, industries = load_industries(load_settings().vocab_dir)
    hit = industry_with_llm(
        _FakeLLM({"industry": "aerospace", "confidence": 0.9, "evidence": "火箭"}),
        industries,
        "某型号火箭首飞",
        "",
        body_chars=100,
    )
    assert hit.industry_id == FALLBACK_INDUSTRY
    assert hit.confidence == 0.0

    good = industry_with_llm(
        _FakeLLM({"industry": "new_materials", "confidence": 1.7, "evidence": "碳纤维"}),
        industries,
        "碳纤维材料量产",
        "",
        body_chars=100,
    )
    assert good.industry_id == "new_materials"
    assert good.confidence == 1.0


def test_classify_industry_falls_back_to_rules_when_llm_fails():
    settings = load_settings()
    _, industries = load_industries(settings.vocab_dir)
    hit = classify_industry(
        settings, _FakeLLM(LLMError("boom")), industries, "生物医药产业园开园", ""
    )
    assert hit.industry_id == "biomedicine"
    assert hit.engine == "rules"


def test_save_analysis_persists_industry_and_set_industry_does_not_overwrite():
    conn = _conn()
    db.save_analysis(
        conn,
        article_id="a1",
        source="svc",
        url="https://example.com/a1",
        title="光伏项目",
        published_at="2026-11-01 00:00:00",
        published_at_utc="2026-10-31 16:00:00",
        content_length=4,
        tag_engine="rules",
        extract_engine="rules",
        tags=(),
        extractions=(),
        category="other",
        category_confidence=0.9,
        category_engine="rules",
        industry="new_energy",
        industry_confidence=0.8,
        industry_engine="rules",
    )
    row = db.get_article(conn, "a1")
    assert row["industry"] == "new_energy"
    assert row["industry_confidence"] == 0.8
    assert row["industry_engine"] == "rules"

    assert db.set_industry(conn, article_id="a1", industry="other", confidence=0.0, engine="rules") is False
    assert db.get_article(conn, "a1")["industry"] == "new_energy"

    assert db.set_industry(conn, article_id="a2", industry="other", confidence=0.0, engine="rules") is False


def test_backfill_industries_fills_only_missing_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_API_KEY", "")
    settings = load_settings()
    db.init_db(settings.db_path)
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    _seed(conn, "new", title="光伏储能项目并网")
    _seed(conn, "kept", title="无关新闻", industry="modern_services")
    conn.close()

    result = bi.backfill_industries(settings, limit=10, workers=2)
    assert result["scanned"] == 1
    assert result["analyzed"] == 1
    assert result["errors"] == 0
    assert result["remaining"] == 0

    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    assert conn.execute("SELECT industry FROM analyzed_articles WHERE article_id='new'").fetchone()[0] == "new_energy"
    assert conn.execute("SELECT industry FROM analyzed_articles WHERE article_id='kept'").fetchone()[0] == "modern_services"
    conn.close()


def test_backfill_industries_rejects_negative_limit():
    settings = load_settings()
    with pytest.raises(ValueError):
        bi.backfill_industries(settings, limit=-1)


def test_search_grouped_filters_by_industry():
    conn = _conn()
    _seed(conn, "energy", title="光伏项目", industry="new_energy")
    _seed(conn, "agri", title="光伏大棚", industry="modern_agriculture")
    out = db.search_grouped(conn, raw_query="光伏", tags=[], industries=["new_energy"])
    ids = [item["article_id"] for group in out["groups"] for item in group["items"]]
    assert ids == ["energy"]

    empty = db.search_grouped(conn, raw_query="光伏", tags=[], industries=[])
    assert empty["total"] == 0


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NEWS_SOURCE_MODE", "demo")
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.setenv("INSIGHT_API_KEY", "secret")
    app = create_app(load_settings())
    with TestClient(app) as c:
        c.post("/v1/analyze", params={"limit": 5}, headers={"X-API-Key": "secret"})
        yield c


HEADERS = {"X-API-Key": "secret"}


def test_industries_endpoint_lists_counts(client):
    resp = client.get("/v1/industries", headers=HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["meta"]["total"] == 10
    assert len(body["data"]) == 10
    assert sum(item["article_count"] for item in body["data"]) == 5


def test_articles_filter_by_industry(client):
    listed = client.get("/v1/industries", headers=HEADERS).json()["data"]
    target = next(item for item in listed if item["article_count"] > 0)
    resp = client.get("/v1/articles", params={"industry": target["id"]}, headers=HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["meta"]["total"] == target["article_count"]
    assert all(item["industry"] == target["id"] for item in body["data"])
