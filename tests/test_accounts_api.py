"""账号、个人中心与管理端接口的端到端用例（TestClient + 临时库）。"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from app import accounts, auth, db
from app.main import create_app
from app.news_client import UpstreamError
from app.settings import load_settings

API_KEY = {"X-API-Key": "secret"}
PASSWORD = "InitPassw0rd!"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NEWS_SOURCE_MODE", "demo")
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.setenv("INSIGHT_API_KEY", "secret")
    monkeypatch.setenv("INSIGHT_ADMIN_PASSWORD", "")
    app = create_app(load_settings())
    with TestClient(app) as c:
        yield c


def _create_user(client, username, *, password=PASSWORD, role="user", status="active"):
    settings = client.app.state.settings
    conn = db.connect(settings.db_path)
    try:
        user = auth.create_user(
            conn,
            username=username,
            password=password,
            display_name=username,
            department="测试部",
            role=role,
        )
        if status != "active":
            accounts.update_user(conn, int(user["id"]), status=status)
        return user
    finally:
        conn.close()


def _login(client, username, password=PASSWORD):
    return client.post("/v1/auth/login", json={"username": username, "password": password})


def _token(client, username, password=PASSWORD):
    resp = _login(client, username, password)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_login_locks_after_five_failures_and_rejects_correct_password(client):
    _create_user(client, "alice")

    messages = []
    for _ in range(4):
        resp = _login(client, "alice", "wrong")
        assert resp.status_code == 401
        messages.append(resp.json()["error"]["message"])
    assert "还可尝试 4 次" in messages[0]
    assert "还可尝试 1 次" in messages[-1]

    locked = _login(client, "alice", "wrong")
    assert locked.status_code == 423
    assert "锁定 15 分钟" in locked.json()["error"]["message"]

    # 锁定期内即使密码正确也不放行
    assert _login(client, "alice").status_code == 423


def test_login_me_logout_and_unknown_user(client):
    _create_user(client, "bob")
    token = _token(client, "bob")

    me = client.get("/v1/auth/me", headers=_auth(token))
    assert me.status_code == 200
    assert me.json()["data"]["username"] == "bob"
    assert "password_hash" not in me.json()["data"]

    assert client.post("/v1/auth/logout", headers=_auth(token)).status_code == 200
    assert client.get("/v1/auth/me", headers=_auth(token)).status_code == 401
    assert _login(client, "nobody").status_code == 401
    assert _login(client, "nobody").json()["error"]["message"] == "用户名或密码错误"


def test_change_password_enforces_policy_and_current_password(client):
    _create_user(client, "carol")
    token = _token(client, "carol")

    weak = client.post(
        "/v1/auth/password",
        json={"current_password": PASSWORD, "new_password": "abcdefgh"},
        headers=_auth(token),
    )
    assert weak.status_code == 400
    assert "大写字母" in weak.json()["error"]["message"]

    wrong = client.post(
        "/v1/auth/password",
        json={"current_password": "nope", "new_password": "NewPassw0rd!"},
        headers=_auth(token),
    )
    assert wrong.status_code == 400
    assert wrong.json()["error"]["message"] == "原密码不正确"

    changed = client.post(
        "/v1/auth/password",
        json={"current_password": PASSWORD, "new_password": "NewPassw0rd!"},
        headers=_auth(token),
    )
    assert changed.status_code == 200
    assert _login(client, "carol", PASSWORD).status_code == 401
    assert _login(client, "carol", "NewPassw0rd!").status_code == 200
    # 改密只保留当前会话，旧 token 仍可用
    assert client.get("/v1/auth/me", headers=_auth(token)).status_code == 200


def test_personal_favorites_history_and_preferences(client):
    # 先用服务 key 触发一次分析，拿到真实文章
    assert client.post("/v1/analyze", params={"limit": 4}, headers=API_KEY).status_code == 200
    article_id = "a1f0c3e2b7d94a51"

    _create_user(client, "dave")
    token = _token(client, "dave")
    headers = _auth(token)

    detail = client.get(f"/v1/articles/{article_id}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["data"]["is_favorite"] is False

    history = client.get("/v1/me/history", headers=headers)
    assert history.status_code == 200
    assert [row["article_id"] for row in history.json()["data"]] == [article_id]

    added = client.post("/v1/me/favorites", json={"article_id": article_id}, headers=headers)
    assert added.status_code == 200
    assert added.json()["data"]["favorite"] is True
    favorites = client.get("/v1/me/favorites", headers=headers)
    assert favorites.json()["meta"]["total"] == 1
    assert favorites.json()["data"][0]["title"]

    removed = client.delete(f"/v1/me/favorites/{article_id}", headers=headers)
    assert removed.status_code == 200
    assert client.get("/v1/me/favorites", headers=headers).json()["meta"]["total"] == 0

    prefs = client.get("/v1/me/preferences", headers=headers)
    assert prefs.status_code == 200
    assert prefs.json()["data"]["industries"] == []
    saved = client.put(
        "/v1/me/preferences",
        json={"industries": ["新能源", "  ", "生物医药"], "push_enabled": False, "digest_time": "08:30"},
        headers=headers,
    )
    assert saved.status_code == 200
    assert saved.json()["data"]["industries"] == ["新能源", "生物医药"]
    assert saved.json()["data"]["push_enabled"] is False

    cleared = client.delete("/v1/me/history", headers=headers)
    assert cleared.status_code == 200
    assert cleared.json()["data"]["cleared"] == 1
    assert client.get("/v1/me/history", headers=headers).json()["meta"]["total"] == 0


def test_admin_user_management_and_role_guard(client):
    _create_user(client, "admin", role="admin")
    _create_user(client, "normal", role="user")
    admin_headers = _auth(_token(client, "admin"))
    user_headers = _auth(_token(client, "normal"))

    assert client.get("/v1/admin/users", headers=user_headers).status_code == 403
    assert client.get("/v1/admin/users", headers=API_KEY).status_code == 200

    created = client.post(
        "/v1/admin/users",
        json={"username": "newbie", "display_name": "新同事", "department": "政策部", "role": "user"},
        headers=admin_headers,
    )
    assert created.status_code == 200, created.text
    temp = created.json()["data"]["temporary_password"]
    assert temp and "password_hash" not in created.json()["data"]

    assert _login(client, "newbie", temp).status_code == 200
    duplicated = client.post(
        "/v1/admin/users",
        json={"username": "newbie", "password": "DupPassw0rd!"},
        headers=admin_headers,
    )
    assert duplicated.status_code == 409

    newbie_id = created.json()["data"]["id"]
    disabled = client.patch(
        f"/v1/admin/users/{newbie_id}", json={"status": "disabled"}, headers=admin_headers
    )
    assert disabled.status_code == 200
    assert _login(client, "newbie", temp).status_code == 403

    reset = client.post(
        f"/v1/admin/users/{newbie_id}/reset-password", headers=admin_headers
    )
    assert reset.status_code == 200
    assert reset.json()["data"]["temporary_password"]


def test_admin_cannot_disable_or_demote_self(client):
    _create_user(client, "selfadmin", role="admin")
    token = _token(client, "selfadmin")
    headers = _auth(token)
    me = client.get("/v1/auth/me", headers=headers).json()["data"]
    assert me["role"] == "admin"

    disabled = client.patch(
        f"/v1/admin/users/{me['id']}", json={"status": "disabled"}, headers=headers
    )
    assert disabled.status_code == 400
    assert "不能禁用或降级当前登录的管理员" in disabled.json()["error"]["message"]

    demoted = client.patch(
        f"/v1/admin/users/{me['id']}", json={"role": "user"}, headers=headers
    )
    assert demoted.status_code == 400
    assert client.get("/v1/auth/me", headers=headers).json()["data"]["role"] == "admin"


def test_admin_sites_stats_and_operation_logs(client, monkeypatch):
    _create_user(client, "root", role="admin")
    headers = _auth(_token(client, "root"))

    # 管理台站点配置现在代理到 crawler 的 sources.local.json；单测不启动第二个
    # 服务，用内存实现替换客户端，只验证鉴权/审计/状态码与路径参数契约。
    store: dict[str, dict] = {}

    def fake_list(self):
        return list(store.values())

    def fake_create(self, payload):
        row = dict(payload)
        row.setdefault("has_api", False)
        store[row["name"]] = row
        return row

    def fake_update(self, name, payload):
        if name not in store:
            return None
        store[name].update(payload)
        return store[name]

    def fake_delete(self, name):
        if name not in store:
            raise UpstreamError("站点不存在", status_code=404, fallbackable=False)
        store.pop(name)

    monkeypatch.setattr("app.main.NewsClient.list_sites", fake_list)
    monkeypatch.setattr("app.main.NewsClient.create_site", fake_create)
    monkeypatch.setattr("app.main.NewsClient.update_site", fake_update)
    monkeypatch.setattr("app.main.NewsClient.delete_site", fake_delete)

    site = client.post(
        "/v1/admin/sites",
        json={"name": "示例站", "list_url": "https://example.com/news", "region": "上海"},
        headers=headers,
    )
    assert site.status_code == 201, site.text
    site_id = site.json()["data"]["name"]
    assert client.get("/v1/admin/sites", headers=headers).json()["meta"]["total"] == 1

    updated = client.patch(
        f"/v1/admin/sites/{site_id}", json={"enabled": False}, headers=headers
    )
    assert updated.status_code == 200
    assert updated.json()["data"]["enabled"] is False
    # 删除必须真正移除记录：复用 enabled 会让「停用」与「删除」不可区分，
    # 管理页删除后仍能看到条目，同名站点也无法重建。
    assert client.delete(f"/v1/admin/sites/{site_id}", headers=headers).status_code == 200
    assert client.get("/v1/admin/sites", headers=headers).json()["meta"]["total"] == 0
    assert client.delete(f"/v1/admin/sites/{site_id}", headers=headers).status_code == 404

    stats = client.get("/v1/admin/stats?days=7", headers=headers)
    assert stats.status_code == 200
    data = stats.json()["data"]
    assert {"totals", "daily", "categories", "jobs", "success_rate"} <= set(data)

    logs = client.get("/v1/admin/operation-logs", headers=headers)
    assert logs.status_code == 200
    actions = [row["action"] for row in logs.json()["data"]]
    assert "admin.user_create" not in actions  # 本用例没建用户
    assert "admin.site_create" in actions


def test_admin_stats_merges_cli_runs_and_counts_errors(client):
    """管理台成功率必须同时反映 API 作业与 CLI 分析运行，否则线上 CLI 跑多少次都显示 0。"""
    _create_user(client, "root", role="admin")
    headers = _auth(_token(client, "root"))

    before = client.get("/v1/admin/stats?days=7", headers=headers).json()["data"]["jobs"]
    settings = client.app.state.settings
    today = accounts._local_today()
    recent = (today - dt.timedelta(days=1)).isoformat()
    stale = (today - dt.timedelta(days=30)).isoformat()
    conn = db.connect(settings.db_path)
    try:
        conn.execute(
            "INSERT INTO runs (started_at, finished_at, mode, scanned, analyzed, skipped, errors)"
            " VALUES (?, ?, 'live', 10, 9, 1, 0)",
            (f"{recent}T01:00:00Z", f"{recent}T01:01:00Z"),
        )
        conn.execute(
            "INSERT INTO runs (started_at, finished_at, mode, scanned, analyzed, skipped, errors)"
            " VALUES (?, ?, 'live', 10, 0, 0, 3)",
            (f"{recent}T02:00:00Z", f"{recent}T02:01:00Z"),
        )
        conn.execute(
            "INSERT INTO runs (started_at, mode, scanned, analyzed, skipped, errors)"
            " VALUES (?, 'live', 10, 0, 0, 0)",
            (f"{recent}T03:00:00Z",),
        )
        # 窗口外的失败 run 不能拉低近 7 天成功率：days 过滤必须同时作用于 runs。
        conn.execute(
            "INSERT INTO runs (started_at, finished_at, mode, scanned, analyzed, skipped, errors)"
            " VALUES (?, ?, 'live', 10, 0, 0, 5)",
            (f"{stale}T02:00:00Z", f"{stale}T02:01:00Z"),
        )
        conn.commit()
    finally:
        conn.close()

    after = client.get("/v1/admin/stats?days=7", headers=headers).json()["data"]
    assert after["jobs"]["succeeded"] == before.get("succeeded", 0) + 1
    assert after["jobs"]["failed"] == before.get("failed", 0) + 1
    finished = after["jobs"]["succeeded"] + after["jobs"]["failed"]
    assert after["success_rate"] == round(after["jobs"]["succeeded"] * 100.0 / finished, 1)


def test_admin_stats_window_boundary_uses_local_midnight(client):
    """窗口首日本地 00:00–08:00 的作业/运行/活跃用户都不能因 UTC 边界被漏计。"""
    _create_user(client, "root", role="admin")
    _create_user(client, "viewer")
    headers = _auth(_token(client, "root"))
    before = client.get("/v1/admin/stats?days=7", headers=headers).json()["data"]

    today = accounts._local_today()
    first_day = today - dt.timedelta(days=6)
    # runs / analysis_jobs 生产格式是空格无 Z；operation_logs 是 ISO「T…Z」，两套都要守。
    boundary_local = dt.datetime.combine(first_day, dt.time(hour=1), tzinfo=accounts._CST)
    outside_local = dt.datetime.combine(
        first_day - dt.timedelta(days=1), dt.time(hour=23), tzinfo=accounts._CST
    )
    boundary_space = boundary_local.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    outside_space = outside_local.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    boundary_iso = boundary_local.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    settings = client.app.state.settings
    conn = db.connect(settings.db_path)
    try:
        viewer = accounts.get_user_by_username(conn, "viewer")
        assert viewer is not None
        conn.execute(
            "INSERT INTO runs (started_at, finished_at, mode, scanned, analyzed, skipped, errors)"
            " VALUES (?, ?, 'live', 1, 1, 0, 0)",
            (boundary_space, boundary_space),
        )
        conn.execute(
            "INSERT INTO runs (started_at, finished_at, mode, scanned, analyzed, skipped, errors)"
            " VALUES (?, ?, 'live', 1, 0, 0, 5)",
            (outside_space, outside_space),
        )
        conn.execute(
            "INSERT INTO analysis_jobs (id, status, payload, created_at, finished_at)"
            " VALUES ('boundary-job', 'succeeded', '{}', ?, ?)",
            (boundary_space, boundary_space),
        )
        conn.execute(
            "INSERT INTO analysis_jobs (id, status, payload, created_at, finished_at)"
            " VALUES ('outside-job', 'failed', '{}', ?, ?)",
            (outside_space, outside_space),
        )
        conn.execute(
            "INSERT INTO operation_logs (user_id, username, action, created_at)"
            " VALUES (?, 'viewer', 'auth.login', ?)",
            (int(viewer["id"]), boundary_iso),
        )
        conn.commit()
    finally:
        conn.close()

    after = client.get("/v1/admin/stats?days=7", headers=headers).json()["data"]
    assert after["jobs"]["succeeded"] == before["jobs"].get("succeeded", 0) + 2
    assert after["jobs"]["failed"] == before["jobs"].get("failed", 0)
    assert after["totals"]["active_users"] == before["totals"]["active_users"] + 1


def test_admin_sites_proxy_passes_upstream_422(client, monkeypatch):
    """crawler 的参数校验错误要原样透传，不能被折成 502。"""
    _create_user(client, "root", role="admin")
    headers = _auth(_token(client, "root"))

    def reject(self, name, payload):
        raise UpstreamError("站点名称不合法", status_code=422, fallbackable=False)

    monkeypatch.setattr("app.main.NewsClient.update_site", reject)
    resp = client.patch("/v1/admin/sites/demo", json={"enabled": False}, headers=headers)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == 422


def test_notifications_are_isolated_per_user(client):
    _create_user(client, "u1")
    _create_user(client, "u2")
    token1 = _token(client, "u1")
    token2 = _token(client, "u2")

    settings = client.app.state.settings
    conn = db.connect(settings.db_path)
    try:
        db.create_notification(
            conn,
            article_id="notice-article-1",
            kind="project_application",
            title="新增项目申报公告",
            message="测试通知",
        )
    finally:
        conn.close()

    first = client.get("/v1/notifications", headers=_auth(token1)).json()["meta"]
    assert first["unread"] == 1
    assert client.post("/v1/notifications/read-all", headers=_auth(token1)).status_code == 200
    assert client.get("/v1/notifications", headers=_auth(token1)).json()["meta"]["unread"] == 0
    # 另一用户仍有 1 条未读
    second = client.get("/v1/notifications", headers=_auth(token2)).json()
    assert second["meta"]["unread"] == 1
    assert second["data"][0]["read"] is False
