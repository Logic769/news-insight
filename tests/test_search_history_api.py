"""搜索历史 + 高相关要素摘要 + 搜索词画像的端到端用例。"""

from __future__ import annotations

import datetime as dt
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from app import accounts, auth, db, recommend
from app.main import create_app
from app.settings import load_settings

API_KEY = {"X-API-Key": "secret"}
PASSWORD = "Str0ngPass!2026"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NEWS_SOURCE_MODE", "demo")
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.setenv("INSIGHT_API_KEY", "secret")
    app = create_app(load_settings())
    with TestClient(app) as c:
        c.post("/v1/analyze", params={"limit": 5}, headers=API_KEY)
        yield c


def _create_user(client, username="searcher", role="user"):
    settings = client.app.state.settings
    conn = db.connect(settings.db_path)
    try:
        return auth.create_user(
            conn,
            username=username,
            password=PASSWORD,
            display_name=username,
            department="测试部",
            role=role,
        )
    finally:
        conn.close()


def _token(client, username="searcher"):
    resp = client.post("/v1/auth/login", json={"username": username, "password": PASSWORD})
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _all_items(body):
    return [item for group in body["data"]["groups"] for item in group["items"]]


def test_search_history_requires_login(client):
    assert client.get("/v1/me/search-history").status_code == 401
    assert client.post("/v1/me/search-history", json={"query": "申报"}).status_code == 401


def test_search_is_recorded_deduped_clicked_and_deleted(client):
    _create_user(client)
    headers = _auth(_token(client))

    first = client.get("/v1/search", params={"q": "申报"}, headers=headers)
    assert first.status_code == 200
    body = first.json()
    history_id = body["meta"]["search_history_id"]
    assert history_id
    items = _all_items(body)
    assert items

    history = client.get("/v1/me/search-history", headers=headers)
    assert history.status_code == 200
    assert history.json()["meta"]["total"] == 1
    row = history.json()["data"][0]
    assert row["id"] == history_id
    assert row["query"] == "申报"
    assert row["result_count"] == body["data"]["total"]
    assert row["clicked_article_id"] is None

    # 10 分钟内同关键词只更新时间/结果数，不会新增第二条。
    again = client.get("/v1/search", params={"q": "申报"}, headers=headers)
    assert again.json()["meta"]["search_history_id"] == history_id
    assert client.get("/v1/me/search-history", headers=headers).json()["meta"]["total"] == 1

    article_id = items[0]["article_id"]
    clicked = client.post(
        f"/v1/me/search-history/{history_id}/click",
        json={"article_id": article_id},
        headers=headers,
    )
    assert clicked.status_code == 200, clicked.text
    assert clicked.json()["data"]["clicked"] is True
    row = client.get("/v1/me/search-history", headers=headers).json()["data"][0]
    assert row["clicked_article_id"] == article_id
    assert row["clicked_at"]

    deleted = client.delete(f"/v1/me/search-history/{history_id}", headers=headers)
    assert deleted.status_code == 200
    assert deleted.json()["data"]["removed"] == 1
    assert client.get("/v1/me/search-history", headers=headers).json()["meta"]["total"] == 0

    # 再记录两条后清空全部。
    assert client.post("/v1/me/search-history", json={"query": "补贴"}, headers=headers).status_code == 200
    assert client.post("/v1/me/search-history", json={"query": "人才"}, headers=headers).status_code == 200
    cleared = client.delete("/v1/me/search-history", headers=headers)
    assert cleared.status_code == 200
    assert cleared.json()["data"]["cleared"] == 2


def test_search_click_rejects_unknown_article_and_history(client):
    _create_user(client)
    headers = _auth(_token(client))
    assert (
        client.post(
            "/v1/me/search-history/9999/click", json={"article_id": "a1f0c3e2b7d94a51"}, headers=headers
        ).status_code
        == 404
    )
    created = client.post("/v1/me/search-history", json={"query": "申报"}, headers=headers)
    history_id = created.json()["data"]["id"]
    assert (
        client.post(
            f"/v1/me/search-history/{history_id}/click",
            json={"article_id": "does-not-exist"},
            headers=headers,
        ).status_code
        == 404
    )


