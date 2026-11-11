"""LLM 客户端：连接复用与 429/5xx 指数退避。"""

from __future__ import annotations

from dataclasses import replace

from app import llm as llm_mod
from app.llm import LLMClient
from app.settings import load_settings


class _Resp:
    def __init__(self, status_code, payload=None, text="", headers=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text
        self.headers = headers or {}

    def json(self):
        return self._payload


class _FakeHttp:
    def __init__(self):
        self.calls = 0

    def post(self, *args, **kwargs):
        self.calls += 1
        if self.calls < 3:
            return _Resp(429, text="rate limited")
        return _Resp(200, {"choices": [{"message": {"content": '{"ok": true}'}}]})

    def close(self):
        pass


def test_chat_json_retries_429_then_succeeds(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    settings = replace(load_settings(), llm_api_key="x", llm_base_url="http://llm.test")
    monkeypatch.setattr(llm_mod.time, "sleep", lambda _seconds: None)
    client = LLMClient(settings)
    fake = _FakeHttp()
    client._client = fake
    try:
        assert client.chat_json("system", "user") == {"ok": True}
        assert fake.calls == 3
    finally:
        client.close()


def test_chat_json_retries_non_object_json_then_succeeds(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    settings = replace(load_settings(), llm_api_key="x", llm_base_url="http://llm.test")
    monkeypatch.setattr(llm_mod.time, "sleep", lambda _seconds: None)

    class ListThenObject(_FakeHttp):
        def post(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return _Resp(200, ["not", "an", "object"])
            return _Resp(200, {"choices": [{"message": {"content": '{"ok": true}'}}]})

    client = LLMClient(settings)
    fake = ListThenObject()
    client._client = fake
    try:
        assert client.chat_json("system", "user") == {"ok": True}
        assert fake.calls == 2
    finally:
        client.close()


def test_chat_json_does_not_retry_401(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    settings = replace(load_settings(), llm_api_key="x", llm_base_url="http://llm.test")

    class Unauthorized(_FakeHttp):
        def post(self, *args, **kwargs):
            self.calls += 1
            return _Resp(401, text="bad key")

    client = LLMClient(settings)
    fake = Unauthorized()
    client._client = fake
    try:
        try:
            client.chat_json("system", "user")
        except Exception as exc:
            assert "401" in str(exc)
        else:
            raise AssertionError("401 应当直接抛错")
        assert fake.calls == 1
    finally:
        client.close()
