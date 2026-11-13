"""GET /v1/search：后端算好排序、按 6 大类分组返回给前端。

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
        # limit 与 fixtures/articles.json 的条数一致：少分析一篇，依赖该篇的用例会
        # 变成「筛出来是空的」这种看不出原因的失败。
        c.post("/v1/analyze", params={"limit": 5}, headers={"X-API-Key": "secret"})
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


def _display_day(item):
    """过滤按**卡片上显示的那一天**（站点墙钟 published_at 的日期），不是真实时刻。

    后端 _DAY_EXPR 是「墙钟优先、utc 兜底」，断言必须同源：夹具里 e5c4a7b6f1d28e93
    墙钟 2026-03-05 00:30 / utc 2026-03-04 16:30，两种口径不同日。若这里改回
    published_at_utc 优先，筛 03-05 的断言会把它当成 03-04 而判错，反过来说
    实现里 _DAY_EXPR 被换成 _TIME_EXPR 时也不会变红 —— B1 会静默复发。
    """
    return item.get("published_at") or item.get("published_at_utc") or ""


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
        assert _display_day(item)[:10] <= "2026-03-05", _display_day(item)


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
        assert _display_day(item)[:10] >= "2026-03-05", _display_day(item)


def test_search_date_filter_follows_displayed_wall_clock_day(client):
    """跨日样本必须按卡片上那一天的归属被筛到，这是 B1 的 API 层守护。

    夹具 e5c4a7b6f1d28e93 墙钟 2026-03-05 00:30 / utc 2026-03-04 16:30（东八区）。
    按展示口径：筛 03-05 应该出现，筛 03-04 不应该。若实现把 _DAY_EXPR 换回
    真实时刻，两条断言都会反过来 —— 这正是上一轮静默复发的那个 bug。
    """
    hit = client.get(
        "/v1/search",
        params={"q": "申报", "published_from": "2026-03-05", "published_to": "2026-03-05"},
        headers=HEADERS,
    ).json()["data"]
    assert "e5c4a7b6f1d28e93" in {i["article_id"] for i in _all_items(hit)}

    miss = client.get(
        "/v1/search",
        params={"q": "申报", "published_from": "2026-03-04", "published_to": "2026-03-04"},
        headers=HEADERS,
    ).json()["data"]
    assert "e5c4a7b6f1d28e93" not in {i["article_id"] for i in _all_items(miss)}


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


def test_articles_source_filter_accepts_comma_separated(client):
    """?source=a,b 是前端最常见的多选写法，必须拆开而不是当成一个站点名。

    原先整串被当成站点名 "a,b"，IN (...) 永远不命中，静默返回 0 条 —— 用户会以为
    这个组合下没有数据。
    """
    resp = client.get(
        "/v1/articles",
        params={"source": "prov-sci-tech,other-site", "limit": 5},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body, "至少要有 prov-sci-tech 的数据"
    assert {i["source"] for i in body} == {"prov-sci-tech"}


def test_articles_source_filter_empty_string_is_treated_as_absent(client):
    """?source=（空串）是「清空多选」残留的空参数，一律当没传。

    若当成「筛一个不存在的站点」返回空集，用户清空站点筛选后看到的是「无结果」，
    而不是全部数据 —— 会以为筛选坏了。
    """
    base = client.get("/v1/articles", params={"limit": 1}, headers=HEADERS).json()
    blank = client.get("/v1/articles", params={"source": ""}, headers=HEADERS)
    assert blank.status_code == 200
    assert blank.json()["meta"]["total"] == base["meta"]["total"] > 0


def test_articles_without_source_filter_is_not_empty(client):
    resp = client.get("/v1/articles", params={"limit": 1}, headers=HEADERS)
    assert resp.json()["meta"]["total"] > 0


def test_search_source_filter_accepts_comma_separated(client):
    resp = client.get(
        "/v1/search",
        params={"q": "申报", "source": "prov-sci-tech,other-site"},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    items = _all_items(resp.json()["data"])
    assert items
    assert {i["source"] for i in items} == {"prov-sci-tech"}
