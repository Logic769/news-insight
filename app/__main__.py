"""CLI entry: `python -m app analyze --limit 10`."""

from __future__ import annotations

import argparse
import json
import logging

from app import db
from app.pipeline import analyze_batch
from app.settings import load_settings


def main() -> int:
    parser = argparse.ArgumentParser(prog="news-insight")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_an = sub.add_parser("analyze", help="跑一批分析")
    p_an.add_argument("--limit", type=int, default=10)
    p_an.add_argument("--force", action="store_true")

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
        }, ensure_ascii=False))
        return 0

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    db.init_db(settings.db_path)
    result = analyze_batch(settings, limit=args.limit, force=args.force)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
