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
from collections.abc import Iterable
from contextlib import closing
from pathlib import Path
from typing import Any

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

-- 异步分析作业：API 提交后立即返回，后台线程执行；状态落库，进程重启后能看出中断。
CREATE TABLE IF NOT EXISTS analysis_jobs (
    id          TEXT PRIMARY KEY,
    status      TEXT NOT NULL,   -- queued/running/succeeded/failed
    payload     TEXT NOT NULL,   -- {"limit":..,"force":..,"concurrency":..}
    result      TEXT,
    error       TEXT,
    created_at  TEXT NOT NULL,
    started_at  TEXT,
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_tags_tag ON article_tags(tag);
CREATE INDEX IF NOT EXISTS idx_analysis_jobs_status ON analysis_jobs(status, created_at);
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
    # 先设 busy_timeout，再只在非 WAL 时切模式：每次连接都执行 journal_mode=WAL
    # 在并发读写下会多拿一次写锁，且对已是 WAL 的库没有收益。
    conn.execute("PRAGMA busy_timeout=30000")
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    if str(mode).lower() != "wal":
        conn.execute("PRAGMA journal_mode=WAL")
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
    commit: bool = True,
) -> None:
    """Replace any previous analysis for this article (idempotent re-runs).

    使用 SAVEPOINT 而不是整库 rollback：批量写入时一篇失败只回滚这一篇，
    不能让同一批里已经写好的文章被调用方的下一次 commit 连带回滚。
    commit=False 供批量事务使用，由调用方统一提交。
    """
    now = _now()
    # SAVEPOINT 在无外层事务时是“最外层”，RELEASE 会立即提交；
    # commit=False 的批量场景必须先显式开事务，否则批量提交退化为逐篇提交。
    if not commit and not conn.in_transaction:
        conn.execute("BEGIN")
    conn.execute("SAVEPOINT save_analysis")
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
    except Exception:
        conn.execute("ROLLBACK TO save_analysis")
        conn.execute("RELEASE save_analysis")
        raise
    else:
        conn.execute("RELEASE save_analysis")
        if commit:
            conn.commit()


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


def analyzed_ids(conn: sqlite3.Connection) -> set[str]:
    """一次取全量已分析 ID，避免 10000 篇时逐篇 SELECT。"""
    return {
        str(row[0])
        for row in conn.execute("SELECT article_id FROM analyzed_articles").fetchall()
    }


def record_run_start(conn: sqlite3.Connection, mode: str) -> int:
    cur = conn.execute("INSERT INTO runs (started_at, mode) VALUES (?,?)", (_now(), mode))
    conn.commit()
    # lastrowid 在 sqlite3 的类型存根里是 int | None；INSERT 成功后必然有值。
    assert cur.lastrowid is not None
    return cur.lastrowid


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


# ------------------------------------------------------------------ jobs
def create_analysis_job(conn: sqlite3.Connection, *, job_id: str, payload: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO analysis_jobs (id, status, payload, created_at) VALUES (?,?,?,?)",
        (job_id, "queued", json.dumps(payload, ensure_ascii=False, sort_keys=True), _now()),
    )
    conn.commit()


def update_analysis_job(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    status: str | None = None,
    result: dict[str, Any] | None = None,
    error: str | None = None,
    started: bool = False,
    finished: bool = False,
) -> None:
    sets: list[str] = []
    args: list[Any] = []
    if status is not None:
        sets.append("status = ?")
        args.append(status)
    if result is not None:
        sets.append("result = ?")
        args.append(json.dumps(result, ensure_ascii=False))
    if error is not None:
        sets.append("error = ?")
        args.append(error[:2000])
    if started:
        sets.append("started_at = ?")
        args.append(_now())
    if finished:
        sets.append("finished_at = ?")
        args.append(_now())
    if not sets:
        return
    args.append(job_id)
    conn.execute(f"UPDATE analysis_jobs SET {', '.join(sets)} WHERE id = ?", args)
    conn.commit()


def _job_row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    for key in ("payload", "result"):
        if item.get(key):
            item[key] = _load_json(item[key])
    return item


