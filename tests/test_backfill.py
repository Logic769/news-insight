"""Tests for the body backfill: it moves text, and it must not get stuck.

The interesting behaviour is not the happy path (copy a body, write it) but the
two ways a naive implementation breaks in production: it can hold the SQLite
write lock across network I/O, and it can get pinned forever on rows that can
never be filled.
"""

from __future__ import annotations

import dataclasses
import shutil
import sqlite3
from pathlib import Path

import pytest

from app import backfill as backfill_mod
from app import db
from app.news_client import UpstreamError
from app.settings import Settings


def _settings(tmp_path: Path, **overrides) -> Settings:
    base = Settings(
        data_dir=tmp_path,
        news_source_mode="live",
        news_base_url="http://upstream.test",
        llm_api_key="",
        api_key="",
    )
    return dataclasses.replace(base, **overrides) if overrides else base


def _seed(db_path: Path, rows: list[tuple[str, str | None, str]]) -> None:
    """rows: (article_id, body, published_at)."""
    db.init_db(db_path)
    conn = db.connect(db_path)
    try:
        for article_id, body, published_at in rows:
            conn.execute(
                "INSERT INTO analyzed_articles (article_id, source, url, title, body,"
                " published_at, content_length, category, category_confidence,"
                " analyzed_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    article_id,
                    "s",
                    "u",
                    "标题 " + article_id,
                    body,
                    published_at,
                    0,
                    "other",
                    0.5,
                    "2026-11-01 00:00:00",
                ),
            )
        conn.commit()
    finally:
        conn.close()


class _ConnTracker:
    """Holds the connection the backfill actually writes through."""

    def __init__(self) -> None:
        self.conn: sqlite3.Connection | None = None


class _FakeClient:
    """Records requests; bodies/errors keyed by article_id.

    `tx_during_calls` samples the *main* connection's `in_transaction` on every
    request. It must be the same connection the backfill writes through: opening a
    second connection would always report `False`, because an uncommitted
    transaction is private to its own connection. (An earlier version of this file
    made exactly that mistake, so the test passed even with the bug present.)
    """

    def __init__(
        self,
        bodies: dict[str, str],
        errors: dict[str, str | tuple[str, int]] | None = None,
        tracker: _ConnTracker | None = None,
    ):
        self.bodies = bodies
        self.errors = errors or {}
        self.tracker = tracker
        self.calls: list[str] = []
        self.tx_during_calls: list[bool] = []

    def get_article(self, article_id: str) -> dict[str, str]:
        self.calls.append(article_id)
        if self.tracker is not None and self.tracker.conn is not None:
            self.tx_during_calls.append(self.tracker.conn.in_transaction)
        if article_id in self.errors:
            spec = self.errors[article_id]
            if isinstance(spec, tuple):
                message, status_code = spec
                raise UpstreamError(message, status_code=status_code)
            raise UpstreamError(spec)
        return {"content": self.bodies.get(article_id, "")}

    def close(self) -> None:
        pass


def _patch_client(
    monkeypatch, client: _FakeClient, tracker: _ConnTracker | None = None
) -> None:
    monkeypatch.setattr(backfill_mod, "NewsClient", lambda settings: client)
    if tracker is not None:
        real_connect = backfill_mod.connect

        def tracking_connect(settings):
            conn = real_connect(settings)
            tracker.conn = conn
            return conn

        monkeypatch.setattr(backfill_mod, "connect", tracking_connect)