def test_articles_include_highlights_only_for_high_relevance(client):
    resp = client.get(
        "/v1/articles",
        params={"include_highlights": "true", "limit": 20},
        headers=API_KEY,
    )
    assert resp.status_code == 200
    rows = resp.json()["data"]
    assert rows
    high = [row for row in rows if float(row.get("relevance_score") or 0) >= 0.8]
    assert high, "demo 数据里至少应有一篇高相关文章"
    for row in rows:
        assert "highlights" in row
        if float(row.get("relevance_score") or 0) >= 0.8:
            assert row["highlights"]
            assert all(item.get("key") and item.get("label") and item.get("value") for item in row["highlights"])
        else:
            assert row["highlights"] == []

    # 默认不返回，避免列表传输无谓的摘要字段。
    plain = client.get("/v1/articles", params={"limit": 1}, headers=API_KEY).json()["data"][0]
    assert "highlights" not in plain


def test_profile_uses_search_terms_and_clicked_articles(client):
    user = _create_user(client, "profile-user")
    settings = client.app.state.settings
    conn = db.connect(settings.db_path)
    try:
        accounts.record_search(conn, user_id=int(user["id"]), query="新能源 申报", result_count=3)
        assert accounts.mark_search_click(
            conn,
            user_id=int(user["id"]),
            history_id=int(accounts.list_search_history(conn, user_id=int(user["id"]))[0][0]["id"]),
            article_id="a1f0c3e2b7d94a51",
        )
        profile = recommend.build_profile(conn, int(user["id"]))
    finally:
        conn.close()
    assert profile.search_terms
    assert "新能源 申报" in profile.search_terms
    assert "新能源" in profile.search_terms
    assert profile.categories, "点击文章应回填分类兴趣"
    score, reason = recommend.score_row(
        {"title": "2026 年新能源项目申报指南", "category": "project_application", "tags": []},
        profile,
    )
    assert score > 0
    assert "匹配历史搜索" in reason


def test_readonly_database_still_serves_search(client, monkeypatch):
    """/v1/search 在只读库上必须照常返回结果，只是不留痕（S1 回归）。

    真实场景是库文件只读挂载 / PRAGMA query_only。这里直接把留痕的写入端打成
    只读异常，验证的是「留痕失败不许冒泡」这条约束，而不是 sqlite 的只读行为本身。
    """
    _create_user(client)
    headers = _auth(_token(client))

    def _boom(*args, **kwargs):
        raise sqlite3.OperationalError("attempt to write a readonly database")

    monkeypatch.setattr(accounts, "record_search", _boom)
    resp = client.get("/v1/search", params={"q": "申报"}, headers=headers)
    assert resp.status_code == 200
    assert _all_items(resp.json()), "留痕失败不应影响检索结果"
    # 没写进去，所以 meta 里的 id 是空的，前端点了也不会归因到不存在的记录。
    assert resp.json()["meta"]["search_history_id"] is None
    assert client.get("/v1/me/search-history", headers=headers).json()["meta"]["total"] == 0


def test_readonly_write_endpoint_returns_503_not_500(client, monkeypatch):
    """只读库上的写端点必须是 503 + Retry-After，且要快速返回。

    原来每个写路由各自 try/except，漏一个就是 500；而且连接继承 30s 的
    busy_timeout，客户端要等满半分钟。现在统一由 OperationalError 处理器映射，
    连接自己把等待压到 _WRITE_BUSY_TIMEOUT_MS。
    """
    import time

    from app import main as main_mod

    _create_user(client)
    headers = _auth(_token(client))

    def _readonly(*args, **kwargs):
        raise sqlite3.OperationalError("attempt to write a readonly database")

    monkeypatch.setattr(accounts, "update_user", _readonly)
    started = time.monotonic()
    resp = client.patch("/v1/me/profile", json={"display_name": "新名字"}, headers=headers)
    elapsed = time.monotonic() - started
    assert resp.status_code == 503, resp.text
    assert resp.headers.get("Retry-After") == "60"
    assert elapsed < 5, f"写端点不该等满 busy_timeout：{elapsed:.2f}s"
    assert main_mod._WRITE_BUSY_TIMEOUT_MS == 3000


