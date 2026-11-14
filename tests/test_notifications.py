"""站内消息提醒：新分析出的项目申报公告只提醒一次，已读状态可查询可清空。"""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import create_app
from app.pipeline import _AnalysisResult, _is_project_notice
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


HEADERS = {"X-API-Key": "secret"}


def test_notifications_created_for_project_articles(client):
    assert client.post("/v1/analyze", params={"limit": 4}, headers=HEADERS).status_code == 200

    resp = client.get("/v1/notifications", headers=HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    items = body["data"]
    meta = body["meta"]
    assert meta["total"] >= 1
    assert meta["unread"] == meta["total"]
    assert meta["returned"] == len(items)
    first = items[0]
    assert first["article_id"]
    assert first["kind"] == "project_application"
    assert first["title"] == "新增项目申报公告"
    assert first["message"]
    assert first["read"] is False
    # 带 Z 的 UTC 时间，浏览器能正确解析成相对时间
    assert first["created_at"].endswith("Z")
    assert first["url"]


def test_notifications_mark_read_and_read_all(client):
    client.post("/v1/analyze", params={"limit": 4}, headers=HEADERS)
    before = client.get("/v1/notifications", headers=HEADERS).json()["meta"]
    assert before["unread"] >= 1

    first_id = client.get("/v1/notifications", headers=HEADERS).json()["data"][0]["id"]
    read = client.post(f"/v1/notifications/{first_id}/read", headers=HEADERS)
    assert read.status_code == 200
    assert read.json()["data"]["read"] is True
    # 重复标记已读不报错，未读数不会变成负值
    assert client.post(f"/v1/notifications/{first_id}/read", headers=HEADERS).status_code == 200
    after_one = client.get("/v1/notifications", headers=HEADERS).json()["meta"]
    assert after_one["unread"] == before["unread"] - 1

    unread = client.get(
        "/v1/notifications", params={"unread_only": "true", "limit": 100}, headers=HEADERS
    )
    unread_body = unread.json()
    assert all(not item["read"] for item in unread_body["data"])
    # unread_only 时 total 与列表同口径；unread 仍可用于角标
    assert unread_body["meta"]["total"] == unread_body["meta"]["unread"] == len(
        unread_body["data"]
    )

    all_read = client.post("/v1/notifications/read-all", headers=HEADERS)
    assert all_read.status_code == 200
    assert all_read.json()["data"]["updated"] == after_one["unread"]
    assert client.get("/v1/notifications", headers=HEADERS).json()["meta"]["unread"] == 0

    missing = client.post("/v1/notifications/999999/read", headers=HEADERS)
    assert missing.status_code == 404


def test_force_reanalysis_does_not_duplicate_notifications(client):
    client.post("/v1/analyze", params={"limit": 4}, headers=HEADERS)
    first = client.get("/v1/notifications", headers=HEADERS).json()["meta"]["total"]
    assert first >= 1

    client.post("/v1/analyze", params={"limit": 4, "force": "true"}, headers=HEADERS)
    second = client.get("/v1/notifications", headers=HEADERS).json()["meta"]["total"]
    assert second == first


def test_create_notification_is_idempotent_and_keeps_first_read_time(tmp_path):
    db_path = tmp_path / "notifications.db"
    db.init_db(db_path)
    conn = db.connect(db_path)
    try:
        assert db.create_notification(
            conn, article_id="a1", kind="project_application", title="标题", message="消息"
        ) is True
        assert db.create_notification(
            conn, article_id="a1", kind="project_application", title="标题", message="消息"
        ) is False
        assert conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 1
        notification_id = conn.execute("SELECT id FROM notifications LIMIT 1").fetchone()[0]
        first = db.mark_notification_read(conn, notification_id)
        second = db.mark_notification_read(conn, notification_id)
        assert first is not None and second is not None
        assert first["read_at"] == second["read_at"]
        assert db.mark_notification_read(conn, notification_id + 999) is None
    finally:
        conn.close()


def test_project_notice_ignores_zero_confidence_tag():
    result = _AnalysisResult(
        article=None,
        category="other",
        hits=[SimpleNamespace(tag_id="project_application", confidence=0.0)],
    )
    assert not _is_project_notice(result)
    result.hits[0].confidence = 0.6
    assert _is_project_notice(result)
