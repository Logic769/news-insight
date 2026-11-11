"""Retention: delete analysis results older than RETENTION_DAYS."""

from __future__ import annotations

import logging

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
        result = db.prune_old_data(conn, retention_days=retention, dry_run=dry_run)
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
