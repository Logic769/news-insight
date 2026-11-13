"""GET /v1/search：后端算好排序、按 5 大类分组返回给前端。

前端的职责只是渲染，因此这里断言的是「后端给出的结构与顺序」：分组键、
类内顺序、空结果的形状、以及默认不返回正文（正文很大，只有显式要求时才给）。
"""

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
        # 先分析一批，让库里有数据可搜
        c.post("/v1/analyze", params={"limit": 4}, headers={"X-API-Key": "secret"})
        yield c


HEADERS = {"X-API-Key": "secret"}


def test_search_requires_key(client):
    assert client.get("/v1/search", params={"q": "申报"}).status_code == 401


def test_search_returns_groups(client):
    resp = client.get("/v1/search", params={"q": "申报"}, headers=HEADERS)
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["query"] == "申报"
    assert isinstance(body["groups"], list)
    assert body["total"] >= 1
    for group in body["groups"]:
        assert set(group) == {"category", "count", "items"}
        assert group["count"] >= len(group["items"])


def test_search_groups_keep_rules_order(client):
    """固定类顺序来自 categories.json，前端的五段布局才能稳定。"""
    resp = client.get("/v1/search", params={"q": "申报"}, headers=HEADERS)
    cats = [g["category"] for g in resp.json()["data"]["groups"]]
    canonical = [
        "project_application",
        "industry_policy",
        "talent_application",
        "enterprise_certification",
        "funding_subsidy",
        "other",
    ]
    positions = [canonical.index(c) for c in cats if c in canonical]
    assert positions == sorted(positions)


def test_search_ranks_descending_inside_group(client):
    resp = client.get("/v1/search", params={"q": "申报"}, headers=HEADERS)
    for group in resp.json()["data"]["groups"]:
        scores = [i["relevance"]["score"] for i in group["items"]]
        assert scores == sorted(scores, reverse=True)


def test_search_unknown_word_returns_empty_groups(client):
    resp = client.get("/v1/search", params={"q": "zzz不存在"}, headers=HEADERS)
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["groups"] == []
    assert body["total"] == 0


def test_search_rejects_empty_query(client):
    assert client.get("/v1/search", params={"q": ""}, headers=HEADERS).status_code == 422


def test_search_hides_body_by_default(client):
    resp = client.get("/v1/search", params={"q": "申报"}, headers=HEADERS)
    for group in resp.json()["data"]["groups"]:
        for item in group["items"]:
            assert "body" not in item


def test_search_can_include_body(client):
    resp = client.get(
        "/v1/search",
        params={"q": "申报", "include_body": "true"},
        headers=HEADERS,
    )
    items = [i for g in resp.json()["data"]["groups"] for i in g["items"]]
    assert items
    assert any(i.get("body") for i in items)


def test_search_can_filter_categories(client):
    resp = client.get(
        "/v1/search",
        params={"q": "申报", "category": "project_application"},
        headers=HEADERS,
    )
    data = resp.json()["data"]
    assert data["total"] > 0
    assert {g["category"] for g in data["groups"]} == {"project_application"}


def test_search_empty_category_param_is_treated_as_absent(client):
    """`?category=` 传空串等同于不传，而不是「只筛历史 NULL 行」。

    空串若直接透传，SQL 会变成 COALESCE(category,'') IN ('')，只命中分类功能
    上线前的历史行；即便在 Python 侧过滤掉空串，剩下的空列表也会被
    search_candidates 当成「筛完没结果」直接返回空集。两种走法都会让用户只是
    清空了筛选框却看不到任何结果，所以这里必须显式归一成「不传」。
    """
    base = client.get("/v1/search", params={"q": "申报"}, headers=HEADERS).json()["data"]
    blank = client.get("/v1/search", params={"q": "申报", "category": ""}, headers=HEADERS)
    assert blank.status_code == 200
    body = blank.json()["data"]
    assert body["total"] == base["total"]
    assert [g["category"] for g in body["groups"]] == [g["category"] for g in base["groups"]]


def test_search_blank_and_real_category_filter_are_and_combined(client):
    """空串与真实类名混传时，只有真实类名生效。"""
    resp = client.get(
        "/v1/search",
        params={"q": "申报", "category": ["", "project_application"]},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total"] > 0
    assert {g["category"] for g in data["groups"]} == {"project_application"}


def test_search_rejects_whitespace_only_query(client):
    """纯空白与空串一样该在参数校验阶段被拒，避免同一件事两种响应码。"""
    assert client.get("/v1/search", params={"q": " "}, headers=HEADERS).status_code == 422
    assert client.get("/v1/search", params={"q": "   "}, headers=HEADERS).status_code == 422
    # 带真实内容的查询不受影响
    assert client.get("/v1/search", params={"q": " 申报 "}, headers=HEADERS).status_code == 200
