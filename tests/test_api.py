"""HTTP 面：入站鉴权与错误格式。"""

import time

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


def test_checklist_endpoint(client):
    headers = {"X-API-Key": "secret"}
    assert client.post("/v1/analyze", params={"limit": 4}, headers=headers).status_code == 200

    resp = client.get("/v1/articles/a1f0c3e2b7d94a51/checklist", headers=headers)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["article_id"] == "a1f0c3e2b7d94a51"
    assert data["engine"] == "rules"
    assert any("2026-04-20" in item["text"] for item in data["items"])

    # 非申报文章没有清单，返回 404 而不是空数组；两种 404 用不同错误码区分
    no_checklist = client.get("/v1/articles/c3a2e5f4d9b06c73/checklist", headers=headers)
    assert no_checklist.status_code == 404
    assert no_checklist.json()["error"]["code"] == "checklist_not_found"
    missing = client.get("/v1/articles/notexist/checklist", headers=headers)
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "article_not_found"


def test_categories_listing_and_filter(client):
    """/v1/categories 要把 6 个大类连同一句中文名一起返回，且统计要能对上过滤结果。"""
    headers = {"X-API-Key": "secret"}
    assert client.post("/v1/analyze", params={"limit": 4}, headers=headers).status_code == 200

    resp = client.get("/v1/categories", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    cats = {c["id"]: c for c in body["data"]}
    assert list(cats) == [
        "project_application",
        "industry_policy",
        "talent_application",
        "enterprise_certification",
        "funding_subsidy",
        "other",
    ]
    assert cats["project_application"]["name"] == "项目申报"
    assert cats["other"]["tags"] == [], "兜底类不打二级标签"
    assert body["meta"]["vocab_version"] >= 1

    counts = {c["category"]: c["count"] for c in client.get("/v1/categories/stats", headers=headers).json()["data"]}
    assert counts["project_application"] == 2, "示例数据里有两篇申报通知"
    assert cats["project_application"]["article_count"] == 2

    filtered = client.get("/v1/articles", params={"category": "project_application"}, headers=headers).json()
    assert filtered["meta"]["total"] == 2
    assert {a["category"] for a in filtered["data"]} == {"project_application"}


def test_category_filter_on_tags_endpoint(client):
    """tag_stats 带 category 时只统计该类下的标签，否则跨类标签会虚高。"""
    headers = {"X-API-Key": "secret"}
    client.post("/v1/analyze", params={"limit": 4}, headers=headers)

    scoped = client.get("/v1/tags", params={"category": "other"}, headers=headers).json()["data"]
    finance = next(t for t in scoped if t["id"] == "finance")
    assert finance["article_count"] == 1, "央行降准那篇被归到 other，规则引擎仍打了 finance"

    unscoped = client.get("/v1/tags", headers=headers).json()["data"]
    assert next(t for t in unscoped if t["id"] == "finance")["article_count"] == 1


def test_analyze_limit_capped(client):
    resp = client.post(
        "/v1/analyze", params={"limit": 500}, headers={"X-API-Key": "secret"}
    )
    assert resp.status_code == 422
    body = resp.json()["error"]
    assert body["code"] == 422 and body["details"]


def test_async_job_lifecycle(client):
    headers = {"X-API-Key": "secret"}
    resp = client.post("/v1/jobs", json={"limit": 4}, headers=headers)
    assert resp.status_code == 202
    job = resp.json()["data"]
    assert job["status"] in ("queued", "running", "succeeded")
    job_id = job["id"]

    deadline = time.time() + 10
    while time.time() < deadline:
        current = client.get(f"/v1/jobs/{job_id}", headers=headers).json()["data"]
        if current["status"] in ("succeeded", "failed"):
            break
        time.sleep(0.05)
    assert current["status"] == "succeeded", current
    assert current["result"]["analyzed"] == 4

    listing = client.get("/v1/jobs", headers=headers).json()["data"]
    assert any(item["id"] == job_id for item in listing)
