"""Retention: delete analysis results older than RETENTION_DAYS."""

from __future__ import annotations

import logging
import sqlite3

from app import db
from app.settings import Settings

LOG = logging.getLogger("news_insight.prune")


def prune(
    settings: Settings,
    *,
    days: int | None = None,
    dry_run: bool = False,
    do_vacuum: bool = False,
) -> dict:
    retention = settings.retention_days if days is None else days
    if retention <= 0:
        return {
            "skipped": True,
            "reason": "RETENTION_DAYS<=0，保留全部数据",
            "retention_days": retention,
        }
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    db.init_db(settings.db_path)
    conn = db.connect(settings.db_path)
    try:
        try:
            result = db.prune_old_data(conn, retention_days=retention, dry_run=dry_run)
        except sqlite3.OperationalError as exc:
            # 只读部署下清理整个跑不了，这不是错误而是部署形态的既定差异：把
            # ERROR + traceback 降成 DEBUG + 一句结论，避免每次启动都刷一屏
            # 「看起来坏了」的日志，真出问题（磁盘满、schema 缺列）仍按 ERROR 抛。
            if db.is_readonly_error(exc):
                LOG.debug("数据库只读，跳过保留期清理：%s", exc)
                return {
                    "skipped": True,
                    "reason": "数据库只读，跳过清理",
                    "retention_days": retention,
                }
            raise
        if do_vacuum and not dry_run:
            db.vacuum(conn)
            result["vacuumed"] = True
        LOG.info(
            "清理完成 retention_days=%s dry_run=%s deleted=%s",
            retention,
            dry_run,
            result.get("deleted"),
        )
        return result
    finally:
        conn.close()
