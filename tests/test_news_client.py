"""NewsClient 的请求参数与 auto 回退语义（不联网）。"""

from __future__ import annotations

from dataclasses import replace

import pytest

from app import news_client as nc
from app.news_client import NewsClient, UpstreamError
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

    def get(self, url, params=None, headers=None):
        _FakeHttpxClient.last_params = params
        return _FakeResponse()


def test_live_list_uses_published_desc(settings, monkeypatch):
    """上游 /v1/articles 只接受 published_desc/published_asc/updated_desc。"""
    monkeypatch.setattr(nc.httpx, "Client", _FakeHttpxClient)
    rows, mode = NewsClient(settings).list_articles(limit=5)
    assert rows == [] and mode == "live"
    assert _FakeHttpxClient.last_params["order"] == "published_desc"


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
    assert mode == "demo" and len(rows) == 4
    assert client.fallback_reason == "连接超时"
