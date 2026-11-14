"""推荐分与首页待办的纯逻辑用例。"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3

from app import recommend


def _row(**overrides):
    row = {
        "article_id": "a1",
        "category_confidence": 0.9,
        "content_length": 1800,
        "published_at": "2026-03-01T09:00:00+08:00",
        "published_at_utc": "2026-03-01T01:00:00Z",
        "tags": [{"tag": "project_application", "confidence": 0.9}],
        "industry": "next_gen_it",
        "category": "project_application",
        "source": "most_kjjh",
    }
    row.update(overrides)
    return row


def test_content_score_prefers_fresh_confident_rows():
    now = dt.datetime(2026, 3, 10, tzinfo=dt.timezone.utc)
    fresh = recommend.content_score(_row(), now=now)
    stale = recommend.content_score(
        _row(
            article_id="a2",
            category_confidence=0.3,
            content_length=100,
            published_at="2025-01-01T00:00:00+08:00",
            published_at_utc="2025-01-01T00:00:00Z",
            tags=[],
        ),
        now=now,
    )
    assert fresh > stale
    assert 0.0 <= stale <= fresh <= 1.0


def test_profile_boosts_matching_industry():
    now = dt.datetime(2026, 3, 10, tzinfo=dt.timezone.utc)
    profile = recommend.Profile(
        industries={"next_gen_it"},
        categories=set(),
        sources=set(),
        tag_weights={},
    )
    matched, reason = recommend.score_row(_row(), profile, now=now)
    unmatched, _ = recommend.score_row(_row(industry="other"), profile, now=now)
    assert matched > unmatched
    assert "行业" in reason


def test_upcoming_deadlines_filters_expired_and_sorts():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE analyzed_articles (
            article_id TEXT PRIMARY KEY, title TEXT, source TEXT, url TEXT,
            published_at TEXT, category TEXT, category_confidence REAL
        );
        CREATE TABLE extractions (
            article_id TEXT, schema_id TEXT, field TEXT, value_json TEXT, raw_text TEXT,
            PRIMARY KEY (article_id, schema_id, field)
        );
        """
    )
    conn.execute(
        "INSERT INTO analyzed_articles VALUES (?,?,?,?,?,?,?)",
        ("late", "晚的", "s", "u1", "2026-03-01", "project_application", 0.9),
    )
    conn.execute(
        "INSERT INTO analyzed_articles VALUES (?,?,?,?,?,?,?)",
        ("soon", "早的", "s", "u2", "2026-03-01", "project_application", 0.9),
    )
    conn.execute(
        "INSERT INTO analyzed_articles VALUES (?,?,?,?,?,?,?)",
        ("gone", "过期", "s", "u3", "2026-03-01", "project_application", 0.9),
    )
    for article_id, value in (("late", "2026-03-20"), ("soon", "2026-03-05"), ("gone", "2026-02-20")):
        conn.execute(
            "INSERT INTO extractions VALUES (?,?,?,?,?)",
            (article_id, "project_application", "deadline", json.dumps(value), value),
        )
    items = recommend.upcoming_deadlines(conn, today=dt.date(2026, 3, 1))
    assert [item["article_id"] for item in items] == ["soon", "late"]
    assert items[0]["days_left"] == 4
    # 首页卡片显示的是全部未到期条数，不是当页条数；接口 meta.total 用它。
    assert recommend.count_upcoming_deadlines(conn, today=dt.date(2026, 3, 1)) == 2
    conn.close()