def test_write_endpoint_busy_returns_503_quickly(client, monkeypatch):
    """写锁被外部连接占住时：503 + Retry-After=1，且不超过写入超时上限。"""
    import time

    from app import main as main_mod

    _create_user(client)
    headers = _auth(_token(client))
    settings = client.app.state.settings

    blocker = db.connect(settings.db_path)
    try:
        blocker.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        resp = client.patch("/v1/me/profile", json={"display_name": "占位"}, headers=headers)
        elapsed = time.monotonic() - started
    finally:
        blocker.rollback()
        blocker.close()
    assert resp.status_code == 503, resp.text
    assert resp.headers.get("Retry-After") == "1"
    # 上限写死成 5s（= 3s 超时 + 调度余量）：拿常量本身做上界的话，把常量调大到
    # 6000 时这条用例仍然通过，测不出「有没有真的压下来」。
    assert elapsed < 5, f"写端点不该等满 busy_timeout：{elapsed:.2f}s"
    assert main_mod._WRITE_BUSY_TIMEOUT_MS == 3000


def test_analyze_endpoint_maps_readonly_to_503_not_502(client, monkeypatch):
    """POST /v1/analyze 遇到只读库必须是 503，不是「分析失败」502。

    502 会让客户端以为上游 LLM/新闻 API 挂了并重试整批；实际是部署形态问题，
    该告诉它「这个实例不接受写入」。
    """
    import time

    from app import main as main_mod

    _create_user(client, "root", role="admin")

    def _readonly(*args, **kwargs):
        raise sqlite3.OperationalError("attempt to write a readonly database")

    monkeypatch.setattr(main_mod, "analyze_batch", _readonly)
    started = time.monotonic()
    resp = client.post("/v1/analyze", params={"limit": 1}, headers=_auth(_token(client, "root")))
    elapsed = time.monotonic() - started
    assert resp.status_code == 503, resp.text
    assert resp.headers.get("Retry-After") == "60"
    assert elapsed < 5


def test_job_submit_maps_readonly_to_503(client, monkeypatch):
    """后台作业入口同样是写：只读时 503 + Retry-After，而不是 500。"""
    from app import jobs as jobs_mod

    _create_user(client, "root", role="admin")

    def _readonly(*args, **kwargs):
        raise sqlite3.OperationalError("attempt to write a readonly database")

    monkeypatch.setattr(jobs_mod.db, "create_analysis_job", _readonly)
    resp = client.post(
        "/v1/jobs",
        json={"type": "analyze", "limit": 1},
        headers=_auth(_token(client, "root")),
    )
    assert resp.status_code == 503, resp.text
    assert resp.headers.get("Retry-After") == "60"


def test_concurrent_same_query_is_deduped(client):
    """8 线程同词并发：只能有一条记录，且所有线程拿到同一个 id（S2 回归）。

    原来是裸 SELECT-then-INSERT，线程同时读到「窗口内没有」就各插一条。这里用
    独立连接并发跑，断言去重窗口内的 id 唯一。
    """
    _create_user(client)
    settings = client.app.state.settings
    results: list[int] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def _worker() -> None:
        conn = db.connect(settings.db_path)
        try:
            history_id = accounts.record_search(
                conn, user_id=1, query="新能源 申报", result_count=7
            )
            with lock:
                results.append(history_id)
        except BaseException as exc:  # noqa: BLE001 - 线程里的异常要带回主线程
            with lock:
                errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=_worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, errors
    assert len(results) == 8
    assert len(set(results)) == 1, f"并发插入了多条同词记录: {sorted(set(results))}"
    conn = db.connect(settings.db_path)
    try:
        rows = conn.execute(
            "SELECT COUNT(*) FROM user_search_history WHERE user_id = 1"
        ).fetchone()[0]
    finally:
        conn.close()
    assert rows == 1


