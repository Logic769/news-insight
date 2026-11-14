"""HTTP 面：入站鉴权与错误格式。"""

import time

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.main import ANALYZE_LOCK, create_app
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


def test_checklist_generation_is_idempotent_and_force_regenerates(client):
    headers = {"X-API-Key": "secret"}
    assert client.post("/v1/analyze", params={"limit": 4}, headers=headers).status_code == 200

    first = client.get("/v1/articles/a1f0c3e2b7d94a51/checklist", headers=headers)
    assert first.status_code == 200
    first_data = first.json()["data"]

    # 默认幂等：已有清单直接复用，不重新生成
    reused = client.post("/v1/articles/a1f0c3e2b7d94a51/checklist", headers=headers)
    assert reused.status_code == 200
    assert reused.json()["data"]["generated_at"] == first_data["generated_at"]
    assert reused.json()["data"]["items"] == first_data["items"]

    # force=true 才重新生成，并覆盖 generated_at
    time.sleep(1.05)
    regenerated = client.post(
        "/v1/articles/a1f0c3e2b7d94a51/checklist",
        json={"force": True},
        headers=headers,
    )
    assert regenerated.status_code == 200
    regenerated_data = regenerated.json()["data"]
    assert regenerated_data["generated_at"] != first_data["generated_at"]
    assert regenerated_data["engine"] == "rules"
    assert any("2026-04-20" in item["text"] for item in regenerated_data["items"])


def test_checklist_generation_rejects_inapplicable_and_missing_articles(client):
    headers = {"X-API-Key": "secret"}
    assert client.post("/v1/analyze", params={"limit": 4}, headers=headers).status_code == 200

    not_applicable = client.post("/v1/articles/c3a2e5f4d9b06c73/checklist", headers=headers)
    assert not_applicable.status_code == 409
    assert not_applicable.json()["error"]["code"] == "checklist_not_applicable"

    missing = client.post("/v1/articles/notexist/checklist", headers=headers)
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "article_not_found"


def test_checklist_generation_returns_409_when_another_job_running(client):
    headers = {"X-API-Key": "secret"}
    assert client.post("/v1/analyze", params={"limit": 4}, headers=headers).status_code == 200

    assert ANALYZE_LOCK.acquire(blocking=False)
    try:
        blocked = client.post(
            "/v1/articles/a1f0c3e2b7d94a51/checklist", json={"force": True}, headers=headers
        )
        assert blocked.status_code == 409
        assert "已有分析或清单生成任务在运行" in blocked.json()["error"]["message"]
    finally:
        ANALYZE_LOCK.release()


def test_checklist_generation_rechecks_existing_after_acquiring_lock(client, monkeypatch):
    """锁前读到 None 不代表锁后仍为空：拿到锁后必须再查一次，避免重复调用 LLM。"""
    headers = {"X-API-Key": "secret"}
    assert client.post("/v1/analyze", params={"limit": 4}, headers=headers).status_code == 200

    original_get_checklist = main.db.get_checklist
    calls = {"count": 0}

    def stale_first_read(conn, article_id):
        calls["count"] += 1
        if calls["count"] == 1:
            return None
        return original_get_checklist(conn, article_id)

    def fail_if_regenerated(*args, **kwargs):
        raise AssertionError("锁内复查已有清单后不应再调用生成器")

    monkeypatch.setattr(main.db, "get_checklist", stale_first_read)
    monkeypatch.setattr(main.checklist_service, "build_for_input", fail_if_regenerated)

    resp = client.post("/v1/articles/a1f0c3e2b7d94a51/checklist", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["data"]["article_id"] == "a1f0c3e2b7d94a51"
    assert calls["count"] >= 2


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


def test_empty_category_filter_returns_empty_not_everything(client):
    """category= 空串是「筛一个不存在的类」，不能静默退化成不过滤。"""
    headers = {"X-API-Key": "secret"}
    client.post("/v1/analyze", params={"limit": 4}, headers=headers)

    resp = client.get("/v1/articles", params={"category": ""}, headers=headers).json()
    assert resp["data"] == []
    assert resp["meta"]["total"] == 0

    # /v1/tags 返回词表本身，空串下应当是「词表照旧、计数全 0」
    tags = client.get("/v1/tags", params={"category": ""}, headers=headers).json()["data"]
    assert tags and {t["article_count"] for t in tags} == {0}


def test_articles_category_and_industry_accept_multi(client):
    """列表端的 category/industry 与检索端一样支持重复/逗号多选。"""
    headers = {"X-API-Key": "secret"}
    client.post("/v1/analyze", params={"limit": 4}, headers=headers)

    both = client.get(
        "/v1/articles", params={"category": "project_application,other"}, headers=headers
    ).json()
    assert both["meta"]["total"] >= 2
    assert {row["category"] for row in both["data"]} <= {"project_application", "other"}

    # industry= 空串必须与 category= 空串同语义：筛不到任何行，而不是命中未分类行
    assert client.get("/v1/articles", params={"industry": ""}, headers=headers).json()[
        "meta"
    ]["total"] == 0


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

# ------------------------------------------------- 新增：列表过滤/排序/统计端点

def test_articles_rejects_unknown_sort(client):
    resp = client.get("/v1/articles", params={"sort": "nope"}, headers={"X-API-Key": "secret"})
    assert resp.status_code == 422


def test_articles_rejects_bad_date(client):
    resp = client.get(
        "/v1/articles", params={"published_from": "2026/03/01"}, headers={"X-API-Key": "secret"}
    )
    assert resp.status_code == 422


def test_articles_time_filter_reports_undated_excluded(client):
    """带时间过滤时 meta 必须回显 undated_excluded，前端才能解释 total 为什么变小。"""
    headers = {"X-API-Key": "secret"}
    assert client.post("/v1/analyze", params={"limit": 4}, headers=headers).status_code == 200
    resp = client.get(
        "/v1/articles",
        params={"published_from": "2026-03-01", "published_to": "2026-03-31"},
        headers=headers,
    )
    assert resp.status_code == 200
    meta = resp.json()["meta"]
    assert "undated_excluded" in meta
    assert meta["undated_excluded"] == 0


def test_articles_sort_by_confidence_puts_nulls_last(client):
    """缺分类置信度的历史行不能被当成「置信度最低」排到最前面。"""
    headers = {"X-API-Key": "secret"}
    assert client.post("/v1/analyze", params={"limit": 4}, headers=headers).status_code == 200
    resp = client.get("/v1/articles", params={"sort": "confidence_desc"}, headers=headers)
    rows = resp.json()["data"]
    assert rows
    seen_null = False
    for row in rows:
        if row.get("category_confidence") is None:
            seen_null = True
        else:
            assert not seen_null, "置信度为 NULL 的行必须排在最后"


def test_sources_stats_endpoint(client):
    headers = {"X-API-Key": "secret"}
    assert client.post("/v1/analyze", params={"limit": 4}, headers=headers).status_code == 200
    resp = client.get("/v1/sources", headers=headers)
    assert resp.status_code == 200
    rows = resp.json()["data"]
    assert rows
    total = client.get("/v1/articles", params={"limit": 1}, headers=headers).json()["meta"]["total"]
    assert sum(r["count"] for r in rows) == total
    for row in rows:
        assert set(row) == {
            "source",
            "source_filter",
            "count",
            "avg_confidence",
            "latest_published_at",
        }
        assert row["source_filter"], "source_filter 必须非空，空串无法作为筛选值"
        assert row["source_filter"] == (row["source"] or "__unlabeled__")


def test_sources_stats_requires_key(client):
    assert client.get("/v1/sources").status_code == 401
