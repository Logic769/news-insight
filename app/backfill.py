"""Backfill article bodies into insight.db without re-running the model.

Why this exists
----------------
`body` is only populated for articles analyzed after the column existed. Every
row analyzed before that has `body IS NULL`, which makes keyword search blind
to正文 for the entire back catalogue. Re-running `analyze --force` would fix it
but burns LLM tokens for results we already have -- the body is a copy of
upstream data, not an analysis output.

So this module only moves text: read the crawler API, write `body`, leave every
derived field (category, tags, extractions) untouched.
"""

from __future__ import annotations

import logging
from typing import Any

from app import db
from app.news_client import NewsClient, UpstreamError
from app.pipeline import connect
from app.settings import Settings

LOG = logging.getLogger("news_insight.backfill")


def _articles_missing_body(conn: Any, limit: int) -> list[dict[str, Any]]:
    """Rows analyzed but without a body, newest first.

    Newest first because that is what users search: the backfill is resumable,
    so running it with a small --limit should fix the most visible rows first.
    """
    rows = conn.execute(
        "SELECT article_id, url FROM analyzed_articles"
        " WHERE body IS NULL OR body = ''"
        " ORDER BY COALESCE(published_at, analyzed_at) DESC"
        " LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def backfill_bodies(settings: Settings, *, limit: int = 500, batch: int = 50) -> dict[str, Any]:
    """Copy bodies from the crawler API into insight.db.

    Returns counts rather than raising on individual failures: a dead upstream
    link must not abort a 500-row run, and the caller reruns until `missing`
    stops shrinking.
    """
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    db.init_db(settings.db_path)
    conn = connect(settings)
    client = NewsClient(settings)
    filled = 0
    failed = 0
    skipped = 0
    try:
        pending = _articles_missing_body(conn, limit)
        LOG.info("待回填正文: %d 篇", len(pending))
        for start in range(0, len(pending), batch):
            chunk = pending[start : start + batch]
            for row in chunk:
                article_id = row["article_id"]
                try:
                    payload = client.get_article(article_id)
                except UpstreamError as exc:
                    LOG.warning("取正文失败 %s: %s", article_id, exc)
                    failed += 1
                    continue
                body = (payload or {}).get("raw_content") or (payload or {}).get("content") or ""
                if not body.strip():
                    # 上游确实没有正文：不算失败，也不写库。代价是这条行仍然是
                    # "缺正文"状态，下一轮还会被选中并再发一次请求——所以
                    # remaining 里始终包含这类不可回填的行，跑几轮都不会归零。
                    # 想永久跳过它们需要一个失败清单，本命令刻意不做（避免在库外
                    # 引入隐式状态）。
                    skipped += 1
                    continue
                db.set_body(conn, article_id=article_id, body=body, commit=False)
                filled += 1
            conn.commit()
            LOG.info("已回填 %d/%d", filled, len(pending))
        remaining = conn.execute(
            "SELECT COUNT(*) FROM analyzed_articles WHERE body IS NULL OR body = ''"
        ).fetchone()[0]
    finally:
        conn.close()
        client.close()
    return {
        "filled": filled,
        "failed": failed,
        # skipped：上游此刻没有正文，不写库、下一轮还会再试
        "skipped": skipped,
        # remaining：仍然缺正文的行数，含 skipped 这类不可回填的，不一定能归零
        "remaining": int(remaining),
    }