def test_profile_ignores_boilerplate_search_terms(client):
    """通用政务词不进画像，也不该让「匹配历史搜索」成为随便什么文章的理由（S3 回归）。

    生产库 481 篇里「通知」出现在 36.6% 的标题中。用户搜过一次「申报通知」之后，
    画像若把「通知」当强信号，几乎所有文章都会命中，排序退化成按新鲜度、理由却写着
    「匹配历史搜索」。
    """
    _create_user(client)
    settings = client.app.state.settings
    conn = db.connect(settings.db_path)
    try:
        accounts.record_search(conn, user_id=1, query="申报通知", result_count=40)
        profile = recommend.build_profile(conn, 1)
    finally:
        conn.close()

    # 「申报通知」整串本身就是通用政务词，拆出来的「申报」「通知」也是——三者都
    # 该被滤掉，于是这条搜索对画像不产生任何信号。这是刻意的：宁可少一个信号，
    # 也不要让遍布全库的词把排序带偏。
    for boilerplate in ("申报通知", "通知", "申报", "项目"):
        assert boilerplate not in profile.search_terms, f"停用词 {boilerplate} 进了画像"

    # 泛词标题不该被算成「匹配历史搜索」。
    reason = recommend.score_row(
        {"title": "关于做好项目申报工作的通知", "category": "project_application", "tags": []},
        profile,
    )[1]
    assert "匹配历史搜索" not in reason


def test_profile_keeps_specific_search_terms(client):
    """有区分度的领域词必须保留——停用词表不能把画像信号一起删光。"""
    _create_user(client)
    settings = client.app.state.settings
    conn = db.connect(settings.db_path)
    try:
        accounts.record_search(conn, user_id=1, query="专精特新 申报", result_count=5)
        profile = recommend.build_profile(conn, 1)
    finally:
        conn.close()

    assert "专精特新" in profile.search_terms
    score, reason = recommend.score_row(
        {"title": "专精特新中小企业认定工作通知", "category": "enterprise_certification", "tags": []},
        profile,
    )
    assert score > 0
    assert "匹配历史搜索" in reason


def test_profile_splits_chinese_query_without_spaces(client):
    """无空格中文长查询必须切出有区分度的片段，否则这条搜索等于不存在（S3 回归）。

    「2026年度专精特新申报通知」是一条典型的真实查询：没有空格，整串在标题里
    永远匹配不到。修复前 build_profile 只产出整串，实测 term 在典型标题里命中
    False —— 用户搜了，画像却什么都没学到。
    """
    _create_user(client)
    settings = client.app.state.settings
    conn = db.connect(settings.db_path)
    try:
        accounts.record_search(conn, user_id=1, query="2026年度专精特新申报通知", result_count=12)
        profile = recommend.build_profile(conn, 1)
    finally:
        conn.close()

    # 有区分度的部分留下来了。
    assert "专精特新" in profile.search_terms
    # 通用片段（整串、年份、政务词）不该进画像。
    for noise in ("2026", "2026年", "年度", "申报", "通知", "2026年度专精特新申报通知"):
        assert noise not in profile.search_terms, f"{noise} 不该进画像"

    # 关键：这条搜索现在真的能命中相关标题了。
    score, reason = recommend.score_row(
        {"title": "专精特新中小企业认定工作通知", "category": "enterprise_certification", "tags": []},
        profile,
    )
    assert score > 0
    assert "匹配历史搜索" in reason


def test_profile_keeps_domain_words_that_look_generic(client):
    """「企业」「服务」这类词看着通用，但组合起来是领域词，不能进停用词表。

    停用词表过宽会误杀「专精特新企业」这类查询——把真信号删掉，代价比留下一个
    泛词大。
    """
    _create_user(client)
    settings = client.app.state.settings
    conn = db.connect(settings.db_path)
    try:
        accounts.record_search(conn, user_id=1, query="专精特新企业 服务", result_count=3)
        profile = recommend.build_profile(conn, 1)
    finally:
        conn.close()

    assert "专精特新企业" in profile.search_terms
    assert "服务" in profile.search_terms


