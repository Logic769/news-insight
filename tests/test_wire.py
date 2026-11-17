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
    # 宿主机若配置了 NO_PROXY=[::1]，httpx 0.28 会把它解析成非法端口；
    # 这套用例只打 TestClient，不需要任何代理变量。
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                 "http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
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


def test_pre_wrapped_error_envelope_is_not_overwritten(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NEWS_SOURCE_MODE", "demo")
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.setenv("INSIGHT_API_KEY", "secret")
    app = create_app(load_settings())

    @app.get("/v1/_wrapped_404")
    def wrapped_404():
        from starlette.responses import JSONResponse

        return JSONResponse(
            status_code=404,
            content={"code": 40402, "msg": "清单不存在", "data": None},
        )

    with TestClient(app) as c:
        resp = c.get("/v1/_wrapped_404", headers=HEADERS)
    assert resp.status_code == 404
    assert resp.json() == {"code": 40402, "msg": "清单不存在", "data": None}


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
    # /v1/schemas 的 data 是数组；裸 dict 返回注解不能被文档化成 object。
    assert envelope["properties"]["data"] == {}

    api_error = schema["components"]["schemas"]["ApiError"]
    assert "details" in api_error["properties"]
    assert "nullable" not in api_error["properties"]["data"]

    # FastAPI 预置的 422 必须被统一错误 schema 替换。
    validation = schema["paths"]["/v1/articles"]["get"]["responses"]["422"]
    assert validation["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ApiError"
    }

    # 同一进程内第二次访问不能把 data schema 再包一层
    assert client.get("/openapi.json").json() == schema


def test_all_v1_operations_have_envelope_and_api_error(client):
    schema = client.get("/openapi.json").json()
    operation_count = 0
    for path, methods in schema["paths"].items():
        if not path.startswith("/v1/"):
            continue
        for method, operation in methods.items():
            if method.lower() not in {"get", "post", "put", "patch", "delete"}:
                continue
            operation_count += 1
            responses = operation["responses"]
            for status, response in responses.items():
                if not status.startswith("2"):
                    continue
                envelope = response["content"]["application/json"]["schema"]
                assert set(envelope["required"]) == {"code", "msg", "data"}
                # 当前 /v1 路由都返回裸 dict，无法表达 data 结构时必须留空，
                # 不能被 additionalProperties 的整响应 schema 冒充。
                assert envelope["properties"]["data"] == {}
            assert responses["422"]["content"]["application/json"]["schema"] == {
                "$ref": "#/components/schemas/ApiError"
            }
    assert operation_count >= 50


def test_custom_error_response_metadata_is_preserved(tmp_path, monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                 "http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NEWS_SOURCE_MODE", "demo")
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.setenv("INSIGHT_API_KEY", "secret")
    app = create_app(load_settings())

    @app.get(
        "/v1/_custom_400",
        responses={
            400: {
                "description": "自定义 400",
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "properties": {"reason": {"type": "string"}},
                        }
                    }
                },
            }
        },
    )
    def custom_400():
        return {"ok": True}

    @app.get("/v1/_custom_401", responses={401: {"description": "自定义 401"}})
    def custom_401():
        return {"ok": True}

    with TestClient(app) as c:
        schema = c.get("/openapi.json").json()
    responses = schema["paths"]["/v1/_custom_400"]["get"]["responses"]
    assert responses["400"]["description"] == "自定义 400"
    assert responses["400"]["content"]["application/json"]["schema"]["properties"]["reason"] == {
        "type": "string"
    }
    assert schema["paths"]["/v1/_custom_401"]["get"]["responses"]["401"]["description"] == (
        "自定义 401"
    )
