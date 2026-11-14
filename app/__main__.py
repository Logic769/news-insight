"""CLI entry: `python -m app analyze --limit 10`."""

from __future__ import annotations

import argparse
import json
import logging
import sys

from app import db
from app.backfill import BackfillUsageError, backfill_bodies
from app.backfill_industries import backfill_industries
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


    p_bf = sub.add_parser("backfill-bodies", help="从新闻 API 补正文与正文 HTML（不调用 LLM，可重复运行）")
    p_bf.add_argument("--limit", type=int, default=500)
    p_bf.add_argument("--batch", type=int, default=50)

    p_bi = sub.add_parser("backfill-industries", help="为已分析文章补受控行业分类（可重复运行）")
    p_bi.add_argument("--limit", type=int, default=500)
    p_bi.add_argument("--workers", type=int, default=None)

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
    if args.cmd == "backfill-bodies":
        try:
            result = backfill_bodies(settings, limit=args.limit, batch=args.batch)
        except (RuntimeError, BackfillUsageError) as exc:
            # demo 模式拒绝回填 / 参数不合法：都是使用问题，不是崩溃，别让用户看 traceback。
            # 只捕 BackfillUsageError：裸 ValueError 会把内部意外 bug 也静默成退出码 2。
            print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
            return 2
        print(json.dumps(result, ensure_ascii=False))
        return 0
    if args.cmd == "backfill-industries":
        result = backfill_industries(settings, limit=args.limit, workers=args.workers)
        print(json.dumps(result, ensure_ascii=False))
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