class TestBackfill:
    def test_demo_mode_refuses_to_touch_network(self, tmp_path, monkeypatch):
        """demo 模式承诺不访问网络，回填必须拒绝而不是偷偷读 fixture。"""
        settings = _settings(tmp_path, news_source_mode="demo")
        client = _FakeClient({})
        _patch_client(monkeypatch, client)
        with pytest.raises(RuntimeError, match="demo"):
            backfill_mod.backfill_bodies(settings)
        assert client.calls == []

    def test_writes_do_not_span_network_calls(self, tmp_path, monkeypatch):
        """每个 chunk 先取完全部正文，再一次性写库，避免写锁跨过 HTTP。

        两个条件都必须满足才测得出来：

        - batch > 1：batch=1 时每个 chunk 只有一行、写完立刻提交，「边取边写」与
          「先取后写」行为等价，变异测不出来（实测 batch=1 下把 set_body 挪进取数
          循环，本用例仍绿）。
        - 行数 > batch（即**多个** chunk）：只有一个 chunk 时把 conn.commit() 挪到
          chunk 循环外也全绿，而默认 batch=50 时那正是「事务跨过下一批请求」的形态。
          实测 6 行 batch=3 下这种变异会让第二个 chunk 的三个请求都落在未提交事务里。
        """
        settings = _settings(tmp_path)
        _seed(settings.db_path, [
            (f"a{i}", None, f"2026-11-{20 - i:02d} 00:00:00") for i in range(1, 7)
        ])
        tracker = _ConnTracker()
        bodies = {f"a{i}": f"正文{i}" for i in range(1, 7)}
        client = _FakeClient(bodies, tracker=tracker)
        _patch_client(monkeypatch, client, tracker)
        result = backfill_mod.backfill_bodies(settings, limit=10, batch=3)
        assert result["filled"] == 6
        assert sorted(client.calls) == sorted(bodies)
        assert len(client.tx_during_calls) == 6, "探针没有采样到全部请求"
        assert not any(client.tx_during_calls), (
            "网络请求期间不应持有未提交事务：" + repr(client.tx_during_calls)
        )

        conn = db.connect(settings.db_path)
        try:
            got = dict(conn.execute("SELECT article_id, body FROM analyzed_articles"))
        finally:
            conn.close()
        assert got == bodies

    def test_unfillable_rows_do_not_pin_the_window(self, tmp_path, monkeypatch):
        """`--limit 1` 时排在最前的死链不能把窗口钉死。

        这是 CodeRabbit 线程 1 的原始诉求：候选集若只取 `limit` 行，而最新那行
        永远取不到正文（404 / 上游无正文），它不会离开候选集，于是每轮都在重试
        它，更早的可回填文章永远轮不到。只看「同一批内死链不阻断其他行」是不够的
        ——那用 limit=10 就能过，哪怕窗口永远不会前进。

        这里把 limit 压到 1，断言**单次运行内**就能越过两篇不可回填的行摸到
        older。扫描预算与写入上限解耦才可能通过。
        """
        settings = _settings(tmp_path)
        _seed(settings.db_path, [
            ("dead", None, "2026-11-05 00:00:00"),
            ("empty", None, "2026-11-04 00:00:00"),
            ("dead2", None, "2026-11-03 00:00:00"),
            ("older", None, "2026-11-01 00:00:00"),
        ])
        client = _FakeClient(
            {"empty": "   ", "older": "旧正文"},
            errors={
                "dead": ("文章详情 dead 返回 404", 404),
                "dead2": ("文章详情 dead2 返回 410", 410),
            },
        )
        _patch_client(monkeypatch, client)
        result = backfill_mod.backfill_bodies(settings, limit=1, batch=10)
        assert "older" in client.calls, (
            "死链占满了窗口，更早的可回填文章没被访问到：" + repr(client.calls)
        )
        assert result["filled"] == 1
        # 死链（404/410）算 skipped：上游明确说没有，重跑也不会有，
        # 计入 failed 会让「failed > 0 就重跑」永远成立。
        assert result["failed"] == 0
        assert result["skipped"] == 3
        assert result["remaining"] == 3

        conn = db.connect(settings.db_path)
        try:
            got = dict(conn.execute("SELECT article_id, body FROM analyzed_articles"))
        finally:
            conn.close()
        assert got["older"] == "旧正文"
        assert got["dead"] is None
        assert got["empty"] is None

    def test_gone_links_are_skipped_but_transient_errors_stay_failed(
        self, tmp_path, monkeypatch
    ):
        """404/410 是死链，算 skipped；5xx 是临时故障，算 failed。

        两者的运维含义相反：failed > 0 意味着「稍后重跑可能补上」，
        skipped 意味着「这篇上游确实没有正文」。死链若计入 failed，
        持续 404 的行会让 failed 永远 > 0，运维永远等不到停止信号。
        """
        settings = _settings(tmp_path)
        _seed(settings.db_path, [
            ("gone404", None, "2026-11-05 00:00:00"),
            ("gone410", None, "2026-11-04 00:00:00"),
            ("boom", None, "2026-11-03 00:00:00"),
            ("ok", None, "2026-11-02 00:00:00"),
        ])
        client = _FakeClient(
            {"ok": "正文"},
            errors={
                "gone404": ("文章详情 gone404 返回 404", 404),
                "gone410": ("文章详情 gone410 返回 410", 410),
                "boom": ("文章详情 boom 返回 503", 503),
            },
        )
        _patch_client(monkeypatch, client)
        result = backfill_mod.backfill_bodies(settings, limit=10, batch=10)
        assert result["filled"] == 1
        assert result["skipped"] == 2, "404/410 应计入 skipped"
        assert result["failed"] == 1, "503 是临时故障，仍应计入 failed"
        assert result["remaining"] == 3

    def test_candidate_selection_is_deterministic(self, tmp_path, monkeypatch):
        """同名次行在 LIMIT 边界上必须稳定：否则两次运行回填的是不同集合。"""
        settings = _settings(tmp_path)
        same_time = "2026-11-02 00:00:00"
        _seed(settings.db_path, [(f"a{i}", None, same_time) for i in range(5)])
        conn = db.connect(settings.db_path)
        try:
            first = [r["article_id"] for r in backfill_mod._articles_missing_body(conn, 3)]
            second = [r["article_id"] for r in backfill_mod._articles_missing_body(conn, 3)]
        finally:
            conn.close()
        assert first == second
        assert first == ["a4", "a3", "a2"]

    def test_set_body_skips_rows_that_already_have_one(self, tmp_path):
        """并发场景：快照之后的写入不能覆盖别人刚填好的正文。"""
        settings = _settings(tmp_path)
        _seed(settings.db_path, [("a1", "已有正文", "2026-11-01 00:00:00")])
        conn = db.connect(settings.db_path)
        try:
            db.set_body(conn, article_id="a1", body="新正文")
            got = conn.execute("SELECT body FROM analyzed_articles WHERE article_id='a1'").fetchone()[0]
        finally:
            conn.close()
        assert got == "已有正文"

    @pytest.mark.parametrize(
        ("kwargs", "expected"),
        [
            ({"limit": -1}, "limit 不能为负数"),
            ({"batch": 0}, "batch 必须"),
            ({"batch": -5}, "batch 必须"),
        ],
    )
    def test_rejects_limits_that_sqlite_would_misread(
        self, tmp_path, monkeypatch, kwargs, expected
    ):
        """`--limit -1` 不能变成「回填整库」。

        `limit * _SCAN_FACTOR` 对负数仍是负数，而 SQLite 把 `LIMIT -1` 当作**不限**
        ——不拦的话这个参数会得到与字面意思相反的结果。`batch=0` 会让
        `range(0, n, 0)` 抛 ValueError。两者都必须在 mkdir/init_db 与任何请求之前拒绝。

        必须 match 具体文案，不能只 `pytest.raises(ValueError)`：删掉入口校验后，
        `batch=0` 仍会在 `range()` 处抛 ValueError、`client.calls` 也仍为空，光靠
        异常类型会把「入口拒绝」与「跑到一半才炸」判成一样（实测变异 C 下只有
        limit=-1 与 batch=-5 两组变红，batch=0 是假覆盖）。
        """
        settings = _settings(tmp_path)
        _seed(settings.db_path, [("a1", None, "2026-11-01 00:00:00")])
        client = _FakeClient({"a1": "正文一"})
        _patch_client(monkeypatch, client)
        # 让 data_dir 处于「尚未创建」状态，才能验证拒绝发生在 mkdir 之前。
        shutil.rmtree(settings.data_dir, ignore_errors=True)
        args = {"limit": 10, "batch": 5}
        args.update(kwargs)
        with pytest.raises(backfill_mod.BackfillUsageError, match=expected):
            backfill_mod.backfill_bodies(settings, **args)
        assert client.calls == [], "参数不合法时不应发出任何请求"
        assert not settings.data_dir.exists(), "入口校验必须早于 mkdir/init_db"

