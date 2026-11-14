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
import re
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
    body_html       TEXT,               -- 清洗后的正文 HTML；只有详情接口返回，列表不返回
    category        TEXT,               -- 一级分类（单选），categories.json 的 id；NULL = 分类功能上线前分析的历史行
    category_confidence REAL,
    category_engine TEXT,               -- llm / rules / none
    industry        TEXT,               -- 行业领域（单选），industries.json 的 id；NULL = 行业功能上线前分析的历史行
    industry_confidence REAL,
    industry_engine TEXT,               -- llm / rules / none
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

-- 站内消息提醒：目前只承载「新分析出项目申报类公告」这一种事件。
-- article_id 唯一键让同一篇文章重复分析（--force）不会反复冒红点；标题存快照，
-- 因为通知是「当时发生了什么」的记录，不随文章后续改名而改写。
CREATE TABLE IF NOT EXISTS notifications (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id  TEXT NOT NULL UNIQUE,
    kind        TEXT NOT NULL,
    title       TEXT NOT NULL,
    message     TEXT NOT NULL,
    created_at  TEXT NOT NULL,   -- ISO8601 UTC，带 Z 后缀，前端可直接 new Date()
    read_at     TEXT
);

-- 本地账号：密码只存 PBKDF2-HMAC-SHA256 派生值，会话只存 token 的 SHA-256。
-- role 目前只有 admin / user 两档；status 只允许 active / disabled。
CREATE TABLE IF NOT EXISTS users (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    username           TEXT NOT NULL UNIQUE,
    password_hash      TEXT NOT NULL,
    password_salt      TEXT NOT NULL,
    password_iterations INTEGER NOT NULL DEFAULT 120000,
    display_name       TEXT NOT NULL DEFAULT '',
    department         TEXT NOT NULL DEFAULT '',
    role               TEXT NOT NULL DEFAULT 'user',
    status             TEXT NOT NULL DEFAULT 'active',
    must_change_password INTEGER NOT NULL DEFAULT 0,
    failed_attempts    INTEGER NOT NULL DEFAULT 0,
    locked_until       TEXT,
    last_login_at      TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash  TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    revoked_at  TEXT
);

-- 通知已读按用户记录；notifications.read_at 是旧版全局已读，保留给无账号的
-- 服务调用与历史数据，新接口一律以本表为准。
CREATE TABLE IF NOT EXISTS notification_reads (
    notification_id INTEGER NOT NULL,
    user_id         INTEGER NOT NULL,
    read_at         TEXT NOT NULL,
    PRIMARY KEY (notification_id, user_id)
);

CREATE TABLE IF NOT EXISTS favorites (
    user_id    INTEGER NOT NULL,
    article_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, article_id)
);

CREATE TABLE IF NOT EXISTS browse_history (
    user_id    INTEGER NOT NULL,
    article_id TEXT NOT NULL,
    viewed_at  TEXT NOT NULL,
    PRIMARY KEY (user_id, article_id)
);