def test_record_search_keeps_caller_transaction_open(client):
    """已有外层事务时，record_search 不能替调用方提交（S2 回归，owns_txn=False 分支）。

    修复前这个分支无条件 conn.commit()，而 except 分支只在 owns_txn 时 rollback，
    一进一出不对称：调用方之后 rollback 撤不回自己的业务写。现有用例全部走
    owns_txn=True（各自 db.connect 无前置写），测不出这条分支。
    """
    _create_user(client)
    settings = client.app.state.settings
    conn = db.connect(settings.db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE users SET display_name = ? WHERE id = 1", ("待提交",))
        assert conn.in_transaction is True

        accounts.record_search(conn, user_id=1, query="专精特新", result_count=1)
        # 关键断言：外层事务仍然开着，没有被提前提交。
        assert conn.in_transaction is True

        conn.rollback()
        assert conn.in_transaction is False
        # 调用方自己的写入跟着一起回滚了，说明 record_search 没有提交它。
        row = conn.execute("SELECT display_name FROM users WHERE id = 1").fetchone()
        assert row["display_name"] != "待提交"
        # record_search 的写入也随外层事务回滚，没有落库。
        count = conn.execute("SELECT COUNT(*) AS n FROM user_search_history").fetchone()["n"]
        assert count == 0
    finally:
        conn.close()


def test_record_search_rolls_back_savepoint_on_failure(client):
    """外层事务里 record_search 写入后失败，只回退自己那一段，不影响调用方（S2 回归）。

    必须让失败发生在「已经写入之后」：早先这版用空关键词抛 ValueError，而该校验
    位于 SAVEPOINT 创建之前，根本没进回滚逻辑——把 ROLLBACK TO 改成 pass 用例照样
    通过，零鉴别力。这里用触发器让清理阶段的 DELETE 失败，才真正走到回滚分支。
    """
    _create_user(client)
    settings = client.app.state.settings
    conn = db.connect(settings.db_path)
    try:
        # 先灌到超过条数上限，让清理阶段的 DELETE 真的删到行。
        # 触发器只在「有行被删」时才触发；首次记录时两条清理 DELETE 都没命中，
        # 根本不会走到回滚分支——这正是上一版用例零鉴别力的原因。
        now = dt.datetime.now(dt.timezone.utc)
        conn.executemany(
            "INSERT INTO user_search_history (user_id, query, result_count, created_at)"
            " VALUES (1, ?, 0, ?)",
            [
                (f"旧词{i}", (now - dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))
                for i in range(accounts.SEARCH_HISTORY_LIMIT + 5)
            ],
        )
        # 禁止清理阶段的 DELETE，模拟「写进去一半后失败」。
        conn.execute(
            "CREATE TRIGGER block_sh_delete BEFORE DELETE ON user_search_history "
            "BEGIN SELECT RAISE(ABORT, 'blocked'); END"
        )
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE users SET display_name = ? WHERE id = 1", ("待提交",))
        try:
            accounts.record_search(conn, user_id=1, query="专精特新", result_count=1)
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("清理阶段的 DELETE 被触发器阻断，应当抛异常")
        # 失败后外层事务仍开着，调用方的写入还在——证明只回退了 savepoint 那一段。
        assert conn.in_transaction is True
        # 本次 record_search 插入的行也被回退掉了（只剩预置的旧词）。
        assert (
            conn.execute("SELECT COUNT(*) AS n FROM user_search_history").fetchone()["n"]
            == accounts.SEARCH_HISTORY_LIMIT + 5
        )
        # 回滚把这次插入的「专精特新」也撤掉了：留下它就说明 savepoint 没回滚干净。
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM user_search_history WHERE query = '专精特新'"
        ).fetchone()["n"] == 0
        row = conn.execute("SELECT display_name FROM users WHERE id = 1").fetchone()
        assert row["display_name"] == "待提交"
        conn.rollback()
    finally:
        conn.execute("DROP TRIGGER IF EXISTS block_sh_delete")
        conn.close()


def test_readonly_complete_schema_starts_normally(tmp_path):
    """只读 + schema 齐全：init_db 必须正常返回（只读部署是合法形态）。"""
    import os
    import stat

    db_path = tmp_path / "insight.db"
    db.init_db(db_path)
    conn = db.connect(db_path)
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.close()

    os.chmod(db_path, stat.S_IREAD)
    try:
        db.init_db(db_path)  # 不该抛
    finally:
        os.chmod(db_path, stat.S_IWRITE | stat.S_IREAD)