def get_analysis_job(conn: sqlite3.Connection, job_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM analysis_jobs WHERE id = ?", (job_id,)).fetchone()
    return _job_row(row) if row is not None else None


def list_analysis_jobs(conn: sqlite3.Connection, limit: int = 20) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM analysis_jobs ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [_job_row(r) for r in rows]


def find_active_job(conn: sqlite3.Connection, payload: dict[str, Any]) -> dict[str, Any] | None:
    """Same-parameter queued/running job reuse: double-clicking POST must not burn tokens twice."""
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    row = conn.execute(
        "SELECT * FROM analysis_jobs WHERE status IN ('queued','running') AND payload = ?"
        " ORDER BY created_at LIMIT 1",
        (raw,),
    ).fetchone()
    return _job_row(row) if row is not None else None


def count_active_analysis_jobs(conn: sqlite3.Connection) -> int:
    """queued + running：用于 POST /v1/jobs 的排队上限保护。"""
    row = conn.execute(
        "SELECT COUNT(*) FROM analysis_jobs WHERE status IN ('queued','running')"
    ).fetchone()
    return int(row[0])


def mark_stale_jobs_failed(conn: sqlite3.Connection) -> int:
    """The in-process executor cannot resume after a restart; surface that instead of a silent stuck job."""
    cur = conn.execute(
        "UPDATE analysis_jobs SET status='failed', error=COALESCE(error, '进程重启，任务中断'), finished_at=?"
        " WHERE status IN ('queued','running')",
        (_now(),),
    )
    conn.commit()
    return int(cur.rowcount)


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
    commit: bool = True,
) -> None:
    """Insert or replace one article's checklist (idempotent re-runs)."""
    if not commit and not conn.in_transaction:
        conn.execute("BEGIN")
    conn.execute("SAVEPOINT save_checklist")
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
    except Exception:
        conn.execute("ROLLBACK TO save_checklist")
        conn.execute("RELEASE save_checklist")
        raise
    else:
        conn.execute("RELEASE save_checklist")
        if commit:
            conn.commit()


def delete_checklist(conn: sqlite3.Connection, article_id: str, *, commit: bool = True) -> None:
    """Drop a stale checklist when a re-analysis no longer triggers the schema."""
    if not commit and not conn.in_transaction:
        conn.execute("BEGIN")
    conn.execute("SAVEPOINT delete_checklist")
    try:
        conn.execute("DELETE FROM article_checklists WHERE article_id = ?", (article_id,))
    except Exception:
        conn.execute("ROLLBACK TO delete_checklist")
        conn.execute("RELEASE delete_checklist")
        raise
    else:
        conn.execute("RELEASE delete_checklist")
        if commit:
            conn.commit()


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


# --------------------------------------------------------------- retention
def _retention_cutoff(retention_days: int) -> str:
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=retention_days)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def prune_old_data(
    conn: sqlite3.Connection, *, retention_days: int, dry_run: bool = False
) -> dict[str, Any]:
    """Delete analysis results older than retention_days.

    SQLite has no foreign keys here, so child rows are removed explicitly before
    the article row. All deletes share one transaction; a failure leaves nothing
    half-deleted. VACUUM is a separate step because it cannot run in a transaction.
    """
    cutoff = _retention_cutoff(retention_days)
    article_subquery = "SELECT article_id FROM analyzed_articles WHERE analyzed_at < ?"
    tables = ("article_tags", "extractions", "article_checklists")
    counts: dict[str, int] = {}
    try:
        for table in tables:
            if dry_run:
                counts[table] = int(
                    conn.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE article_id IN ({article_subquery})",
                        (cutoff,),
                    ).fetchone()[0]
                )
            else:
                cur = conn.execute(
                    f"DELETE FROM {table} WHERE article_id IN ({article_subquery})", (cutoff,)
                )
                counts[table] = int(cur.rowcount)
        if dry_run:
            counts["analyzed_articles"] = int(
                conn.execute(
                    "SELECT COUNT(*) FROM analyzed_articles WHERE analyzed_at < ?", (cutoff,)
                ).fetchone()[0]
            )
            counts["runs"] = int(
                conn.execute("SELECT COUNT(*) FROM runs WHERE started_at < ?", (cutoff,)).fetchone()[0]
            )
            counts["analysis_jobs"] = int(
                conn.execute(
                    "SELECT COUNT(*) FROM analysis_jobs"
                    " WHERE status IN ('succeeded','failed')"
                    " AND COALESCE(finished_at, created_at) < ?",
                    (cutoff,),
                ).fetchone()[0]
            )
        else:
            counts["analyzed_articles"] = int(
                conn.execute("DELETE FROM analyzed_articles WHERE analyzed_at < ?", (cutoff,)).rowcount
            )
            counts["runs"] = int(
                conn.execute("DELETE FROM runs WHERE started_at < ?", (cutoff,)).rowcount
            )
            counts["analysis_jobs"] = int(
                conn.execute(
                    "DELETE FROM analysis_jobs"
                    " WHERE status IN ('succeeded','failed')"
                    " AND COALESCE(finished_at, created_at) < ?",
                    (cutoff,),
                ).rowcount
            )
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {
        "cutoff": cutoff,
        "retention_days": retention_days,
        "dry_run": dry_run,
        "deleted": counts,
    }


def vacuum(conn: sqlite3.Connection) -> None:
    """Reclaim file space after a large prune. Caller must have no open transaction."""
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("VACUUM")
