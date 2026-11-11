"""HTTP 面：入站鉴权与错误格式。"""

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.settings import load_settings


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NEWS_SOURCE_MODE", "demo")
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.setenv("INSIGHT_API_KEY", "secret")
    app = create_app(load_settings())
    with TestClient(app) as c:
        yield c


def test_health_is_open(client):
    assert client.get("/health").status_code == 200


def test_business_endpoints_require_key(client):
    assert client.get("/v1/schemas").status_code == 401
    assert client.post("/v1/analyze", params={"limit": 1}).status_code == 401


def test_api_key_headers_accepted(client):
    assert client.get("/v1/schemas", headers={"X-API-Key": "secret"}).status_code == 200
    assert (
        client.get("/v1/schemas", headers={"Authorization": "Bearer secret"}).status_code == 200
    )


def test_wrong_key_rejected(client):
    assert client.get("/v1/schemas", headers={"X-API-Key": "nope"}).status_code == 401


def test_analyze_limit_capped(client):
    resp = client.post(
        "/v1/analyze", params={"limit": 500}, headers={"X-API-Key": "secret"}
    )
    assert resp.status_code == 422
    body = resp.json()["error"]
    assert body["code"] == 422 and body["details"]
