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
from collections.abc import Iterable, Sequence
from contextlib import closing
from pathlib import Path
from typing import Any

from app import search as search_mod
from app.tagging import TagDef

LOG = logging.getLogger("news_insight.db")

# SQL 侧的归一不能靠 SQL 自己拼：SQLite 的 lower() 只折 ASCII，把 "Ärger" 折完还是
# "Ärger"，而 Python 的 casefold() 会折成 "ärger" —— 两层判断一旦用不同实现，
# 预筛就会把打分阶段本可命中的行丢掉（用户看到 200 + 空结果，不报错）。
# 所以直接把 search._norm 注册成 SQL 函数，两边跑同一份代码，物理上不可能分叉。
# 代价是每行每列一次 Python 回调：1000 行 × 6000 字正文全表扫描实测 0.34s vs
# 纯 SQL 0.25s，而候选集上限只有 2000 行，这个量级可以接受。
_NORM_FUNC = "py_norm"

# 检索候选集上限：泛词会把整库都捞进来，用它兜住最坏情况下 Python 侧的内存占用。
_CANDIDATE_LIMIT = 2000


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS analyzed_articles (
    article_id      TEXT PRIMARY KEY,
    source          TEXT NOT NULL,
    url             TEXT NOT NULL,
    title           TEXT NOT NULL,
    published_at    TEXT,
    published_at_utc TEXT,
    content_length  INTEGER NOT NULL DEFAULT 0,
    body            TEXT,               -- 正文纯文本；关键词检索用。NULL = 落库时上游未提供（历史行），不改写为空串
    category        TEXT,               -- 一级分类（单选），categories.json 的 id；NULL = 分类功能上线前分析的历史行
    category_confidence REAL,
    category_engine TEXT,               -- llm / rules / none
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

# 依赖新增列的索引不能写在 SCHEMA_SQL 里：executescript 先于补列执行，旧库会因为
# category 列还不存在而直接报错。放在 _migrate_columns 之后建。
_POST_MIGRATION_SQL = """
CREATE INDEX IF NOT EXISTS idx_analyzed_category ON analyzed_articles(category, published_at DESC);
"""

# init_db 只有 CREATE TABLE IF NOT EXISTS，升级前建好的库不会自动长出新列。
# 新增列必须在这里补一次 ADD COLUMN，否则老库上的 INSERT 会因列不存在直接失败。
_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    # (列, 列定义) —— 都落在 analyzed_articles 上
    ("body", "TEXT"),
    ("category", "TEXT"),
    ("category_confidence", "REAL"),
    ("category_engine", "TEXT"),
)


def _load_json(raw: Any) -> Any:
    """库被手工改坏时不要让整个端点 500。"""
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _ensure_norm_func(conn: sqlite3.Connection) -> None:
    """Register `search._norm` as the SQL function `py_norm`.

    Idempotent and cheap: `create_function` overwrites, so calling it per query is
    harmless. Registering here (not in connect()) keeps the dependency one-way -- a
    connection used only for reads never pays for it, and the function always matches
    whatever `search._norm` currently does, including future changes to WS_CHARS.
    """
    conn.create_function(_NORM_FUNC, 1, lambda value: search_mod._norm(value or ""), deterministic=True)


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
        _migrate_columns(conn)
        conn.executescript(_POST_MIGRATION_SQL)
        conn.commit()


