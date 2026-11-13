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

# ------------------------------------------------------- 新增：时间/来源/排序

def _all_items(data):
    return [i for g in data["groups"] for i in g["items"]]


def test_search_rejects_bad_date_format(client):
    """日期只接受 YYYY-MM-DD：带时分秒的输入语义不明，必须 422 而不是猜。"""
    for bad in ("2026/03/01", "2026-3-1", "20260301", "昨天"):
        resp = client.get(
            "/v1/search",
            params={"q": "申报", "published_from": bad},
            headers=HEADERS,
        )
        assert resp.status_code == 422, bad


def test_search_rejects_unknown_sort(client):
    resp = client.get(
        "/v1/search", params={"q": "申报", "sort": "score"}, headers=HEADERS
    )
    assert resp.status_code == 422


def test_search_echoes_sort(client):
    resp = client.get("/v1/search", params={"q": "申报", "sort": "published_asc"}, headers=HEADERS)
    assert resp.json()["data"]["sort"] == "published_asc"


@pytest.mark.parametrize(
    ("sort", "expected"),
    [
        ("published_desc", True),
        ("published_asc", False),
    ],
)
def test_search_sorts_by_published_time(client, sort, expected):
    """按时间排时，类内顺序必须是真的按时间，而不是碰巧相关度也同向。"""
    resp = client.get("/v1/search", params={"q": "申报", "sort": sort}, headers=HEADERS)
    for group in resp.json()["data"]["groups"]:
        stamps = [
            i.get("published_at_utc") or i.get("published_at") or ""
            for i in group["items"]
        ]
        assert stamps == sorted(stamps, reverse=expected), (group["category"], stamps)


def test_search_time_sort_keeps_high_relevance_first_on_ties(client):
    """同一时刻的多篇仍按相关度降序：时间相同不等于顺序可以随便。"""
    resp = client.get(
        "/v1/search", params={"q": "申报", "sort": "published_asc"}, headers=HEADERS
    )
    for group in resp.json()["data"]["groups"]:
        items = group["items"]
        for prev, cur in zip(items, items[1:], strict=False):
            prev_time = prev.get("published_at_utc") or prev.get("published_at") or ""
            cur_time = cur.get("published_at_utc") or cur.get("published_at") or ""
            if prev_time != cur_time:
                continue
            assert prev["relevance"]["score"] >= cur["relevance"]["score"]


def _instant(item):
    """过滤用的是真实时刻（published_at_utc 优先），断言也必须按同一口径。

    早先这里断言 (published_at or '')[:10]，而 published_at 是站点墙钟：混时区数据下
    「筛 03-05 却返回标着 03-04/03-06 的条目」会同时被过滤正确、断言错误掩盖掉。
    """
    return item.get("published_at_utc") or item.get("published_at") or ""


def test_search_published_to_includes_whole_day(client):
    """published_to 是「含当天」：终点被推到次日 00:00:00，当天的行都要在。"""
    resp = client.get(
        "/v1/search",
        params={"q": "申报", "published_to": "2026-03-05"},
        headers=HEADERS,
    )
    data = resp.json()["data"]
    items = _all_items(data)
    assert items
    for item in items:
        assert _instant(item)[:10] <= "2026-03-05", _instant(item)


def test_search_published_from_excludes_earlier_days(client):
    base = client.get("/v1/search", params={"q": "申报"}, headers=HEADERS).json()["data"]
    resp = client.get(
        "/v1/search",
        params={"q": "申报", "published_from": "2026-03-05"},
        headers=HEADERS,
    )
    data = resp.json()["data"]
    assert data["total"] <= base["total"]
    for item in _all_items(data):
        assert _instant(item)[:10] >= "2026-03-05", _instant(item)


def test_articles_rejects_out_of_range_date_with_422(client):
    """9999-12-31 是日期控件常用的「不限」，加一天会溢出，必须 422 而不是 500。"""
    resp = client.get(
        "/v1/articles", params={"published_to": "9999-12-31"}, headers=HEADERS
    )
    assert resp.status_code == 422, resp.text
    resp = client.get(
        "/v1/search",
        params={"q": "申报", "published_to": "9999-12-31"},
        headers=HEADERS,
    )
    assert resp.status_code == 422, resp.text


def test_search_source_filter_returns_only_that_source(client):
    resp = client.get(
        "/v1/search",
        params={"q": "申报", "source": "prov-sci-tech"},
        headers=HEADERS,
    )
    data = resp.json()["data"]
    assert data["total"] > 0
    assert {i["source"] for i in _all_items(data)} == {"prov-sci-tech"}


def test_search_empty_source_param_is_treated_as_absent(client):
    base = client.get("/v1/search", params={"q": "申报"}, headers=HEADERS).json()["data"]
    blank = client.get("/v1/search", params={"q": "申报", "source": ""}, headers=HEADERS)
    assert blank.status_code == 200
    assert blank.json()["data"]["total"] == base["total"]


def test_search_unknown_source_returns_empty_not_everything(client):
    """筛一个不存在的站点必须是空集；静默退化成「不过滤」会让用户以为筛选生效了。"""
    resp = client.get(
        "/v1/search",
        params={"q": "申报", "source": "no-such-source"},
        headers=HEADERS,
    )
    data = resp.json()["data"]
    assert data["total"] == 0
    assert data["groups"] == []