CREATE TABLE IF NOT EXISTS user_preferences (
    user_id         INTEGER PRIMARY KEY,
    industries_json TEXT NOT NULL DEFAULT '[]',
    push_enabled    INTEGER NOT NULL DEFAULT 1,
    digest_time     TEXT NOT NULL DEFAULT '09:00',
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS operation_logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER,
    username   TEXT NOT NULL DEFAULT '',
    action     TEXT NOT NULL,
    target     TEXT NOT NULL DEFAULT '',
    detail     TEXT NOT NULL DEFAULT '',
    ip         TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sites (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    list_url   TEXT NOT NULL,
    category   TEXT NOT NULL DEFAULT '',
    region     TEXT NOT NULL DEFAULT '',
    timezone   TEXT NOT NULL DEFAULT 'Asia/Shanghai',
    enabled    INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
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
CREATE INDEX IF NOT EXISTS idx_notifications_created ON notifications(created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_notifications_unread ON notifications(read_at, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_analyzed_published ON analyzed_articles(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_extractions_schema ON extractions(schema_id, field);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id, expires_at);
CREATE INDEX IF NOT EXISTS idx_notification_reads_user ON notification_reads(user_id, notification_id);
CREATE INDEX IF NOT EXISTS idx_favorites_user ON favorites(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_history_user ON browse_history(user_id, viewed_at DESC);
CREATE INDEX IF NOT EXISTS idx_operation_logs_created ON operation_logs(created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_operation_logs_action ON operation_logs(action, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_sites_enabled ON sites(enabled, name);
"""

# --- 两套时间口径：定义必须在使用它们的 DDL 之前 ---------------------------
#
# 排序与聚合共用「真实时刻」：优先 published_at_utc，历史行 utc 缺失时回退站点墙钟
# published_at。混时区站点里墙钟最大的那篇不一定最新，按时间排必须用它。
#
# 日期**过滤**用的是另一套口径（_DAY_EXPR），两者不可互换，理由见下。
_TIME_EXPR = "COALESCE(NULLIF(published_at_utc, ''), published_at)"

# 日期筛选按「卡片上显示的那一天」判定，也就是站点本地墙钟日期。
#
# 为什么不用真实时刻做区间：两者在生产库上差得很远——472 行里 137 行（29%）墙钟日
# 与 UTC 日不同日。按 UTC 日筛 11-01，会返回卡片上标着「2026-11-03 00:00」的条目
# （东八区 11-03 00:00 == UTC 11-02 16:00），用户看到的第一反应是「筛错了」。
# 规范书要求的是「按发布时间范围筛选」，用户判定的依据只能是界面上那个日期，
# 所以区间必须跟展示同源。
#
# 代价：跨时区时「同一时刻」的两篇可能被分到不同天。当前 6 个源全是 Asia/Shanghai，
# 且 published_at 自带时区偏移，日期的本地性没有歧义。若将来接入多时区源并需要
# 「同一时刻」的严格全集，应改成给每次查询传一个展示时区、把边界换算成 UTC 再比，
# 而不是退回现在的写法。
# 墙钟优先、utc 兜底：与 _TIME_EXPR 的优先级相反，但这里要的是「界面上显示的那一天」。
# 两列都空的行无法判定，会被排除并计入 undated_excluded。
_DAY_EXPR = "substr(COALESCE(NULLIF(published_at, ''), NULLIF(published_at_utc, '')), 1, 10)"

# SQLite 内置的 trim(x) 只去掉 U+0020，'\t' / NBSP / 全角空格会原样留下，于是这类
# 「看起来是空的」日期既命中上界（'\t' < '2026-11-06'）又算不出 undated，行会被静默丢掉。
# 用第二参数显式给一张表，且直接由 search.WS_CHARS 生成：两处共用同一份码位表，
# 不会再出现「SQL 侧手写 8 个、Python 侧 22 个」这种静默丢行的缺口（U+2003、
# U+0085 等复制的日期此前会被既不算结果也不算 undated）。
_SQL_BLANK_CHARS = " || ".join(f"char({ord(ch)})" for ch in search_mod.WS_CHARS)
# 「日期不可判定」的统一判定：NULL，或只由空白组成（含制表符/全角空格）。
_DAY_MISSING_SQL = f"({_DAY_EXPR} IS NULL OR trim({_DAY_EXPR}, {_SQL_BLANK_CHARS}) = '')"


# 依赖新增列的索引不能写在 SCHEMA_SQL 里：executescript 先于补列执行，旧库会因为
# category 列还不存在而直接报错。放在 _migrate_columns 之后建。
#
# 索引表达式直接引用上面两个常量（f-string 拼接），而不是再抄一遍字面量：SQLite 的
# 表达式索引按语法文本匹配，DDL 与查询里的表达式差一处 NULLIF 就会退化成全表扫描，
# 且不会报错。抄写两遍等于给自己埋一个静默的性能退化。
_POST_MIGRATION_SQL = f"""
CREATE INDEX IF NOT EXISTS idx_analyzed_category ON analyzed_articles(category, published_at DESC);
CREATE INDEX IF NOT EXISTS idx_analyzed_recency ON analyzed_articles(COALESCE(published_at, analyzed_at) DESC, article_id DESC);
-- 时间排序/聚合按 _TIME_EXPR 比较，索引表达式必须与查询里一字不差才可能被用上；
-- 沿用 recency 那个表达式（不含 utc）会让 WHERE 走全表扫描。
CREATE INDEX IF NOT EXISTS idx_analyzed_recency_utc ON analyzed_articles({_TIME_EXPR} DESC, article_id DESC);
-- published_asc 的截断键是 ({_TIME_EXPR} IS NULL), {_TIME_EXPR} ASC, article_id ASC：
-- 升序时 SQLite 默认把 NULL 排最前（无日期行会占掉「最早 2000 行」的名额），
-- 加 IS NULL 前缀后排序表达式与 recency_utc 不同，需要一条同序索引，否则 2 万行
-- 全命中时退化为 SCAN + TEMP B-TREE（实测 2.0s → 0.05s，索引扫描可提前 LIMIT）。
CREATE INDEX IF NOT EXISTS idx_analyzed_asc_undated_last ON analyzed_articles(({_TIME_EXPR} IS NULL), {_TIME_EXPR} ASC, article_id ASC);
-- (source, 时间) 组合索引：来源筛选和「发布单位 + 时间范围」同时出现时，
-- 单列 source 索引仍要把命中行全取出来再排序。
CREATE INDEX IF NOT EXISTS idx_analyzed_source_time ON analyzed_articles(source, {_TIME_EXPR} DESC);
-- 日期筛选按展示用的墙钟日（_DAY_EXPR）比较，与上面的真实时刻不是同一个表达式，
-- 需要单独的索引，否则带日期过滤的分页要把整表取出来再过筛。
CREATE INDEX IF NOT EXISTS idx_analyzed_day ON analyzed_articles({_DAY_EXPR} DESC);
"""

# init_db 只有 CREATE TABLE IF NOT EXISTS，升级前建好的库不会自动长出新列。
# 新增列必须在这里补一次 ADD COLUMN，否则老库上的 INSERT 会因列不存在直接失败。
_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    # (列, 列定义) —— 都落在 analyzed_articles 上
    ("body", "TEXT"),
    ("body_html", "TEXT"),
    ("category", "TEXT"),
    ("category_confidence", "REAL"),
    ("category_engine", "TEXT"),
    ("industry", "TEXT"),
    ("industry_confidence", "REAL"),
    ("industry_engine", "TEXT"),
)


def _load_json(raw: Any) -> Any:
    """库被手工改坏时不要让整个端点 500。"""
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _now_iso() -> str:
    """UTC ISO8601（带 Z）：通知要由浏览器解析相对时间，不能丢时区信息。"""
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
    industry: str = "",
    industry_confidence: float | None = None,
    industry_engine: str = "",
    body: str | None = None,
    body_html: str | None = None,
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
            industry=industry,
            industry_confidence=industry_confidence,
            industry_engine=industry_engine,
            body=body,
            body_html=body_html,
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
    industry: str,
    industry_confidence: float | None,
    industry_engine: str,
    body: str | None,
    body_html: str | None,
    now: str,
) -> None:
    conn.execute(
        """
        INSERT INTO analyzed_articles
            (article_id, source, url, title, published_at, published_at_utc,
             content_length, body, body_html, category, category_confidence, category_engine,
             industry, industry_confidence, industry_engine,
             tag_engine, extract_engine, analyzed_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(article_id) DO UPDATE SET
            source=excluded.source, url=excluded.url, title=excluded.title,
            published_at=excluded.published_at, published_at_utc=excluded.published_at_utc,
            content_length=excluded.content_length,
            -- 正文只在本次真的拿到时覆盖：早期行 body 为 NULL，重新分析若上游没返回正文
            -- （详情接口抖动、正文为空），不能被写成 NULL/空串，否则检索会静默丢文章。
            body         = COALESCE(NULLIF(excluded.body, ''), analyzed_articles.body),
            body_html    = COALESCE(NULLIF(excluded.body_html, ''), analyzed_articles.body_html),
            -- 空值表示「这次没做分类」（旧版本重跑、categories.json 被移走后退回单级打标），
            -- 不能拿它覆盖库里已有的分类；明确判成 other 时 category='other'，不是空值，会正常写入。
            category            = COALESCE(excluded.category, analyzed_articles.category),
            category_confidence = COALESCE(excluded.category_confidence, analyzed_articles.category_confidence),
            category_engine     = COALESCE(NULLIF(excluded.category_engine, ''), analyzed_articles.category_engine),
            industry            = COALESCE(excluded.industry, analyzed_articles.industry),
            industry_confidence = COALESCE(excluded.industry_confidence, analyzed_articles.industry_confidence),
            industry_engine     = COALESCE(NULLIF(excluded.industry_engine, ''), analyzed_articles.industry_engine),
            tag_engine=excluded.tag_engine,
            extract_engine=excluded.extract_engine, analyzed_at=excluded.analyzed_at
        """,
        (article_id, source, url, title, published_at, published_at_utc,
         content_length, body, body_html, category or None, category_confidence, category_engine,
         industry or None, industry_confidence, industry_engine,
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


# _TIME_EXPR / _DAY_EXPR 定义在文件上方（_POST_MIGRATION_SQL 之前），因为索引 DDL
# 要用 f-string 引用它们的同一份文本。这里只说明各自的用途分工：
# 排序与聚合（_LIST_ORDERS / cutoff_order / source_stats）用 _TIME_EXPR；
# 日期过滤（_time_clauses）用 _DAY_EXPR。两者不可互换。


def _time_clauses(
    published_from: str | None,
    published_to: str | None,
) -> tuple[list[str], list[Any]]:
    """[from, to) 半开区间，按 published_at 的日期前缀比较（纯日期字符串）。

    参数允许是 'YYYY-MM-DD' 或 'YYYY-MM-DD HH:MM:SS'；带时分秒的输入会被**截断为日期**
    （'2026-11-01 15:30:00' 当作整日窗口起点 11-01），调用方想精确到时刻请自己换算。
    """
    clauses: list[str] = []
    args: list[Any] = []
    # 只要带了任一端，就必须把「日期不可判定」的行排除掉，而不是只靠上下界。
    # 原因在 SQLite 的比较语义：published_at 是纯空白（'   ' / '\t' / 全角空格）时
    # NULLIF(published_at,'') 不会把它变成 NULL（它只认空串），于是 _DAY_EXPR 得到
    # '   ' 而不是 NULL，'   ' < '2026-11-07' 为真——这类行会被上界命中、同时又被
    # undated_excluded 统计为「未计入」，同一行既出现又被说成排除掉了。
    # 判空必须走 _DAY_MISSING_SQL：内置 trim() 只认半角空格，'\t' 会被漏掉。
    if published_from:
        clauses.append(f"{_DAY_EXPR} >= ?")
        args.append(published_from[:10])
    if published_to:
        # 用 < 而不是 <=：区间由调用方把日期转成「次日」，这样当天 23:59:59
        # 与带毫秒的时间戳都不会被漏掉。
        clauses.append(f"{_DAY_EXPR} < ?")
        args.append(published_to[:10])
    if published_from or published_to:
        # 与 _DAY_MISSING_SQL 严格互补：能被这项排除的行，恰好是 undated 统计数到的那批。
        clauses.append(f"NOT {_DAY_MISSING_SQL}")
    return clauses, args


_DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def normalize_day(value: str | None) -> str | None:
    """把 'YYYY-MM-DD' 归一成 'YYYY-MM-DD 00:00:00'；空串/None 视为未传。

    保留时分秒是为了让调用方的区间语义（[from, to) 的 to 是「次日零点」）读起来
    仍是时间戳；_time_clauses 只取前 10 位做日期比较。

    只接受纯日期：检索条件里的日期来自日期选择器，带时分秒的输入没有明确语义
    （是区间起点还是整点截止？），与其猜不如在参数校验阶段就拒掉。
    """
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    # 先卡形状再解析：date.fromisoformat 从 3.11 起还接受 "20260301"，
    # strptime("%Y-%m-%d") 又会接受 "2026-3-1"，两者都是前端不该送来的形式。
    # 只认 YYYY-MM-DD，别的直接在参数校验阶段拒掉，避免同一天多种写法被当成
    # 不同的检索条件、或紧凑格式被悄悄解释成另一天。
    if not _DAY_RE.fullmatch(text):
        raise ValueError(f"日期格式应为 YYYY-MM-DD: {value}")
    try:
        parsed = dt.date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"日期不合法: {value}") from exc
    return parsed.strftime("%Y-%m-%d 00:00:00")


def normalize_day_end(value: str | None) -> str | None:
    """把日期终点推到「次日 00:00:00」，与 _time_clauses 的 [from, to) 配套。"""
    start = normalize_day(value)
    if start is None:
        return None
    try:
        parsed = dt.date.fromisoformat(start[:10]) + dt.timedelta(days=1)
    except OverflowError as exc:
        # 日期控件常拿 9999-12-31 当「不限」，日期算术会越界；这是参数问题，
        # 应该按 422 拒掉，而不是让请求以 500 结束。
        raise ValueError(f"日期超出可比较范围: {value}") from exc
    return parsed.strftime("%Y-%m-%d 00:00:00")


# 未标注来源（source 为空串/NULL）没有天然可用的筛选值：空串在 HTTP 层表示「清空多选」，
# 会被 _split_multi 丢弃。这里给一个显式 token，前端从 /v1/sources 的 source_filter 拿到它，
# 筛选时原样回传，库里再映射成「source 为空」。
UNLABELED_SOURCE = "__unlabeled__"


def _source_clause(sources: Sequence[str] | None) -> tuple[list[str], list[Any]]:
    if sources is None:
        return [], []
    if not sources:
        # 空列表 = 「筛完什么都不剩」，与不传（None）不是一回事
        return ["0"], []
    names = [s for s in sources if s != UNLABELED_SOURCE]
    clauses: list[str] = []
    args: list[Any] = []
    if names:
        placeholders = ",".join("?" for _ in names)
        clauses.append(f"a.source IN ({placeholders})")
        args.extend(names)
    if len(names) != len(sources):
        clauses.append("COALESCE(a.source, '') = ''")
    if len(clauses) == 1:
        return clauses, args
    return ["(" + " OR ".join(clauses) + ")"], args


# 列表排序：默认最新优先。按 category_confidence 排时用 IS NULL 前缀把缺值的行
# 压到最后，否则 SQLite 把 NULL 当最小值，升序时一堆无评分行会排在最前面。
#
# 按时间排时**必须与时间过滤用同一个表达式**（_TIME_EXPR，真实时刻）。早先这里写的是
# COALESCE(published_at, analyzed_at)（站点墙钟），于是同一批数据在「按某天筛」和
# 「按时间排」上用了两套口径：生产库 472 行里 137 行（29%）utc 与墙钟差一天以上，
# 用户筛 11-01 会看到标着 11-02 的条目，同刻不同源的行还会排错位。
# 换成 _TIME_EXPR 后 ORDER BY 也能命中 idx_analyzed_recency_utc，省掉一次 TEMP B-TREE。
# 但**只有不带日期过滤时才省**：带 _DAY_EXPR 过滤时过滤键与排序键不是同一个表达式，
# 索引只能缩小扫描范围，排序仍需 TEMP B-TREE，成本随窗口内命中行数线性增长
# （2 万行、窗口命中大半、取 20 行实测 0.110s；无日期过滤 0.000s）。
# 代价：两列都空的行不再拿 analyzed_at 顶到最新，按「发布时间排序」的语义它们本就该排
# 最后（DESC 时 NULL 居末）；生产库当前 0 行两列皆空，先不为它单独建兜底索引。
_LIST_ORDERS = {
    "published_desc": f"{_TIME_EXPR} DESC, a.article_id DESC",
    "published_asc": f"{_TIME_EXPR} ASC, a.article_id ASC",
    "confidence_desc": "(a.category_confidence IS NULL), a.category_confidence DESC, a.article_id DESC",
    "confidence_asc": "(a.category_confidence IS NULL), a.category_confidence ASC, a.article_id ASC",
}
LIST_SORTS = tuple(_LIST_ORDERS)

# 检索的类内排序：默认按相关度，另外允许按时间翻。"相关度排序" 是产品默认，
# 按时间排是给「先看最新的」这类浏览型查询用的，两者都得由后端算完，
# 因为前端拿不到复算分数所需的查询覆盖度。
SEARCH_SORTS = ("relevance", "published_desc", "published_asc")


def list_analyzed(
    conn: sqlite3.Connection,
    *,
    tag: str | None,
    limit: int,
    offset: int,
    category: str | None = None,
    sources: Sequence[str] | None = None,
    industry: str | None = None,
    published_from: str | None = None,
    published_to: str | None = None,
    sort: str = "published_desc",
) -> tuple[list[dict[str, Any]], int, int]:
    """分页列表；返回 (rows, total, undated_excluded)。

    undated_excluded 只在带了时间过滤时才有意义：它统计「本来能命中其他条件、
    但因为两个时间列都是 NULL 而无法判断是否在区间内」的行数。
    """
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
    if industry is not None:
        clauses.append("COALESCE(a.industry, '') = ?")
        args.append(industry)
    src_clauses, src_args = _source_clause(sources)
    clauses.extend(src_clauses)
    args.extend(src_args)

    time_clauses, time_args = _time_clauses(published_from, published_to)
    all_clauses = [*clauses, *time_clauses]
    where = f"WHERE {' AND '.join(all_clauses)}" if all_clauses else ""

    total = conn.execute(f"SELECT COUNT(*) FROM analyzed_articles a {where}", [*args, *time_args]).fetchone()[0]
    undated = 0
    if time_clauses:
        # 单独数一遍「两个时间列都空」的行：它们本来就命不中时间条件，
        # 不数出来的话用户看到的 total 会比预期少一截且无解释。
        undated_clauses = [
            *clauses,
            # 与 _time_clauses 里的排除条件共用 _DAY_MISSING_SQL：两处各写一套的话，
            # 纯空白行会被过滤条件排除却不计入 undated，total 少一截还没解释。
            _DAY_MISSING_SQL,
        ]
        undated = conn.execute(
            "SELECT COUNT(*) FROM analyzed_articles a WHERE " + " AND ".join(undated_clauses),
            args,
        ).fetchone()[0]
    order = _LIST_ORDERS.get(sort)
    if order is None:
        raise ValueError(f"未知排序: {sort}")
    rows = conn.execute(
        f"""SELECT a.* FROM analyzed_articles a {where}
            ORDER BY {order}
            LIMIT ? OFFSET ?""",
        [*args, *time_args, limit, offset],
    ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        # 列表只给卡片字段：正文与正文 HTML 都是整段大字段，一页 20 条会多传
        # 几百 KB；需要正文的前端走 /v1/articles/{id} 详情或 /v1/search?include_body=1。
        item.pop("body", None)
        item.pop("body_html", None)
        item["tags"] = [
            {"tag": r["tag"], "confidence": r["confidence"], "engine": r["engine"]}
            for r in conn.execute(
                "SELECT tag, confidence, engine FROM article_tags WHERE article_id = ? ORDER BY confidence DESC",
                (row["article_id"],),
            )
        ]
        out.append(item)
    return out, int(total), int(undated)


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


def industry_stats(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """One row per controlled industry; unclassified (pre-upgrade) rows are excluded."""
    rows = conn.execute(
        "SELECT industry, COUNT(*) AS count, ROUND(AVG(industry_confidence), 3) AS avg_confidence "
        "FROM analyzed_articles WHERE industry IS NOT NULL AND industry <> '' "
        "GROUP BY industry ORDER BY count DESC"
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


def source_stats(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """One row per crawler source, for the source filter and the admin board.

    latest_published_at uses _TIME_EXPR rather than published_at on purpose: with
    mixed-timezone sources the row with the largest wall clock is not necessarily
    the newest one, and a board showing "last updated" must use real instants.
    """
    rows = conn.execute(
        "SELECT COALESCE(a.source, '') AS source, COUNT(*) AS count,"
        " ROUND(AVG(a.category_confidence), 3) AS avg_confidence,"
        f" MAX({_TIME_EXPR}) AS latest_published_at "
        "FROM analyzed_articles a GROUP BY COALESCE(a.source, '')"
        " ORDER BY count DESC, source ASC"
    ).fetchall()
    # 不在这里过滤空 source：/v1/articles 的 total 把它算进去，这里若跳过，
    # sum(count) 会比 total 少，前端「全部来源」与站点计数会对不上。
    # 空 source 由前端显示成「未标注来源」；它自己需要一个非空筛选值，
    # 因为空串在 HTTP 层是「清空多选」，无法选中这一组。
    return [
        {**dict(r), "source_filter": r["source"] or UNLABELED_SOURCE} for r in rows
    ]


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


# ------------------------------------------------------------------ notifications
def create_notification(
    conn: sqlite3.Connection,
    *,
    article_id: str,
    kind: str,
    title: str,
    message: str,
    commit: bool = True,
) -> bool:
    """为文章创建站内通知；同一篇文章只提醒一次（--force 重分析不会重复冒红点）。

    返回 True 表示这次真的插入了新通知。调用方在批量事务里传 commit=False。
    """
    cur = conn.execute(
        "INSERT INTO notifications (article_id, kind, title, message, created_at)"
        " VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT(article_id) DO NOTHING",
        (article_id, kind, title, message, _now_iso()),
    )
    if commit:
        conn.commit()
    return int(cur.rowcount) > 0


def _notification_row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    # 用户维度查询会带 user_read；无账号的服务调用仍回落到全局 read_at。
    if "user_read" in item:
        item["read"] = bool(item.pop("user_read"))
    else:
        item["read"] = item["read_at"] is not None
    return item


def list_notifications(
    conn: sqlite3.Connection,
    *,
    user_id: int | None = None,
    limit: int = 20,
    unread_only: bool = False,
) -> tuple[list[dict[str, Any]], int, int]:
    """返回 (通知列表, 总数, 未读数)。

    user_id 为空时按旧版全局 read_at 统计（服务账号/无登录场景）；传了用户
    则读取 notification_reads，每个人各有一份已读状态，互不影响。total 与列表
    使用同一筛选条件，unread 始终是该用户（或全局）的未读总数。
    """
    if user_id is None:
        join = ""
        select_read = "n.read_at"
        base_args: list[Any] = []
        where = "WHERE n.read_at IS NULL" if unread_only else ""
        unread_where = "WHERE read_at IS NULL"
        unread_args: list[Any] = []
    else:
        join = "LEFT JOIN notification_reads r ON r.notification_id = n.id AND r.user_id = ?"
        select_read = "CASE WHEN r.read_at IS NULL THEN 0 ELSE 1 END AS user_read"
        base_args = [user_id]
        where = "WHERE r.read_at IS NULL" if unread_only else ""
        unread_where = (
            "WHERE n.id NOT IN (SELECT notification_id FROM notification_reads WHERE user_id = ?)"
        )
        unread_args = [user_id]
    rows = conn.execute(
        f"""SELECT n.id, n.article_id, n.kind, n.title, n.message, n.created_at,
                   {select_read},
                   a.url, a.source, a.category, a.published_at
            FROM notifications n
            {join}
            LEFT JOIN analyzed_articles a ON a.article_id = n.article_id
            {where}
            ORDER BY n.created_at DESC, n.id DESC
            LIMIT ?""",
        [*base_args, limit],
    ).fetchall()
    total = int(
        conn.execute(
            f"SELECT COUNT(*) FROM notifications n {join} {where}", base_args
        ).fetchone()[0]
    )
    unread = int(
        conn.execute(f"SELECT COUNT(*) FROM notifications n {unread_where}", unread_args).fetchone()[0]
    )
    return [_notification_row(r) for r in rows], total, unread


def mark_notification_read(
    conn: sqlite3.Connection, notification_id: int, *, user_id: int | None = None
) -> dict[str, Any] | None:
    """标记单条已读；重复调用保持首次已读时间不变。不存在返回 None。"""
    exists = conn.execute(
        "SELECT 1 FROM notifications WHERE id = ?", (notification_id,)
    ).fetchone()
    if exists is None:
        return None
    if user_id is None:
        conn.execute(
            "UPDATE notifications SET read_at = COALESCE(read_at, ?) WHERE id = ?",
            (_now_iso(), notification_id),
        )
        read_expr = "n.read_at"
        read_args: list[Any] = []
        join = ""
    else:
        conn.execute(
            "INSERT INTO notification_reads (notification_id, user_id, read_at) VALUES (?,?,?)"
            " ON CONFLICT(notification_id, user_id) DO NOTHING",
            (notification_id, user_id, _now_iso()),
        )
        read_expr = "CASE WHEN r.read_at IS NULL THEN 0 ELSE 1 END AS user_read"
        read_args = [user_id]
        join = "LEFT JOIN notification_reads r ON r.notification_id = n.id AND r.user_id = ?"
    row = conn.execute(
        f"""SELECT n.id, n.article_id, n.kind, n.title, n.message, n.created_at,
                   {read_expr},
                   a.url, a.source, a.category, a.published_at
           FROM notifications n
           {join}
           LEFT JOIN analyzed_articles a ON a.article_id = n.article_id
           WHERE n.id = ?""",
        [*read_args, notification_id],
    ).fetchone()
    conn.commit()
    return _notification_row(row) if row is not None else None


def mark_all_notifications_read(
    conn: sqlite3.Connection, *, user_id: int | None = None
) -> int:
    if user_id is None:
        cur = conn.execute(
            "UPDATE notifications SET read_at = ? WHERE read_at IS NULL", (_now_iso(),)
        )
    else:
        cur = conn.execute(
            """INSERT INTO notification_reads (notification_id, user_id, read_at)
               SELECT n.id, ?, ? FROM notifications n
               WHERE NOT EXISTS (
                   SELECT 1 FROM notification_reads r
                   WHERE r.notification_id = n.id AND r.user_id = ?)""",
            (user_id, _now_iso(), user_id),
        )
    conn.commit()
    return int(cur.rowcount)


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
    tables = (
        "article_tags",
        "extractions",
        "article_checklists",
        "favorites",
        "browse_history",
        "notifications",
    )
    counts: dict[str, int] = {}
    try:
        # notification_reads 没有 article_id，必须在 notifications 被删前按通知 id 清掉。
        if dry_run:
            counts["notification_reads"] = int(
                conn.execute(
                    "SELECT COUNT(*) FROM notification_reads WHERE notification_id IN ("
                    "SELECT id FROM notifications WHERE article_id IN (" + article_subquery + "))",
                    (cutoff,),
                ).fetchone()[0]
            )
        else:
            counts["notification_reads"] = int(
                conn.execute(
                    "DELETE FROM notification_reads WHERE notification_id IN ("
                    "SELECT id FROM notifications WHERE article_id IN (" + article_subquery + "))",
                    (cutoff,),
                ).rowcount
            )
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
def _undated_excluded(
    conn: sqlite3.Connection,
    *,
    terms: Sequence[str],
    tag_ids: Sequence[str],
    categories: Sequence[str] | None,
    sources: Sequence[str] | None,
    industries: Sequence[str] | None,
    published_from: str | None,
    published_to: str | None,
) -> int:
    """带日期过滤时被排除的「无日期」命中数；不筛日期时返回 0。

    条件与 search_candidates 逐项同源（signal / category / source），只有日期那一段
    换成「判不出日期」——这就是「本来命中、只因日期不可判定而被筛掉」的那批。
    """
    if not (published_from or published_to):
        return 0
    clauses, args = _signal_clauses(conn, terms, tag_ids)
    if categories is not None:
        if not categories:
            return 0
        placeholders = ",".join("?" for _ in categories)
        clauses.append("COALESCE(a.category, '') IN (" + placeholders + ")")
        args.extend(categories)
    if industries is not None:
        if not industries:
            return 0
        placeholders = ",".join("?" for _ in industries)
        clauses.append("COALESCE(a.industry, '') IN (" + placeholders + ")")
        args.extend(industries)
    src_clauses, src_args = _source_clause(sources)
    clauses.extend(src_clauses)
    args.extend(src_args)
    clauses.append(_DAY_MISSING_SQL)
    row = conn.execute(
        "SELECT COUNT(*) FROM analyzed_articles a WHERE " + " AND ".join(clauses),
        args,
    ).fetchone()
    return int(row[0]) if row else 0


def _signal_clauses(
    conn: sqlite3.Connection,
    terms: Sequence[str],
    tag_ids: Sequence[str],
) -> tuple[list[str], list[Any]]:
    """检索命中的信号条件：命中任一标签，或标题/正文包含任一词。

    抽出来是为了让 search_candidates（取候选）与 search_grouped 的
    undated_excluded 统计用同一份条件——两处各写一遍，将来改了一处就会出现
    「统计说排除了 N 篇，实际筛掉的却是另一批」。

    标签必须带正置信度才算信号：search.score_article 对 confidence<=0 的标签直接
    `continue`，has_signal 随后也会把这类行判为「无关系」。SQL 侧不跟着卡，
    零置信度标签的行会占掉候选集名额、并被算进 undated_excluded——报出「有 N 篇
    只因日期不可判定被排除」，而它们本来就不会出现在结果里。
    """
    clauses: list[str] = []
    args: list[Any] = []
    signal: list[str] = []
    if tag_ids:
        placeholders = ",".join("?" for _ in tag_ids)
        signal.append(
            "a.article_id IN (SELECT article_id FROM article_tags"
            " WHERE tag IN (" + placeholders + ") AND confidence > 0)"
        )
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
    return clauses, args


def search_candidates(
    conn: sqlite3.Connection,
    *,
    terms: Sequence[str],
    tag_ids: Sequence[str],
    categories: Sequence[str] | None = None,
    sources: Sequence[str] | None = None,
    industries: Sequence[str] | None = None,
    published_from: str | None = None,
    published_to: str | None = None,
    sort: str = "relevance",
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
    a one-character keyword matches everything. The caller re-ranks by relevance
    (see search_grouped), so this order is not the display order; it only decides
    *which* rows survive the cut.

    That choice has to be deterministic and meaningful. Without ORDER BY, SQLite
    returns rows in whatever order the scan happens to produce, so once a query
    exceeds `limit` two identical runs can truncate to different sets and the user
    sees results appear and disappear between refreshes. Newest-first is the same
    bias the product wants (recent news matters more), and `article_id` breaks
    full ties so the set never depends on scan order.

    `idx_analyzed_recency` exists to make that ordering cheap, and the win is much
    larger than the sort itself. Without the index SQLite has to evaluate every
    hit before it can order them, so the per-row `py_norm` scan runs over the whole
    match set even though only `limit` rows are kept. With the index it walks in
    index order and stops after `limit` rows. Measured on 20000 matching rows,
    limit=2000: 3.9s -> 0.07s when the term is in the title, 11.9s -> 0.82s when
    it is only in the body. The two differ by a factor because the prefilter's OR
    short-circuits on a title hit and never normalizes the 6000-char body, so do
    not quote the cheaper number as the normal case -- most specific terms only
    appear in the body. Benchmarking just the ORDER BY clause (no py_norm) shows
    0.01s vs 0.002s and badly understates this, because that plan never had to
    normalize the rows it discards.
    """
    clauses: list[str] = []
    args: list[Any] = []

    signal, signal_args = _signal_clauses(conn, terms, tag_ids)
    clauses.extend(signal)
    args.extend(signal_args)

    if categories is not None:
        if not categories:
            return []
        placeholders = ",".join("?" for _ in categories)
        clauses.append("COALESCE(a.category, '') IN (" + placeholders + ")")
        args.extend(categories)
    if industries is not None:
        if not industries:
            return []
        placeholders = ",".join("?" for _ in industries)
        clauses.append("COALESCE(a.industry, '') IN (" + placeholders + ")")
        args.extend(industries)

    # 时间与来源筛选和 list_analyzed 共用同一套子句，两处口径必须一致：
    # 否则同一批数据在「浏览」和「检索」里被筛出不同的集合，用户会以为丢数据。
    src_clauses, src_args = _source_clause(sources)
    clauses.extend(src_clauses)
    args.extend(src_args)
    time_clauses, time_args = _time_clauses(published_from, published_to)
    clauses.extend(time_clauses)
    args.extend(time_args)

    # ORDER BY 必须在截断前定序，否则同一查询两次可能截到不同集合，用户会看到
    # 结果忽有忽无。它同时决定「命中超过 limit 时保留哪些」——取最新的，所以
    # 顺序本身就是产品行为，不只是稳定性。该表达式有 idx_analyzed_recency
    # 支撑（见 _POST_MIGRATION_SQL）：没有这个索引时先算完全部命中行才能排序，
    # 于是每行的 py_norm 都要跑一遍，即使最后只留 limit 行。2 万行全命中、
    # limit=2000 实测（本机 min-of-2）：标题命中 3.9s → 0.07s；仅正文命中
    # 11.9s → 0.82s（预筛的 OR 在标题命中时短路，不会归一 6000 字正文，所以
    # 两条路径成本差一个档，别把 0.07s 当成常态——用户查的细分词大多只在正文）。
    #
    # 没有任何 signal（terms 与 tag_ids 都空）时 where 为空串，会退化成全表扫描。
    # 生产路径走不到：search_grouped 在 query.is_empty 处提前返回空结果。这里保留
    # 该行为而不是返回 []，是因为本函数是公开的候选集查询，"不限条件" 是合理语义，
    # 由调用方负责判空。
    # 排序必须和调用方的意图一致，因为它决定「超过 limit 时留哪一批」。
    # 按时间升序查却仍用「最新优先」截断，会把用户想看的最老的几条直接截掉，
    # 结果集里只剩最新的 2000 行——不报错，但答案是错的。
    # relevance 模式仍按最新优先截断：相关度要在 Python 侧算完才知道，SQL 阶段
    # 拿不到，只能先按一个与产品偏好一致（新的更重要）的次序取候选。
    # 截断键必须与 _LIST_ORDERS / search_grouped 用同一个时间表达式，否则「筛 11-01
    # 的窗口」和「留哪 2000 行」用的是两套时间口径：升序查会把真实时刻最老的行截掉，
    # 留下墙钟最老的（生产库 29% 的行两者不在同一天）。
    if sort not in SEARCH_SORTS:
        raise ValueError(f"未知排序: {sort}")
    # ASC 时 SQLite 把 NULL 排在最前，两列皆空的行会占掉「最早」的名额；加 IS NULL
    # 前缀把它们压到最后，只影响截断保留哪一批。空白文本键不是 NULL，本前缀不改变其顺序。
    # 该顺序由 idx_analyzed_asc_undated_last 支撑（DDL 见 _POST_MIGRATION_SQL），
    # 否则 2 万行全命中会退化成一整轮临时排序，LIMIT 无法提前停止。
    cutoff_order = (
        f"({_TIME_EXPR} IS NULL), {_TIME_EXPR} ASC, a.article_id ASC"
        if sort == "published_asc"
        else f"{_TIME_EXPR} DESC, a.article_id DESC"
    )
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        "SELECT a.article_id, a.source, a.url, a.title, a.body,"
        " a.published_at, a.published_at_utc, a.content_length,"
        " a.category, a.category_confidence, a.category_engine,"
        " a.industry, a.industry_confidence, a.industry_engine,"
        " a.tag_engine, a.analyzed_at"
        " FROM analyzed_articles a " + where
        + " ORDER BY " + cutoff_order
        + " LIMIT ?",
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


def set_body(conn: sqlite3.Connection, *, article_id: str, body: str, commit: bool = True) -> bool:
    """Write only the body column, leaving every derived field untouched.

    The guard mirrors the candidate set used by the backfill command: only rows
    that still have no body are written. Without it, a snapshot taken before a
    concurrent analyse run could overwrite the body that run just wrote with an
    older copy.

    Returns whether the row was actually written. The guard can reject the write
    (another process filled the row first), and the backfill reports a `filled`
    count to the user -- counting a no-op as filled would make progress look better
    than it is.
    """
    cur = conn.execute(
        "UPDATE analyzed_articles SET body = ?"
        " WHERE article_id = ? AND (body IS NULL OR body = '')",
        (body, article_id),
    )
    written = cur.rowcount > 0
    if commit:
        conn.commit()
    return written


def set_industry(
    conn: sqlite3.Connection,
    *,
    article_id: str,
    industry: str,
    confidence: float | None,
    engine: str,
    commit: bool = True,
) -> bool:
    """Write only the industry columns, never overwriting an existing industry."""
    cur = conn.execute(
        "UPDATE analyzed_articles SET industry = ?, industry_confidence = ?, industry_engine = ?"
        " WHERE article_id = ? AND (industry IS NULL OR industry = '')",
        (industry, confidence, engine, article_id),
    )
    written = cur.rowcount > 0
    if commit:
        conn.commit()
    return written


def set_body_html(
    conn: sqlite3.Connection, *, article_id: str, body_html: str, commit: bool = True
) -> bool:
    """Write only the body_html column, with the same no-overwrite guard as set_body."""
    cur = conn.execute(
        "UPDATE analyzed_articles SET body_html = ?"
        " WHERE article_id = ? AND (body_html IS NULL OR body_html = '')",
        (body_html, article_id),
    )
    written = cur.rowcount > 0
    if commit:
        conn.commit()
    return written


# --------------------------------------------------------------- search

def search_grouped(
    conn: sqlite3.Connection,
    *,
    raw_query: str,
    tags: list[TagDef],
    category_order: Sequence[str] = (),
    categories: Sequence[str] | None = None,
    sources: Sequence[str] | None = None,
    industries: Sequence[str] | None = None,
    published_from: str | None = None,
    published_to: str | None = None,
    per_category: int = 20,
    include_body: bool = False,
    sort: str = "relevance",
) -> dict[str, Any]:
    """Score every candidate, then group by category and rank inside each group.

    Returning groups rather than one flat ranked list is the whole point: the
    product asks for five categories as the outer structure, and a flat list
    would force the frontend to re-sort by a score it never receives.
    """
    query = search_mod.resolve_query(raw_query, tags)
    if query.is_empty:
        # 形状与正常返回一致（缺键会让前端 branch 出 undefined）：空查询同样要带
        # sort/returned/truncated/undated_excluded，否则前端得为「空结果」单独
        # 兜一套字段默认值，而这套默认值迟早会跟真实返回漂移。
        return {
            "query": raw_query,
            "matched_tags": [],
            "terms": [],
            "groups": [],
            "total": 0,
            "sort": sort,
            "returned": 0,
            "truncated": False,
            "undated_excluded": 0,
        }

    # 多取一行用来判断「是否还有更多」：只比对 len(rows) == limit 的话，库里恰好
    # 2000 行时会误报截断（其实全取完了），而把 limit 传死又不随将来改动走。
    rows = search_candidates(
        conn,
        terms=query.term_texts,
        tag_ids=query.tag_ids,
        categories=categories,
        sources=sources,
        industries=industries,
        published_from=published_from,
        published_to=published_to,
        # 排序影响截断方向：按时间升序查时不能还按「最新优先」取候选，
        # 否则最老的那批恰好是被截掉的那批。
        sort=sort,
        limit=_CANDIDATE_LIMIT + 1,
    )
    undated_excluded = _undated_excluded(
        conn,
        terms=query.term_texts,
        tag_ids=query.tag_ids,
        categories=categories,
        sources=sources,
        industries=industries,
        published_from=published_from,
        published_to=published_to,
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
    if sort == "relevance":
        scored.sort(
            key=lambda pair: (pair[0], pair[1].get("published_at") or "", pair[1]["article_id"]),
            reverse=True,
        )
    elif sort in ("published_desc", "published_asc"):
        # 按时间排时相关度仍要能决定同秒内的顺序：同一批站点的发布时间常见
        # 精确到分钟甚至全是 00:00:00，只按时间排会让同一天的几十篇恒为某一种
        # 任意顺序。两级键都带上，时间主序、相关度次序。
        #
        # 相关度这一级始终「高的在前」，所以不能用 reverse 一刀切（升序时
        # reverse=False 会把低分排前面），只能把分数取负后统一升序。
        if sort == "published_desc":
            scored.sort(
                key=lambda pair: (
                    pair[1].get("published_at_utc") or pair[1].get("published_at") or "",
                    pair[0],
                    pair[1]["article_id"],
                ),
                reverse=True,
            )
        else:
            scored.sort(
                key=lambda pair: (
                    pair[1].get("published_at_utc") or pair[1].get("published_at") or "",
                    -pair[0],
                    pair[1]["article_id"],
                ),
            )
    else:
        raise ValueError(f"未知排序: {sort}")

    buckets: dict[str, list[dict[str, Any]]] = {}
    for _, item in scored:
        buckets.setdefault(item.get("category") or "", []).append(item)

    # 先按调用方给的固定类顺序输出（前端五段布局稳定），再补上不在顺序里
    # 的类（other、以及分类功能上线前的历史 NULL 行）。
    ordered = [c for c in category_order if c in buckets]
    known = set(ordered)
    ordered.extend(sorted(k for k in buckets if k not in known))

    groups: list[dict[str, Any]] = [
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
        "sort": sort,
        "groups": groups,
        # 候选集（最多 2000 行）内命中的行数，不是全库真实命中数：泛词被截断时
        # 这个值会偏小。前端展示「共 N 条」时应理解为「本次返回范围内的条数」。
        "total": len(scored),
        # 实际渲染出去的条数（每类封顶 per_category 之后），与 total 的差额
        # 就是「还有更多没显示」，前端据此决定要不要显示「查看更多」。
        "returned": sum(len(g["items"]) for g in groups),
        # 候选集是否被 _CANDIDATE_LIMIT 截断。为 true 时 total 只是下界，
        # 前端必须提示「结果可能不完整」，不能把 total 当成全库命中数展示。
        "truncated": truncated,
        # 带日期过滤时，有多少篇「本来命中关键词与分类/来源条件，但两个时间列都
        # 判不出日期」的文章被排除在外。不带日期过滤时恒为 0（此时没有东西被排除）。
        #
        # 必须单独数：total 只数筛完剩下的，用户看到「共 20 条」而实际库里有 23 篇
        # 命中，会以为丢了数据。统计口径与 list_analyzed 的 undated_excluded 一致，
        # 且同样不受 _CANDIDATE_LIMIT 截断影响——它是 COUNT(*)，不是对 rows 数一遍。
        "undated_excluded": undated_excluded,
    }