def _migrate_columns(conn: sqlite3.Connection) -> None:
    """Idempotent ADD COLUMN for databases created before a column existed.

    SQLite has no ALTER TABLE ... IF NOT EXISTS, so the existing column set is
    read first; adding a column that already exists is the only failure mode
    worth avoiding and it is cheapest to avoid by inspection.
    """
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(analyzed_articles)")}
    for column, decl in _ADDED_COLUMNS:
        if column in existing:
            continue
        conn.execute(f"ALTER TABLE analyzed_articles ADD COLUMN {column} {decl}")
        LOG.info("已为 analyzed_articles 补列: %s", column)


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
    category: str = "",
    category_confidence: float | None = None,
    category_engine: str = "",
    body: str | None = None,
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
            category=category,
            category_confidence=category_confidence,
            category_engine=category_engine,
            body=body,
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
    category: str,
    category_confidence: float | None,
    category_engine: str,
    body: str | None,
    now: str,
) -> None:
    conn.execute(
        """
        INSERT INTO analyzed_articles
            (article_id, source, url, title, published_at, published_at_utc,
             content_length, body, category, category_confidence, category_engine,
             tag_engine, extract_engine, analyzed_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(article_id) DO UPDATE SET
            source=excluded.source, url=excluded.url, title=excluded.title,
            published_at=excluded.published_at, published_at_utc=excluded.published_at_utc,
            content_length=excluded.content_length,
            -- 正文只在本次真的拿到时覆盖：早期行 body 为 NULL，重新分析若上游没返回正文
            -- （详情接口抖动、正文为空），不能被写成 NULL/空串，否则检索会静默丢文章。
            body         = COALESCE(NULLIF(excluded.body, ''), analyzed_articles.body),
            -- 空值表示「这次没做分类」（旧版本重跑、categories.json 被移走后退回单级打标），
            -- 不能拿它覆盖库里已有的分类；明确判成 other 时 category='other'，不是空值，会正常写入。
            category            = COALESCE(excluded.category, analyzed_articles.category),
            category_confidence = COALESCE(excluded.category_confidence, analyzed_articles.category_confidence),
            category_engine     = COALESCE(NULLIF(excluded.category_engine, ''), analyzed_articles.category_engine),
            tag_engine=excluded.tag_engine,
            extract_engine=excluded.extract_engine, analyzed_at=excluded.analyzed_at
        """,
        (article_id, source, url, title, published_at, published_at_utc,
         content_length, body, category or None, category_confidence, category_engine,
         tag_engine, extract_engine, now),
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
    category: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    clauses: list[str] = []
    args: list[Any] = []
    if tag:
        clauses.append("a.article_id IN (SELECT article_id FROM article_tags WHERE tag = ?)")
        args.append(tag)
    # 用 is not None 而不是真值判断：category= 空串是「筛一个不存在的类」，
    # 应该和未知类名一样返回空集，而不是静默退化成「不过滤」
    if category is not None:
        clauses.append("a.category = ?")
        args.append(category)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
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


def category_stats(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """One row per first-level category; unclassified (pre-upgrade) rows are excluded.

    NULL category means "analyzed before categorization existed", which is not
    the same as the 'other' bucket -- folding them together would make the
    'other' count jump on every deployment that predates this feature.
    """
    rows = conn.execute(
        "SELECT category, COUNT(*) AS count, ROUND(AVG(category_confidence), 3) AS avg_confidence "
        "FROM analyzed_articles WHERE category IS NOT NULL AND category <> '' "
        "GROUP BY category ORDER BY count DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def tag_stats(conn: sqlite3.Connection, *, category: str | None = None) -> list[dict[str, Any]]:
    # 与 list_analyzed 一致：空串是「筛一个不存在的类」，返回空集而不是全量
    if category is not None:
        rows = conn.execute(
            "SELECT t.tag, COUNT(*) AS count, ROUND(AVG(t.confidence), 3) AS avg_confidence "
            "FROM article_tags t JOIN analyzed_articles a ON a.article_id = t.article_id "
            "WHERE a.category = ? GROUP BY t.tag ORDER BY count DESC",
            (category,),
        ).fetchall()
        return [dict(r) for r in rows]
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


# ------------------------------------------------------------------ search
def search_candidates(
    conn: sqlite3.Connection,
    *,
    terms: Sequence[str],
    tag_ids: Sequence[str],
    categories: Sequence[str] | None = None,
    limit: int = _CANDIDATE_LIMIT,
) -> list[dict[str, Any]]:
    """Rows that might match, prefiltered in SQL before scoring.

    Scoring needs the title, the body and the tag confidences, so it cannot be
    done in SQL. This function's only job is to keep the Python side from
    loading the whole table: a candidate must either carry one of the queried
    tags or contain one of the terms literally.

    Literal matching uses instr() rather than LIKE on purpose: a keyword is user
    input, and LIKE would let "%" and "_" act as wildcards, so searching for
    "50%" would quietly match every row.

    `limit` is a safety valve, not pagination -- it bounds worst-case memory when
    a one-character keyword matches everything. This query does not ORDER BY: the
    caller re-ranks by relevance, and its tie-breakers (see search_grouped) are
    what make the final order independent of the row order returned here.
    """
    clauses: list[str] = []
    args: list[Any] = []

    signal: list[str] = []
    if tag_ids:
        placeholders = ",".join("?" for _ in tag_ids)
        signal.append("a.article_id IN (SELECT article_id FROM article_tags WHERE tag IN (" + placeholders + "))")
        args.extend(tag_ids)
    # term 与 title/body 都过同一个 _norm：空白（含 \u2003 这类从网页复制的窄空格）
    # 与非 ASCII 大小写（ÖBB / müller）都必须在预筛阶段就被折掉，否则 SQL 阶段丢行，
    # 打分阶段再准也没用。
    _ensure_norm_func(conn)
    for term in terms:
        needle = search_mod._norm(term)
        if not needle:
            continue
        signal.append("(instr(py_norm(a.title), ?) > 0 OR instr(py_norm(a.body), ?) > 0)")
        args.extend([needle, needle])

    if signal:
        clauses.append("(" + " OR ".join(signal) + ")")

    if categories is not None:
        if not categories:
            return []
        placeholders = ",".join("?" for _ in categories)
        clauses.append("COALESCE(a.category, '') IN (" + placeholders + ")")
        args.extend(categories)

    # 没有任何 signal（terms 与 tag_ids 都空）时 where 为空串，会退化成全表扫描。
    # 生产路径走不到：search_grouped 在 query.is_empty 处提前返回空结果。这里保留
    # 该行为而不是返回 []，是因为本函数是公开的候选集查询，"不限条件" 是合理语义，
    # 由调用方负责判空。
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        "SELECT a.article_id, a.source, a.url, a.title, a.body,"
        " a.published_at, a.published_at_utc, a.content_length,"
        " a.category, a.category_confidence, a.category_engine,"
        " a.tag_engine, a.analyzed_at"
        " FROM analyzed_articles a " + where + " LIMIT ?",
        [*args, limit],
    ).fetchall()

    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["tag_confidences"] = {}
        item["tags"] = []
        out.append(item)

    if not out:
        return out

    # 每个候选都要自己的标签置信度：这就是「标签置信度」这一项打分的输入。
    # 分批 IN 查询避免逐行往返，也避开 SQLite 的变量个数上限。
    ids = [r["article_id"] for r in out]
    by_id = {r["article_id"]: r for r in out}
    for chunk_start in range(0, len(ids), 500):
        chunk = ids[chunk_start : chunk_start + 500]
        placeholders = ",".join("?" for _ in chunk)
        for tr in conn.execute(
            "SELECT article_id, tag, confidence, evidence, engine FROM article_tags"
            " WHERE article_id IN (" + placeholders + ")",
            chunk,
        ):
            entry = by_id[tr["article_id"]]
            entry["tag_confidences"][tr["tag"]] = tr["confidence"]
            entry["tags"].append(
                {
                    "tag": tr["tag"],
                    "confidence": tr["confidence"],
                    "evidence": tr["evidence"],
                    "engine": tr["engine"],
                }
            )
    return out




def set_body(conn: sqlite3.Connection, *, article_id: str, body: str, commit: bool = True) -> None:
    """Write only the body column, leaving every derived field untouched.

    The guard mirrors the candidate set used by the backfill command: only rows
    that still have no body are written. Without it, a snapshot taken before a
    concurrent analyse run could overwrite the body that run just wrote with an
    older copy.
    """
    conn.execute(
        "UPDATE analyzed_articles SET body = ?"
        " WHERE article_id = ? AND (body IS NULL OR body = '')",
        (body, article_id),
    )
    if commit:
        conn.commit()


# --------------------------------------------------------------- search

def search_grouped(
    conn: sqlite3.Connection,
    *,
    raw_query: str,
    tags: list[TagDef],
    category_order: Sequence[str] = (),
    categories: Sequence[str] | None = None,
    per_category: int = 20,
    include_body: bool = False,
) -> dict[str, Any]:
    """Score every candidate, then group by category and rank inside each group.

    Returning groups rather than one flat ranked list is the whole point: the
    product asks for five categories as the outer structure, and a flat list
    would force the frontend to re-sort by a score it never receives.
    """
    query = search_mod.resolve_query(raw_query, tags)
    if query.is_empty:
        return {"query": raw_query, "matched_tags": [], "terms": [], "groups": [], "total": 0}

    # 多取一行用来判断「是否还有更多」：只比对 len(rows) == limit 的话，库里恰好
    # 2000 行时会误报截断（其实全取完了），而把 limit 传死又不随将来改动走。
    rows = search_candidates(
        conn,
        terms=query.term_texts,
        tag_ids=query.tag_ids,
        categories=categories,
        limit=_CANDIDATE_LIMIT + 1,
    )
    truncated = len(rows) > _CANDIDATE_LIMIT
    if truncated:
        rows = rows[:_CANDIDATE_LIMIT]
        # 打满上限说明预筛基本没起作用（典型的是一两个泛词），返回的 total 会偏小，
        # 且偏多少无法从响应里看出来。留一条 WARNING 让运维知道该收紧关键词，
        # 而不是把「排序看起来不对」当成打分逻辑的 bug。
        LOG.warning(
            "检索候选集打满上限 %d，结果可能不完整：terms=%d tag_ids=%d",
            _CANDIDATE_LIMIT,
            len(query.term_texts),
            len(query.tag_ids),
        )

    scored: list[tuple[float, dict[str, Any]]] = []
    for row in rows:
        rel = search_mod.score_article(
            query,
            title=row["title"],
            body=row.get("body"),
            tag_confidences=row["tag_confidences"],
        )
        if not search_mod.has_signal(rel):
            continue
        item = dict(row)
        item.pop("tag_confidences", None)
        item["relevance"] = rel.as_dict()
        if not include_body:
            item.pop("body", None)
        scored.append((rel.score, item))

    # 同分兜底：先按发布时间倒序，最后用 article_id 打破完全平局。少了最后这一
    # 层，同分同时间的多篇按 SQLite 返回顺序排，查询计划一变排列就会抖——而前端
    # 拿到的 items[] 顺序就是最终展示顺序，抖动等于用户看到列表顺序会变。
    scored.sort(
        key=lambda pair: (pair[0], pair[1].get("published_at") or "", pair[1]["article_id"]),
        reverse=True,
    )

    buckets: dict[str, list[dict[str, Any]]] = {}
    for _, item in scored:
        buckets.setdefault(item.get("category") or "", []).append(item)

    # 先按调用方给的固定类顺序输出（前端五段布局稳定），再补上不在顺序里
    # 的类（other、以及分类功能上线前的历史 NULL 行）。
    ordered = [c for c in category_order if c in buckets]
    known = set(ordered)
    ordered.extend(sorted(k for k in buckets if k not in known))

    groups = [
        {
            "category": key,
            "count": len(buckets[key]),
            "items": buckets[key][:per_category],
        }
        for key in ordered
    ]
    return {
        "query": raw_query,
        "matched_tags": list(query.tag_ids),
        "terms": list(query.term_texts),
        "groups": groups,
        # 候选集（最多 2000 行）内命中的行数，不是全库真实命中数：泛词被截断时
        # 这个值会偏小。前端展示「共 N 条」时应理解为「本次返回范围内的条数」。
        "total": len(scored),
    }
