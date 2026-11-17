"""统一响应信封：/v1/* 返回 {code, msg, data}，/health 与文档保持原样。"""

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.settings import load_settings
from app.wire import error_code


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NEWS_SOURCE_MODE", "demo")
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.setenv("INSIGHT_API_KEY", "secret")
    app = create_app(load_settings())
    with TestClient(app) as c:
        yield c


HEADERS = {"X-API-Key": "secret"}


def test_success_is_wrapped_with_code_msg_data(client):
    resp = client.get("/v1/schemas", headers=HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["msg"] == "ok"
    assert isinstance(body["data"], list)
    assert "error" not in body


def test_pagination_meta_is_kept_on_top_level(client):
    resp = client.get("/v1/articles", params={"limit": 1}, headers=HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["data"] == []
    assert body["meta"]["limit"] == 1
    assert body["meta"]["total"] == 0


def test_auth_failure_uses_business_code(client):
    resp = client.get("/v1/schemas")
    assert resp.status_code == 401
    body = resp.json()
    assert body == {"code": 40101, "msg": "未登录或登录已过期", "data": None}


def test_validation_error_keeps_details(client):
    resp = client.get("/v1/articles", params={"limit": "abc"}, headers=HEADERS)
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == 42201
    assert body["data"] is None
    assert body["details"]


def test_not_found_codes_distinguish_article_and_checklist(client):
    assert client.post("/v1/analyze", params={"limit": 4}, headers=HEADERS).status_code == 200

    no_checklist = client.get("/v1/articles/c3a2e5f4d9b06c73/checklist", headers=HEADERS)
    assert no_checklist.status_code == 404
    assert no_checklist.json()["code"] == 40402

    missing = client.get("/v1/articles/notexist/checklist", headers=HEADERS)
    assert missing.status_code == 404
    assert missing.json()["code"] == 40401

    not_applicable = client.post("/v1/articles/c3a2e5f4d9b06c73/checklist", headers=HEADERS)
    assert not_applicable.status_code == 409
    assert not_applicable.json()["code"] == 40902


def test_unmatched_route_and_method_are_error_envelopes(client):
    missing = client.get("/v1/does-not-exist", headers=HEADERS)
    assert missing.status_code == 404
    assert missing.json() == {
        "code": 40400,
        "msg": "Not Found",
        "data": None,
    }

    method = client.post("/v1/schemas", headers=HEADERS)
    assert method.status_code == 405
    assert method.json()["code"] == 40501
    assert method.json()["data"] is None


def test_unhandled_exception_is_a_json_error_envelope(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NEWS_SOURCE_MODE", "demo")
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.setenv("INSIGHT_API_KEY", "secret")
    app = create_app(load_settings())

    @app.get("/v1/_boom")
    def boom():
        raise RuntimeError("probe")

    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.get("/v1/_boom", headers=HEADERS)
    assert resp.status_code == 500
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json() == {
        "code": 50001,
        "msg": "服务器内部错误",
        "data": None,
    }


def test_lock_error_has_its_own_business_code():
    assert error_code(423) == 42301


def test_health_and_docs_are_not_wrapped(client):
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert "code" not in health.json()

    docs = client.get("/docs")
    assert docs.status_code == 200
    assert "text/html" in docs.headers["content-type"]


def test_openapi_documents_envelope(client):
    schema = client.get("/openapi.json").json()
    operation = schema["paths"]["/v1/schemas"]["get"]["responses"]["200"]
    envelope = operation["content"]["application/json"]["schema"]
    assert set(envelope["required"]) == {"code", "msg", "data"}
    assert "ApiError" in schema["components"]["schemas"]

    # 同一进程内第二次访问不能把 data schema 再包一层
    assert client.get("/openapi.json").json() == schema
