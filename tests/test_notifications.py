"""站内消息提醒：新分析出的项目申报公告只提醒一次，已读状态可查询可清空。"""

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

    unread = client.get("/v1/notifications", params={"unread_only": "true"}, headers=HEADERS)
    assert all(not item["read"] for item in unread.json()["data"])

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
