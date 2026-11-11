"""CLI entry: `python -m app analyze --limit 10`."""

from __future__ import annotations

import argparse
import json
import logging

from app import db
from app.pipeline import analyze_batch
from app.prune import prune
from app.settings import load_settings


def main() -> int:
    parser = argparse.ArgumentParser(prog="news-insight")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_an = sub.add_parser("analyze", help="跑一批分析")
    p_an.add_argument("--limit", type=int, default=10)
    p_an.add_argument("--force", action="store_true")
    p_an.add_argument("--concurrency", type=int, default=None, help="并发分析线程数，默认取 ANALYZE_CONCURRENCY")

    p_pr = sub.add_parser("prune", help="删除超过保留期的分析结果")
    p_pr.add_argument("--days", type=int, default=None, help="保留天数，默认取 RETENTION_DAYS")
    p_pr.add_argument("--dry-run", action="store_true", help="只统计不删除")
    p_pr.add_argument("--vacuum", action="store_true", help="清理后执行 VACUUM 回收磁盘")

    sub.add_parser("init", help="初始化数据库")
    sub.add_parser("health", help="打印当前配置")

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    settings = load_settings()

    if args.cmd == "init":
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        db.init_db(settings.db_path)
        print(json.dumps({"ok": True, "db": str(settings.db_path)}))
        return 0
    if args.cmd == "health":
        print(json.dumps({
            "llm_enabled": settings.llm_enabled,
            "news_source_mode": settings.news_source_mode,
            "news_base_url": settings.news_base_url,
            "db": str(settings.db_path),
            "retention_days": settings.retention_days,
        }, ensure_ascii=False))
        return 0
    if args.cmd == "prune":
        result = prune(
            settings, days=args.days, dry_run=args.dry_run, do_vacuum=args.vacuum
        )
        print(json.dumps(result, ensure_ascii=False))
        return 0

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    db.init_db(settings.db_path)
    result = analyze_batch(
        settings, limit=args.limit, force=args.force, concurrency=args.concurrency
    )
    # CLI 定时批处理也要执行保留期清理，否则只有 API/job 路径会删旧数据。
    try:
        prune(settings)
    except Exception:
        logging.getLogger("news_insight.main").exception("分析后清理过期数据失败")
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
