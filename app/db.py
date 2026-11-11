"""Dedicated SQLite store for analysis results.

Deliberately a separate file from the crawler's news.db: two processes writing
one SQLite database is the classic way to manufacture `database is locked`
errors, and the crawler already pins itself to a single writer. Linkage back to
the crawler is by `article_id` (the upstream `id` field).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable

LOG = logging.getLogger("news_insight.db")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS analyzed_articles (
    article_id      TEXT PRIMARY KEY,
    source          TEXT NOT NULL,
    url             TEXT NOT NULL,
    title           TEXT NOT NULL,
    published_at    TEXT,
    published_at_utc TEXT,
    content_length  INTEGER NOT NULL DEFAULT 0,
    tag_engine      TEXT,
    extract_engine  TEXT,
    analyzed_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS article_tags (
    article_id  TEXT NOT NULL,
    tag         TEXT NOT NULL,
    confidence  REAL NOT NULL,
    evidence    TEXT,
    engine      TEXT NOT NULL,
    PRIMARY KEY (article_id, tag)
);

CREATE TABLE IF NOT EXISTS extractions (
    article_id  TEXT NOT NULL,
    schema_id   TEXT NOT NULL,
    field       TEXT NOT NULL,
    value_json  TEXT NOT NULL,
    raw_text    TEXT,
    confidence  REAL NOT NULL,
    verified    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (article_id, schema_id, field)
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    mode        TEXT NOT NULL,
    scanned     INTEGER NOT NULL DEFAULT 0,
    analyzed    INTEGER NOT NULL DEFAULT 0,
    skipped     INTEGER NOT NULL DEFAULT 0,
    errors      INTEGER NOT NULL DEFAULT 0,
    detail      TEXT
);

-- 清单是抽取结果的派生视图：单独成表，重新生成不会碰事实表。
CREATE TABLE IF NOT EXISTS article_checklists (
    article_id   TEXT PRIMARY KEY,
    engine       TEXT NOT NULL,
    items_json   TEXT NOT NULL,
    generated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tags_tag ON article_tags(tag);
CREATE INDEX IF NOT EXISTS idx_analyzed_published ON analyzed_articles(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_extractions_schema ON extractions(schema_id, field);
"""


def _load_json(raw: Any) -> Any:
    """库被手工改坏时不要让整个端点 500。"""
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(db_path: Path) -> None:
    # sqlite3 的上下文管理器只 commit/rollback、不 close，必须用 closing 兜住句柄
    with closing(connect(db_path)) as conn:
        conn.executescript(SCHEMA_SQL)
        conn.commit()


