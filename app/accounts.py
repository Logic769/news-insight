"""Data access for local accounts, personal features and the admin console.

Kept separate from db.py: db.py owns the analysis schema and its carefully tuned
time/search expressions, while everything here is ordinary CRUD around users.
The tables are still created by db.SCHEMA_SQL so one init_db() remains enough.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from typing import Any


# 与 db._now_iso 相同的格式：UTC、带 Z，前端 new Date() 可直接解析。
def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# 数据里的 published_at 是站点墙钟（当前 6 个源全是东八区）。容器默认 UTC，
# 直接 date.today() 会在东八区 00:00–08:00 把「今天」算成昨天。
_CST = dt.timezone(dt.timedelta(hours=8))


def _local_today() -> dt.date:
    return dt.datetime.now(_CST).date()


def _user_row(row: sqlite3.Row, *, include_secret: bool = False) -> dict[str, Any]:
    item = dict(row)
    if not include_secret:
        for key in ("password_hash", "password_salt", "password_iterations"):
            item.pop(key, None)
    item["must_change_password"] = bool(item.get("must_change_password", 0))
    return item


def public_user(user: dict[str, Any]) -> dict[str, Any]:
    """对外用户对象：绝不包含密码派生值与盐。"""
    allowed = (
        "id",
        "username",
        "display_name",
        "department",
        "role",
        "status",
        "must_change_password",
        "last_login_at",
        "created_at",
        "updated_at",
    )
    out = {key: user[key] for key in allowed if key in user}
    out["must_change_password"] = bool(out.get("must_change_password", False))
    out["is_admin"] = out.get("role") == "admin"
    return out


def count_users(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])


def create_user(
    conn: sqlite3.Connection,
    *,
    username: str,
    display_name: str,
    department: str,
    role: str,
    password_hash: str,
    password_salt: str,
    password_iterations: int,
    must_change_password: bool = False,
) -> dict[str, Any]:
    now = _now_iso()
    cur = conn.execute(
        """INSERT INTO users
           (username, password_hash, password_salt, password_iterations,
            display_name, department, role, status, must_change_password, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?, 'active', ?,?,?)""",
        (
            username,
            password_hash,
            password_salt,
            password_iterations,
            display_name,
            department,
            role,
            int(must_change_password),
            now,
            now,
        ),
    )
    conn.commit()
    user_id = int(cur.lastrowid or 0)
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    assert row is not None
    return public_user(_user_row(row))


def get_user_by_username(
    conn: sqlite3.Connection, username: str, *, include_secret: bool = True
) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    return _user_row(row, include_secret=include_secret) if row is not None else None


def get_user(
    conn: sqlite3.Connection, user_id: int, *, include_secret: bool = False
) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _user_row(row, include_secret=include_secret) if row is not None else None


def list_users(
    conn: sqlite3.Connection,
    *,
    q: str = "",
    role: str = "",
    status: str = "",
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    clauses: list[str] = []
    args: list[Any] = []
    if q:
        clauses.append("(username LIKE ? OR display_name LIKE ? OR department LIKE ?)")
        like = f"%{q}%"
        args.extend([like, like, like])
    if role:
        clauses.append("role = ?")
        args.append(role)
    if status:
        clauses.append("status = ?")
        args.append(status)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    total = int(conn.execute(f"SELECT COUNT(*) FROM users {where}", args).fetchone()[0])
    rows = conn.execute(
        f"SELECT * FROM users {where} ORDER BY id ASC LIMIT ? OFFSET ?",
        [*args, limit, offset],
    ).fetchall()
    return [public_user(_user_row(row)) for row in rows], total


def update_user(
    conn: sqlite3.Connection,
    user_id: int,
    *,
    display_name: str | None = None,
    department: str | None = None,
    role: str | None = None,
    status: str | None = None,
) -> dict[str, Any] | None:
    sets: list[str] = []
    args: list[Any] = []
    for column, value in (
        ("display_name", display_name),
        ("department", department),
        ("role", role),
        ("status", status),
    ):
        if value is not None:
            sets.append(f"{column} = ?")
            args.append(value)
    if not sets:
        return get_user(conn, user_id)
    sets.append("updated_at = ?")
    args.append(_now_iso())
    args.append(user_id)
    conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = ?", args)
    conn.commit()
    return get_user(conn, user_id)


def update_user_password(
    conn: sqlite3.Connection,
    user_id: int,
    *,
    password_hash: str,
    password_salt: str,
    password_iterations: int,
    must_change_password: bool = False,
) -> None:
    conn.execute(
        """UPDATE users
           SET password_hash = ?, password_salt = ?, password_iterations = ?,
               must_change_password = ?, failed_attempts = 0, locked_until = NULL, updated_at = ?
           WHERE id = ?""",
        (
            password_hash,
            password_salt,
            password_iterations,
            int(must_change_password),
            _now_iso(),
            user_id,
        ),
    )
    conn.commit()


def record_login_failure(
    conn: sqlite3.Connection, user_id: int, *, max_attempts: int, lock_minutes: int
) -> tuple[int, str | None]:
    row = conn.execute(
        "SELECT failed_attempts, locked_until FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    if row is None:
        return 1, None
    attempts = int(row["failed_attempts"] or 0) + 1
    locked_until: str | None = None
    if attempts >= max_attempts:
        locked_until = (
            dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=lock_minutes)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "UPDATE users SET failed_attempts = ?, locked_until = ?, updated_at = ? WHERE id = ?",
        (attempts, locked_until, _now_iso(), user_id),
    )
    conn.commit()
    return attempts, locked_until


def record_login_success(conn: sqlite3.Connection, user_id: int) -> None:
    conn.execute(
        "UPDATE users SET failed_attempts = 0, locked_until = NULL, last_login_at = ?, updated_at = ?"
        " WHERE id = ?",
        (_now_iso(), _now_iso(), user_id),
    )
    conn.commit()


def create_session(
    conn: sqlite3.Connection, *, token_hash: str, user_id: int, expires_at: str
) -> None:
    conn.execute(
        "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?,?,?,?)",
        (token_hash, user_id, _now_iso(), expires_at),
    )
    conn.commit()


def get_session_user(conn: sqlite3.Connection, token_hash: str) -> dict[str, Any] | None:
    row = conn.execute(
        """SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id
           WHERE s.token_hash = ? AND s.revoked_at IS NULL
             AND s.expires_at > ? AND u.status = 'active'""",
        (token_hash, _now_iso()),
    ).fetchone()
    return _user_row(row) if row is not None else None


def revoke_session(conn: sqlite3.Connection, token_hash: str) -> None:
    conn.execute(
        "UPDATE sessions SET revoked_at = COALESCE(revoked_at, ?) WHERE token_hash = ?",
        (_now_iso(), token_hash),
    )
    conn.commit()


def revoke_user_sessions(
    conn: sqlite3.Connection, user_id: int, *, except_token_hash: str = ""
) -> int:
    cur = conn.execute(
        "UPDATE sessions SET revoked_at = ?"
        " WHERE user_id = ? AND revoked_at IS NULL AND token_hash <> ?",
        (_now_iso(), user_id, except_token_hash),
    )
    conn.commit()
    return int(cur.rowcount)


# ------------------------------------------------------------------ favorites
def add_favorite(conn: sqlite3.Connection, *, user_id: int, article_id: str) -> bool:
    cur = conn.execute(
        "INSERT INTO favorites (user_id, article_id, created_at) VALUES (?,?,?)"
        " ON CONFLICT(user_id, article_id) DO NOTHING",
        (user_id, article_id, _now_iso()),
    )
    conn.commit()
    return int(cur.rowcount) > 0


def remove_favorite(conn: sqlite3.Connection, *, user_id: int, article_id: str) -> bool:
    cur = conn.execute(
        "DELETE FROM favorites WHERE user_id = ? AND article_id = ?", (user_id, article_id)
    )
    conn.commit()
    return int(cur.rowcount) > 0


def is_favorite(conn: sqlite3.Connection, *, user_id: int, article_id: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM favorites WHERE user_id = ? AND article_id = ?",
            (user_id, article_id),
        ).fetchone()
        is not None
    )


def list_favorites(
    conn: sqlite3.Connection, *, user_id: int, limit: int = 50, offset: int = 0
) -> tuple[list[dict[str, Any]], int]:
    total = int(
        conn.execute("SELECT COUNT(*) FROM favorites WHERE user_id = ?", (user_id,)).fetchone()[0]
    )
    rows = conn.execute(
        """SELECT f.article_id, f.created_at AS favorited_at,
                  a.title, a.source, a.url, a.published_at, a.published_at_utc,
                  a.category, a.category_confidence, a.content_length
           FROM favorites f JOIN analyzed_articles a ON a.article_id = f.article_id
           WHERE f.user_id = ?
           ORDER BY f.created_at DESC LIMIT ? OFFSET ?""",
        (user_id, limit, offset),
    ).fetchall()
    return [dict(row) for row in rows], total


# ------------------------------------------------------------------ history
def record_history(conn: sqlite3.Connection, *, user_id: int, article_id: str) -> None:
    conn.execute(
        "INSERT INTO browse_history (user_id, article_id, viewed_at) VALUES (?,?,?)"
        " ON CONFLICT(user_id, article_id) DO UPDATE SET viewed_at = excluded.viewed_at",
        (user_id, article_id, _now_iso()),
    )
    conn.execute(
        """DELETE FROM browse_history WHERE user_id = ? AND article_id NOT IN (
               SELECT article_id FROM browse_history WHERE user_id = ?
               ORDER BY viewed_at DESC, article_id LIMIT 100)""",
        (user_id, user_id),
    )
    conn.commit()


def list_history(
    conn: sqlite3.Connection, *, user_id: int, limit: int = 50, offset: int = 0
) -> tuple[list[dict[str, Any]], int]:
    total = int(
        conn.execute("SELECT COUNT(*) FROM browse_history WHERE user_id = ?", (user_id,)).fetchone()[0]
    )
    rows = conn.execute(
        """SELECT h.article_id, h.viewed_at,
                  a.title, a.source, a.url, a.published_at, a.published_at_utc,
                  a.category, a.category_confidence, a.content_length
           FROM browse_history h JOIN analyzed_articles a ON a.article_id = h.article_id
           WHERE h.user_id = ?
           ORDER BY h.viewed_at DESC LIMIT ? OFFSET ?""",
        (user_id, limit, offset),
    ).fetchall()
    return [dict(row) for row in rows], total


def clear_history(conn: sqlite3.Connection, *, user_id: int) -> int:
    cur = conn.execute("DELETE FROM browse_history WHERE user_id = ?", (user_id,))
    conn.commit()
    return int(cur.rowcount)



# ------------------------------------------------------------ search history
SEARCH_HISTORY_LIMIT = 200
SEARCH_HISTORY_RETENTION_DAYS = 180


def record_search(
    conn: sqlite3.Connection,
    *,
    user_id: int,
    query: str,
    result_count: int = 0,
) -> int:
    """记录一次搜索；10 分钟内同一关键词只更新时间/结果数，避免输入抖动刷屏。"""
    text = " ".join(query.split()).strip()
    if not text:
        raise ValueError("搜索关键词不能为空")
    now = dt.datetime.now(dt.timezone.utc)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    recent_cutoff = (now - dt.timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    # 「窗口内已有就更新、没有就插入」必须和后面的清理在同一个写事务里完成。
    #
    # 原来是裸 SELECT 再 INSERT：两个并发的同词搜索会同时读到「窗口内没有」，各插
    # 一条，10 分钟去重直接失效——历史列表出现两项一模一样的词，而且两条挂着不同
    # 的 id，点击归因会记到用户没看到的那一条上。
    #
    # 不用 UNIQUE 索引兜底：去重窗口是滑动的，唯一键只能是 (user_id, query, 精确到
    # 秒的 created_at)，而那本来就不会撞。真正要的是「判断与写入不可分割」，也就是
    # 一把写锁。BEGIN IMMEDIATE 让后到者一定看得到先到者已提交的行，于是走 UPDATE
    # 分支——id 与已挂上的 clicked_article_id 都保住。
    #
    # 调用方可能已经开了事务（显式记录端点在同连接上做了别的写），此时不能再 BEGIN。
    #
    # 已有外层事务时用 SAVEPOINT 而不是 commit：原来这里无论 ownership 都调
    # conn.commit()，等于替调用方提交了它还没准备好提交的写入，而 except 分支只在
    # owns_txn 时才 rollback —— 一进一出不对称，调用方之后的 rollback 撤不回自己的
    # 业务写（实测 in_transaction 由 True 变 False）。SAVEPOINT 只回退本函数的写入，
    # 进出都不改变外层事务的状态。
    owns_txn = not conn.in_transaction
    savepoint = "record_search_sp" if not owns_txn else None
    if owns_txn:
        conn.execute("BEGIN IMMEDIATE")
    else:
        conn.execute(f"SAVEPOINT {savepoint}")
    try:
        row = conn.execute(
            "SELECT id FROM user_search_history"
            " WHERE user_id = ? AND query = ? AND created_at >= ?"
            " ORDER BY id DESC LIMIT 1",
            (user_id, text, recent_cutoff),
        ).fetchone()
        if row is not None:
            history_id = int(row["id"])
            conn.execute(
                "UPDATE user_search_history SET result_count = ?, created_at = ? WHERE id = ?",
                (max(0, int(result_count)), now_iso, history_id),
            )
        else:
            cur = conn.execute(
                "INSERT INTO user_search_history (user_id, query, result_count, created_at)"
                " VALUES (?,?,?,?)",
                (user_id, text, max(0, int(result_count)), now_iso),
            )
            history_id = int(cur.lastrowid or 0)
        # 保留期按自然时间；条数上限按最近记录。两条清理都只影响当前用户。
        expiry = (now - dt.timedelta(days=SEARCH_HISTORY_RETENTION_DAYS)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        conn.execute(
            "DELETE FROM user_search_history WHERE user_id = ? AND created_at < ?",
            (user_id, expiry),
        )
        conn.execute(
            """DELETE FROM user_search_history WHERE user_id = ? AND id NOT IN (
                   SELECT id FROM user_search_history WHERE user_id = ?
                   ORDER BY created_at DESC, id DESC LIMIT ?
               )""",
            (user_id, user_id, SEARCH_HISTORY_LIMIT),
        )
        if owns_txn:
            conn.commit()
        else:
            conn.execute(f"RELEASE {savepoint}")
    except BaseException:
        # 拿不到写锁时把「已开始的事务」还回去，别让调用方的 next commit 带上一半写入。
        if owns_txn:
            conn.rollback()
        elif savepoint is not None:
            conn.execute(f"ROLLBACK TO {savepoint}")
            conn.execute(f"RELEASE {savepoint}")
        raise
    return history_id


def list_search_history(
    conn: sqlite3.Connection, *, user_id: int, limit: int = 50, offset: int = 0
) -> tuple[list[dict[str, Any]], int]:
    total = int(
        conn.execute(
            "SELECT COUNT(*) FROM user_search_history WHERE user_id = ?", (user_id,)
        ).fetchone()[0]
    )
    rows = conn.execute(
        "SELECT id, query, result_count, clicked_article_id, clicked_at, created_at"
        " FROM user_search_history WHERE user_id = ?"
        " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
        (user_id, limit, offset),
    ).fetchall()
    return [dict(row) for row in rows], total


def mark_search_click(
    conn: sqlite3.Connection, *, user_id: int, history_id: int, article_id: str
) -> bool:
    cur = conn.execute(
        "UPDATE user_search_history SET clicked_article_id = ?, clicked_at = ?"
        " WHERE id = ? AND user_id = ?",
        (article_id, _now_iso(), history_id, user_id),
    )
    conn.commit()
    return int(cur.rowcount) > 0


def delete_search_history(conn: sqlite3.Connection, *, user_id: int, history_id: int) -> bool:
    cur = conn.execute(
        "DELETE FROM user_search_history WHERE id = ? AND user_id = ?",
        (history_id, user_id),
    )
    conn.commit()
    return int(cur.rowcount) > 0


def clear_search_history(conn: sqlite3.Connection, *, user_id: int) -> int:
    cur = conn.execute("DELETE FROM user_search_history WHERE user_id = ?", (user_id,))
    conn.commit()
    return int(cur.rowcount)

# ------------------------------------------------------------------ preferences
def _preference_row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    try:
        item["industries"] = json.loads(item.pop("industries_json") or "[]")
    except (TypeError, ValueError):
        item["industries"] = []
    item["push_enabled"] = bool(item.get("push_enabled", 1))
    return item


def get_preferences(conn: sqlite3.Connection, user_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM user_preferences WHERE user_id = ?", (user_id,)).fetchone()
    if row is None:
        now = _now_iso()
        conn.execute(
            "INSERT INTO user_preferences (user_id, industries_json, push_enabled, digest_time, updated_at)"
            " VALUES (?, '[]', 1, '09:00', ?)",
            (user_id, now),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM user_preferences WHERE user_id = ?", (user_id,)).fetchone()
    assert row is not None
    return _preference_row(row)


def save_preferences(
    conn: sqlite3.Connection,
    user_id: int,
    *,
    industries: list[str],
    push_enabled: bool,
    digest_time: str,
) -> dict[str, Any]:
    conn.execute(
        """INSERT INTO user_preferences (user_id, industries_json, push_enabled, digest_time, updated_at)
           VALUES (?,?,?,?,?)
           ON CONFLICT(user_id) DO UPDATE SET
             industries_json = excluded.industries_json,
             push_enabled = excluded.push_enabled,
             digest_time = excluded.digest_time,
             updated_at = excluded.updated_at""",
        (user_id, json.dumps(industries, ensure_ascii=False), int(push_enabled), digest_time, _now_iso()),
    )
    conn.commit()
    return get_preferences(conn, user_id)


# ------------------------------------------------------------------ operation logs
def log_operation(
    conn: sqlite3.Connection,
    *,
    user_id: int | None,
    username: str,
    action: str,
    target: str = "",
    detail: str = "",
    ip: str = "",
    commit: bool = True,
) -> None:
    conn.execute(
        "INSERT INTO operation_logs (user_id, username, action, target, detail, ip, created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (user_id, username, action, target, detail[:1000], ip, _now_iso()),
    )
    if commit:
        conn.commit()


def list_operation_logs(
    conn: sqlite3.Connection,
    *,
    action: str = "",
    username: str = "",
    date_from: str = "",
    date_to: str = "",
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    clauses: list[str] = []
    args: list[Any] = []
    if action:
        clauses.append("action = ?")
        args.append(action)
    if username:
        clauses.append("username LIKE ?")
        args.append(f"%{username}%")
    if date_from:
        clauses.append("substr(created_at, 1, 10) >= ?")
        args.append(date_from[:10])
    if date_to:
        clauses.append("substr(created_at, 1, 10) <= ?")
        args.append(date_to[:10])
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    total = int(conn.execute(f"SELECT COUNT(*) FROM operation_logs {where}", args).fetchone()[0])
    rows = conn.execute(
        f"SELECT * FROM operation_logs {where} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
        [*args, limit, offset],
    ).fetchall()
    return [dict(row) for row in rows], total


# ------------------------------------------------------------------ sites
def _site_row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["enabled"] = bool(item.get("enabled", 1))
    return item


def list_sites(conn: sqlite3.Connection, *, enabled: bool | None = None) -> list[dict[str, Any]]:
    where = "WHERE enabled = ?" if enabled is not None else ""
    args: list[Any] = [int(enabled)] if enabled is not None else []
    rows = conn.execute(f"SELECT * FROM sites {where} ORDER BY id ASC", args).fetchall()
    return [_site_row(row) for row in rows]


def create_site(
    conn: sqlite3.Connection,
    *,
    name: str,
    list_url: str,
    category: str = "",
    region: str = "",
    timezone: str = "Asia/Shanghai",
    enabled: bool = True,
) -> dict[str, Any]:
    now = _now_iso()
    cur = conn.execute(
        """INSERT INTO sites (name, list_url, category, region, timezone, enabled, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (name, list_url, category, region, timezone, int(enabled), now, now),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM sites WHERE id = ?", (int(cur.lastrowid or 0),)).fetchone()
    assert row is not None
    return _site_row(row)


def update_site(
    conn: sqlite3.Connection,
    site_id: int,
    *,
    name: str | None = None,
    list_url: str | None = None,
    category: str | None = None,
    region: str | None = None,
    timezone: str | None = None,
    enabled: bool | None = None,
) -> dict[str, Any] | None:
    sets: list[str] = []
    args: list[Any] = []
    for column, value in (
        ("name", name),
        ("list_url", list_url),
        ("category", category),
        ("region", region),
        ("timezone", timezone),
    ):
        if value is not None:
            sets.append(f"{column} = ?")
            args.append(value)
    if enabled is not None:
        sets.append("enabled = ?")
        args.append(int(enabled))
    if not sets:
        row = conn.execute("SELECT * FROM sites WHERE id = ?", (site_id,)).fetchone()
        return _site_row(row) if row is not None else None
    sets.append("updated_at = ?")
    args.append(_now_iso())
    args.append(site_id)
    conn.execute(f"UPDATE sites SET {', '.join(sets)} WHERE id = ?", args)
    conn.commit()
    row = conn.execute("SELECT * FROM sites WHERE id = ?", (site_id,)).fetchone()
    return _site_row(row) if row is not None else None


def delete_site(conn: sqlite3.Connection, site_id: int) -> bool:
    """删除站点。

    这里必须真正 DELETE，不能只把 enabled 置 0：enabled 同时承担「启用/禁用」开关，
    若删除也复用它，管理页会把已删除的站点继续列出来（只是状态显示停用），
    用户点了删除却看到记录还在；同时 name 的 UNIQUE 约束会让同名站点无法重建。
    """
    cur = conn.execute("DELETE FROM sites WHERE id = ?", (site_id,))
    conn.commit()
    return int(cur.rowcount) > 0


# ------------------------------------------------------------------ admin stats
def admin_summary(conn: sqlite3.Connection, *, days: int = 7) -> dict[str, Any]:
    days = max(1, min(90, days))
    start_date = _local_today() - dt.timedelta(days=days - 1)
    start = start_date.isoformat()
    # 文章发布时间是站点墙钟（按本地日比较），作业/运行/日志时间戳是 UTC ISO；
    # 两者不能共用一个字符串边界，否则本地窗口首日 00:00–08:00 的记录会被漏掉。
    start_utc_dt = dt.datetime.combine(start_date, dt.time.min, tzinfo=_CST).astimezone(
        dt.timezone.utc
    )
    # operation_logs 用 ISO「T…Z」，runs/analysis_jobs 用「YYYY-MM-DD HH:MM:SS」；
    # 同一时刻必须按各自的存储格式生成边界，字符串比较才不会在日期相同时按第 11 位
    # （空格 0x20 < T 0x54）把窗口内的记录判到窗口外。
    start_utc = start_utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    start_utc_space = start_utc_dt.strftime("%Y-%m-%d %H:%M:%S")
    total_articles = int(conn.execute("SELECT COUNT(*) FROM analyzed_articles").fetchone()[0])
    total_sources = int(
        conn.execute("SELECT COUNT(DISTINCT source) FROM analyzed_articles").fetchone()[0]
    )
    total_users = count_users(conn)
    active_users = int(
        conn.execute(
            "SELECT COUNT(DISTINCT user_id) FROM operation_logs"
            " WHERE user_id IS NOT NULL AND created_at >= ?",
            (start_utc,),
        ).fetchone()[0]
    )
    daily_rows = conn.execute(
        """SELECT substr(COALESCE(published_at, analyzed_at), 1, 10) AS day, COUNT(*) AS count
           FROM analyzed_articles
           WHERE substr(COALESCE(published_at, analyzed_at), 1, 10) >= ?
           GROUP BY day ORDER BY day ASC""",
        (start,),
    ).fetchall()
    category_rows = conn.execute(
        "SELECT COALESCE(category, '') AS category, COUNT(*) AS count"
        " FROM analyzed_articles GROUP BY COALESCE(category, '') ORDER BY count DESC"
    ).fetchall()
    # 管理台必须同时看到两条执行链：API 提交的 analysis_jobs 和 CLI/定时任务写入
    # 的 runs。只统计前者时，线上实际跑的分析（CLI）会让「成功率」「任务数」看起
    # 来永远是 0/100。runs 没有 status 列，用 errors 是否为 0 归类；未完成的 run
    # （finished_at 为空）不计入成功率分母，避免刚启动就被算成失败。
    # days 是「最近 N 天」；作业/运行统计必须与 daily 同一窗口，否则看板
    # 一边显示近 7 天文章、一边把全部历史任务算进成功率。
    job_rows = conn.execute(
        "SELECT status, COUNT(*) AS count FROM analysis_jobs WHERE created_at >= ? GROUP BY status",
        (start_utc_space,),
    ).fetchall()
    jobs = {str(row["status"]): int(row["count"]) for row in job_rows}
    run_stats = conn.execute(
        """SELECT
             SUM(CASE WHEN finished_at IS NOT NULL AND errors = 0 THEN 1 ELSE 0 END) AS succeeded,
             SUM(CASE WHEN finished_at IS NOT NULL AND errors > 0 THEN 1 ELSE 0 END) AS failed
           FROM runs WHERE started_at >= ?""",
        (start_utc_space,),
    ).fetchone()
    run_succeeded = int(run_stats["succeeded"] or 0) if run_stats else 0
    run_failed = int(run_stats["failed"] or 0) if run_stats else 0
    jobs["succeeded"] = jobs.get("succeeded", 0) + run_succeeded
    jobs["failed"] = jobs.get("failed", 0) + run_failed
    finished = jobs.get("succeeded", 0) + jobs.get("failed", 0)
    success_rate = round(jobs.get("succeeded", 0) * 100.0 / finished, 1) if finished else 100.0
    return {
        "days": days,
        "range_start": start,
        "totals": {
            "articles": total_articles,
            "sources": total_sources,
            "users": total_users,
            "active_users": active_users,
        },
        "daily": [dict(row) for row in daily_rows],
        "categories": [dict(row) for row in category_rows],
        "jobs": jobs,
        "success_rate": success_rate,
    }