def test_readonly_lagging_schema_fails_fast_instead_of_500_later(tmp_path):
    """只读 + schema 落后：必须启动即失败，而不是 /health 200 却检索 500。

    这里用真实的缺列（DROP COLUMN），不是 monkeypatch 迁移函数：上一版用例
    把 _migrate_columns 换掉就直接 return，于是既没证明 schema 会落后、也没
    证明服务会拒绝启动，零鉴别力。
    """
    import os
    import stat

    from app.main import create_app
    from app.settings import Settings

    db_path = tmp_path / "insight.db"
    db.init_db(db_path)
    conn = db.connect(db_path)
    conn.execute("ALTER TABLE analyzed_articles DROP COLUMN title_norm")
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.commit()
    conn.close()

    settings = Settings(data_dir=db_path.parent, news_source_mode="live", require_auth=False)
    os.chmod(db_path, stat.S_IREAD)
    try:
        # init_db 在 lifespan 里：抛 RuntimeError 才能让部署者立刻看到「先以可写
        # 方式启动一次完成迁移」，而不是等第一次检索才炸 no such column。
        with pytest.raises(RuntimeError) as ctx:
            db.init_db(db_path)
        assert "title_norm" in str(ctx.value)
        assert "列" in str(ctx.value)
        # 服务也不该「看起来健康」：create_app 本身不建连，启动失败由 lifespan 抛出。
        app = create_app(settings)
        with pytest.raises(RuntimeError):
            with TestClient(app):
                pass
    finally:
        os.chmod(db_path, stat.S_IWRITE | stat.S_IREAD)


def test_readonly_missing_table_fails_fast_too(tmp_path):
    """只读 + 缺表：同样必须启动即失败（缺表与缺列是同一类故障）。

    原来的 _missing_columns 只看 analyzed_articles 的列，删掉 `sites` 表后
    init_db 照样返回，随后查询会炸 `no such table: sites`。
    """
    import os
    import stat

    db_path = tmp_path / "insight.db"
    db.init_db(db_path)
    conn = db.connect(db_path)
    conn.execute("DROP TABLE sites")
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.commit()
    conn.close()

    os.chmod(db_path, stat.S_IREAD)
    try:
        with pytest.raises(RuntimeError) as ctx:
            db.init_db(db_path)
        assert "sites" in str(ctx.value)
    finally:
        os.chmod(db_path, stat.S_IWRITE | stat.S_IREAD)


def test_readonly_non_wal_database_still_serves_search(tmp_path):
    """只读且非 WAL 的库必须仍能检索（S1 真实回归）。

    db.connect 里「非 WAL 就切 WAL」是一次写操作，只读文件上必然抛
    attempt to write a readonly database。该异常原来直接冒泡，导致认证中间件和
    GET /v1/search 全部 500——只读部署这种合法形态整个不可用。

    这里用真实文件（不是 monkeypatch），把库设成 DELETE 模式再置只读，
    端到端跑一次检索。
    """
    import os
    import stat

    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.settings import Settings

    # 文件名必须与 Settings.db_path 一致（data_dir / "insight.db"），否则服务打开的
    # 是另一个新库，这个用例就退化成「测一个可写库能检索」——零鉴别力。
    db_path = tmp_path / "insight.db"
    # 用真实 schema：手搓一张假表测不出 lifespan 里的 analysis_jobs 写路径。
    db.init_db(db_path)
    conn = db.connect(db_path)
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.close()

    # Settings.data_dir 是目录，db_path 是它的派生属性。
    settings = Settings(data_dir=db_path.parent, news_source_mode="live", require_auth=False)
    app = create_app(settings)
    os.chmod(db_path, stat.S_IREAD)
    try:
        # init_db 必须能在只读库上跑完：它在 lifespan 里，冒泡 = 服务起不来。
        db.init_db(db_path)
        # 只读非 WAL 下，init_db / JobManager.start / db.connect 三处都会碰到写操作；
        # 任何一处冒泡都会让 lifespan 失败，服务根本起不来。
        with TestClient(app) as client:
            resp = client.get("/v1/search", params={"q": "专精特新"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["meta"]["search_history_id"] is None
    finally:
        os.chmod(db_path, stat.S_IWRITE | stat.S_IREAD)


def test_http_exception_headers_are_forwarded(client):
    """503 的 Retry-After 必须真的到客户端（S4 回归）。

    自定义 HTTPException handler 只构造了 content，没传 exc.headers，
    于是 Retry-After 被静默丢弃，「稍后重试」的语义没了。
    """
    from fastapi import HTTPException

    @client.app.get("/__test_503__")
    def _raise_503() -> None:
        raise HTTPException(status_code=503, detail="忙", headers={"Retry-After": "1"})

    # 该 fixture 已设 INSIGHT_API_KEY，不带 key 会被中间件拦成 401。
    resp = client.get("/__test_503__", headers=API_KEY)
    assert resp.status_code == 503
    assert resp.headers.get("Retry-After") == "1"