def save_analysis(
    conn: sqlite3.Connection,
    *,
    article_id: str,
    source: str,
    url: str,
    title: str,
    published_at: str | None,
    published_at_utc: str | None,
    content_length: int,
    tag_engine: str,
    extract_engine: str,
    tags: Iterable[tuple[str, float, str, str]],
    extractions: Iterable[tuple[str, str, Any, str, float, bool]],
) -> None:
    """Replace any previous analysis for this article (idempotent re-runs).

    整体在一个事务里完成；中途失败立刻回滚，避免调用方吞掉异常后，
    下一篇的 commit 把这里已执行的 DELETE 一起提交，留下「有主行、无标签」的脏状态。
    """
    now = _now()
    try:
        _replace_analysis(
            conn,
            article_id=article_id,
            source=source,
            url=url,
            title=title,
            published_at=published_at,
            published_at_utc=published_at_utc,
            content_length=content_length,
            tag_engine=tag_engine,
            extract_engine=extract_engine,
            tags=tags,
            extractions=extractions,
            now=now,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _replace_analysis(
    conn: sqlite3.Connection,
    *,
    article_id: str,
    source: str,
    url: str,
    title: str,
    published_at: str | None,
    published_at_utc: str | None,
    content_length: int,
    tag_engine: str,
    extract_engine: str,
    tags: Iterable[tuple[str, float, str, str]],
    extractions: Iterable[tuple[str, str, Any, str, float, bool]],
    now: str,
) -> None:
    conn.execute(
        """
        INSERT INTO analyzed_articles
            (article_id, source, url, title, published_at, published_at_utc,
             content_length, tag_engine, extract_engine, analyzed_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(article_id) DO UPDATE SET
            source=excluded.source, url=excluded.url, title=excluded.title,
            published_at=excluded.published_at, published_at_utc=excluded.published_at_utc,
            content_length=excluded.content_length, tag_engine=excluded.tag_engine,
            extract_engine=excluded.extract_engine, analyzed_at=excluded.analyzed_at
        """,
        (article_id, source, url, title, published_at, published_at_utc,
         content_length, tag_engine, extract_engine, now),
    )
    conn.execute("DELETE FROM article_tags WHERE article_id = ?", (article_id,))
    conn.executemany(
        "INSERT INTO article_tags (article_id, tag, confidence, evidence, engine) VALUES (?,?,?,?,?)",
        [(article_id, tag, conf, evidence, engine) for tag, conf, evidence, engine in tags],
    )
    conn.execute("DELETE FROM extractions WHERE article_id = ?", (article_id,))
    conn.executemany(
        """INSERT INTO extractions
           (article_id, schema_id, field, value_json, raw_text, confidence, verified)
           VALUES (?,?,?,?,?,?,?)""",
        [
            (article_id, schema_id, fname, json.dumps(value, ensure_ascii=False), raw, conf, int(verified))
            for schema_id, fname, value, raw, conf, verified in extractions
        ],
    )


def is_analyzed(conn: sqlite3.Connection, article_id: str) -> bool:
    row = conn.execute("SELECT 1 FROM analyzed_articles WHERE article_id = ?", (article_id,)).fetchone()
    return row is not None


def record_run_start(conn: sqlite3.Connection, mode: str) -> int:
    cur = conn.execute("INSERT INTO runs (started_at, mode) VALUES (?,?)", (_now(), mode))
    conn.commit()
    return int(cur.lastrowid)


def record_run_finish(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    scanned: int,
    analyzed: int,
    skipped: int,
    errors: int,
    detail: str = "",
) -> None:
    conn.execute(
        "UPDATE runs SET finished_at=?, scanned=?, analyzed=?, skipped=?, errors=?, detail=? WHERE id=?",
        (_now(), scanned, analyzed, skipped, errors, detail[:2000], run_id),
    )
    conn.commit()


def list_analyzed(
    conn: sqlite3.Connection,
    *,
    tag: str | None,
    limit: int,
    offset: int,
) -> tuple[list[dict[str, Any]], int]:
    where, args = "", []
    if tag:
        where = "WHERE a.article_id IN (SELECT article_id FROM article_tags WHERE tag = ?)"
        args.append(tag)
    total = conn.execute(f"SELECT COUNT(*) FROM analyzed_articles a {where}", args).fetchone()[0]
    rows = conn.execute(
        f"""SELECT a.* FROM analyzed_articles a {where}
            ORDER BY COALESCE(a.published_at, a.analyzed_at) DESC
            LIMIT ? OFFSET ?""",
        [*args, limit, offset],
    ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["tags"] = [
            {"tag": r["tag"], "confidence": r["confidence"], "engine": r["engine"]}
            for r in conn.execute(
                "SELECT tag, confidence, engine FROM article_tags WHERE article_id = ? ORDER BY confidence DESC",
                (row["article_id"],),
            )
        ]
        out.append(item)
    return out, int(total)


def get_article(conn: sqlite3.Connection, article_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM analyzed_articles WHERE article_id = ?", (article_id,)).fetchone()
    if row is None:
        return None
    item = dict(row)
    item["tags"] = [
        {"tag": r["tag"], "confidence": r["confidence"], "evidence": r["evidence"], "engine": r["engine"]}
        for r in conn.execute(
            "SELECT tag, confidence, evidence, engine FROM article_tags WHERE article_id = ? ORDER BY confidence DESC",
            (article_id,),
        )
    ]
    ext: dict[str, dict[str, Any]] = {}
    for r in conn.execute(
        "SELECT schema_id, field, value_json, raw_text, confidence, verified FROM extractions WHERE article_id = ?",
        (article_id,),
    ):
        ext.setdefault(r["schema_id"], {})[r["field"]] = {
            "value": _load_json(r["value_json"]),
            "raw_text": r["raw_text"],
            "confidence": r["confidence"],
            "verified": bool(r["verified"]),
        }
    item["extractions"] = ext
    return item


def save_checklist(
    conn: sqlite3.Connection,
    *,
    article_id: str,
    engine: str,
    items: Iterable[dict[str, Any]],
) -> None:
    """Insert or replace one article's checklist (idempotent re-runs)."""
    try:
        conn.execute(
            """
            INSERT INTO article_checklists (article_id, engine, items_json, generated_at)
            VALUES (?,?,?,?)
            ON CONFLICT(article_id) DO UPDATE SET
                engine=excluded.engine, items_json=excluded.items_json,
                generated_at=excluded.generated_at
            """,
            (article_id, engine, json.dumps(list(items), ensure_ascii=False), _now()),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def delete_checklist(conn: sqlite3.Connection, article_id: str) -> None:
    """Drop a stale checklist when a re-analysis no longer triggers the schema."""
    try:
        conn.execute("DELETE FROM article_checklists WHERE article_id = ?", (article_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def get_checklist(conn: sqlite3.Connection, article_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT engine, items_json, generated_at FROM article_checklists WHERE article_id = ?",
        (article_id,),
    ).fetchone()
    if row is None:
        return None
    items = _load_json(row["items_json"])
    if not isinstance(items, list):
        LOG.warning("清单 JSON 损坏，按空清单处理 article_id=%s", article_id)
        items = []
    # 旧版清单没有 verified 字段：按未核验处理，避免下游把历史数据当已核验事实。
    items = [{**item, "verified": bool(item.get("verified", False))} if isinstance(item, dict) else item for item in items]
    return {
        "engine": row["engine"],
        "items": items,
        "generated_at": row["generated_at"],
    }


def tag_stats(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT tag, COUNT(*) AS count, ROUND(AVG(confidence), 3) AS avg_confidence "
        "FROM article_tags GROUP BY tag ORDER BY count DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def list_extractions(
    conn: sqlite3.Connection,
    *,
    schema_id: str,
    field: str | None,
    due_before: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Flat view of extracted facts, joined back to the article metadata."""
    sql = """
        SELECT e.article_id, a.title, a.source, a.url, a.published_at, a.published_at_utc,
               e.schema_id, e.field, e.value_json, e.raw_text, e.confidence, e.verified
        FROM extractions e JOIN analyzed_articles a ON a.article_id = e.article_id
        WHERE e.schema_id = ?
    """
    args: list[Any] = [schema_id]
    if field:
        sql += " AND e.field = ?"
        args.append(field)
    if due_before:
        # 用子查询而不是再加一个 e.field='deadline'：否则 field 与 due_before
        # 同时传入时条件互斥（e.field='materials' AND e.field='deadline'）恒空集。
        # 未指定 field 时默认只看截止日期这一行，避免同一篇文章的多个字段行重复返回。
        if not field:
            sql += " AND e.field = 'deadline'"
        sql += (
            " AND e.article_id IN (SELECT d.article_id FROM extractions d"
            " WHERE d.schema_id = ? AND d.field = 'deadline'"
            " AND json_extract(d.value_json, '$') <= ?)"
        )
        args.extend([schema_id, due_before])
    sql += " ORDER BY COALESCE(a.published_at, a.analyzed_at) DESC LIMIT ?"
    args.append(limit)
    out = []
    for r in conn.execute(sql, args):
        item = dict(r)
        item["value"] = _load_json(item.pop("value_json"))
        item["verified"] = bool(item["verified"])
        out.append(item)
    return out


def list_runs(conn: sqlite3.Connection, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]
