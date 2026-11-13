"""NewsClient 的请求参数与 auto 回退语义（不联网）。"""

from __future__ import annotations

from dataclasses import replace

import pytest

from app import news_client as nc
from app.news_client import NewsArticle, NewsClient, UpstreamError
from app.settings import load_settings


@pytest.fixture()
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NEWS_BASE_URL", "http://upstream.test")
    monkeypatch.setenv("NEWS_SOURCE_MODE", "live")
    monkeypatch.setenv("LLM_API_KEY", "")
    return load_settings()


class _FakeResponse:
    status_code = 200
    text = ""

    def json(self):
        return {"data": [], "meta": {}}


class _FakeHttpxClient:
    last_params = None

    def __init__(self, timeout=None):
        self.timeout = timeout

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def close(self):
        pass

    def get(self, url, params=None, headers=None):
        _FakeHttpxClient.last_params = params
        return _FakeResponse()


def test_live_list_uses_published_desc(settings, monkeypatch):
    """上游 /v1/articles 只接受 published_desc/published_asc/updated_desc。"""
    monkeypatch.setattr(nc.httpx, "Client", _FakeHttpxClient)
    rows, mode = NewsClient(settings).list_articles(limit=5)
    assert rows == [] and mode == "live"
    assert _FakeHttpxClient.last_params["order"] == "published_desc"


def test_live_list_page_size_never_exceeds_upstream_limit(settings, monkeypatch):
    """limit>100 时不能把 page_size 放大到上游 le=100 之外。"""
    captured = []

    class CaptureClient(_FakeHttpxClient):
        def get(self, url, params=None, headers=None):
            captured.append(params["page_size"])
            return _FakeResponse()

    monkeypatch.setattr(nc.httpx, "Client", CaptureClient)
    NewsClient(settings).list_articles(limit=1000)
    assert captured == [100]


def test_live_mode_raises_upstream_error(settings, monkeypatch):
    client = NewsClient(settings)

    def boom(**kwargs):
        raise UpstreamError("连接失败")

    monkeypatch.setattr(client, "_live_list", boom)
    with pytest.raises(UpstreamError):
        client.list_articles(limit=5)


def test_auto_mode_falls_back_and_records_reason(settings, monkeypatch):
    client = NewsClient(replace(settings, news_source_mode="auto"))

    def boom(**kwargs):
        raise UpstreamError("连接超时")

    monkeypatch.setattr(client, "_live_list", boom)
    rows, mode = client.list_articles(limit=5)
    # 不写死条数：演示夹具会随用例需要增删，这里验证的是「回退成功且不超过 limit」。
    assert mode == "demo"
    assert 0 < len(rows) <= 5
    assert client.fallback_reason == "连接超时"


def test_list_articles_pages_until_limit(settings, monkeypatch):
    client = NewsClient(settings)
    pages = {
        1: {
            "data": [{"id": "a", "source": "s", "url": "u1", "title": "a"}],
            "meta": {"has_next": True, "total_pages": 2},
        },
        2: {
            "data": [{"id": "b", "source": "s", "url": "u2", "title": "b"}],
            "meta": {"has_next": False, "total_pages": 2},
        },
    }

    monkeypatch.setattr(client, "_live_list", lambda **kwargs: pages[kwargs["page"]])
    rows, mode = client.list_articles(limit=2)
    assert mode == "live"
    assert [r.id for r in rows] == ["a", "b"]


def test_bad_total_pages_does_not_crash(settings, monkeypatch):
    client = NewsClient(settings)
    monkeypatch.setattr(
        client,
        "_live_list",
        lambda **kwargs: {
            "data": [{"id": "a", "source": "s", "url": "u1", "title": "a"}],
            "meta": {"has_next": False, "total_pages": "two"},
        },
    )
    rows, mode = client.list_articles(limit=5)
    assert mode == "live" and [r.id for r in rows] == ["a"]


def test_from_payload_bad_content_length_is_tolerated():
    art = NewsArticle.from_payload({"content_length": "not-a-number"})
    assert art.content_length == 0


def test_hydrate_bodies_bad_detail_does_not_raise(settings, monkeypatch):
    client = NewsClient(settings)
    article = NewsArticle(
        id="a", source="s", url="u1", title="a", published_at=None,
        published_at_utc=None, timezone="Asia/Shanghai", content="", raw_content=None,
        content_length=0, updated_at=None,
    )

    def bad_detail(_article_id):
        raise ValueError("not json")

    monkeypatch.setattr(client, "_live_detail", bad_detail)
    out = client.hydrate_bodies([article], "live")
    assert out[0].body_ok is False


def test_auto_mode_does_not_fall_back_on_4xx(settings, monkeypatch):
    client = NewsClient(replace(settings, news_source_mode="auto"))

    def bad_request(**kwargs):
        raise UpstreamError("新闻 API 返回 422", status_code=422, fallbackable=False)

    monkeypatch.setattr(client, "_live_list", bad_request)
    with pytest.raises(UpstreamError):
        client.list_articles(limit=5)


def test_hydrate_bodies_deduplicates_same_article(settings, monkeypatch):
    client = NewsClient(settings)
    article = NewsArticle(
        id="a", source="s", url="u1", title="a", published_at=None,
        published_at_utc=None, timezone="Asia/Shanghai", content="", raw_content=None,
        content_length=0, updated_at=None,
    )
    calls = []
    monkeypatch.setattr(
        client,
        "_live_detail",
        lambda article_id: calls.append(article_id) or {"data": {"content": "body"}},
    )
    out = client.hydrate_bodies([article, article], "live")
    assert [a.id for a in out] == ["a"]
    assert calls == ["a"]


def test_hydrate_bodies_uses_detail_and_keeps_order(settings, monkeypatch):
    client = NewsClient(replace(settings, news_detail_concurrency=2))
    articles = [
        NewsArticle(
            id="a", source="s", url="u1", title="a", published_at=None,
            published_at_utc=None, timezone="Asia/Shanghai", content="", raw_content=None,
            content_length=0, updated_at=None,
        ),
        NewsArticle(
            id="b", source="s", url="u2", title="b", published_at=None,
            published_at_utc=None, timezone="Asia/Shanghai", content="", raw_content=None,
            content_length=0, updated_at=None,
        ),
    ]
    monkeypatch.setattr(
        client,
        "_live_detail",
        lambda article_id: {"data": {"content": f"body-{article_id}", "content_length": 6}},
    )
    out = client.hydrate_bodies(articles, "live")
    assert [a.body for a in out] == ["body-a", "body-b"]
