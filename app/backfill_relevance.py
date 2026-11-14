"""Backfill the semantic relevance score for already analyzed articles.

Re-running `analyze --force` would repeat tagging and extraction, which are the
expensive LLM calls whose answers already exist. This module only fills the
relevance columns; every other derived field is left untouched.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from app import db
from app.llm import LLMClient
from app.pipeline import connect
from app.relevance import RelevanceHit, score_relevance
from app.settings import Settings

LOG = logging.getLogger("news_insight.backfill_relevance")


def _missing_relevance(conn, limit: int) -> list[dict[str, Any]]:
    limit = max(limit, 0)
    rows = conn.execute(
        "SELECT article_id, title, body, category, category_confidence FROM analyzed_articles"
        " WHERE relevance_score IS NULL"
        " ORDER BY COALESCE(published_at, analyzed_at) DESC, article_id DESC"
        " LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def _score(settings: Settings, llm: LLMClient, row: dict[str, Any]) -> tuple[str, RelevanceHit]:
    hit = score_relevance(
        settings,
        llm,
        title=row["title"] or "",
        body=row["body"] or "",
        category=row["category"] or "",
        category_confidence=row["category_confidence"],
    )
    return row["article_id"], hit


def backfill_relevance(settings: Settings, *, limit: int = 500, workers: int | None = None) -> dict[str, Any]:
    if limit < 0:
        raise ValueError(f"limit 不能为负数（收到 {limit}）")
    db.init_db(settings.db_path)
    conn = connect(settings)
    llm = LLMClient(settings)
    scanned = analyzed = skipped = errors = 0
    engines: dict[str, int] = {}
    try:
        rows = _missing_relevance(conn, limit)
        scanned = len(rows)
        if not rows:
            return {"scanned": 0, "analyzed": 0, "skipped": 0, "errors": 0, "remaining": 0, "engines": {}}
        workers = workers if workers is not None else settings.analyze_concurrency
        workers = max(1, min(int(workers), 100, len(rows)))
        LOG.info("待回填语义相关性 %d 篇，并发=%d", len(rows), workers)
        results: list[tuple[str, RelevanceHit]] = []
        if workers == 1:
            for row in rows:
                try:
                    results.append(_score(settings, llm, row))
                except Exception:
                    LOG.exception("语义相关性评分失败 %s", row["article_id"])
                    errors += 1
        else:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {executor.submit(_score, settings, llm, row): row for row in rows}
                for future in as_completed(futures):
                    row = futures[future]
                    try:
                        results.append(future.result())
                    except Exception:
                        LOG.exception("语义相关性评分失败 %s", row["article_id"])
                        errors += 1
        for article_id, hit in results:
            if db.set_relevance(
                conn,
                article_id=article_id,
                score=hit.score,
                engine=hit.engine,
                reason=hit.reason,
                commit=False,
            ):
                analyzed += 1
                engines[hit.engine] = engines.get(hit.engine, 0) + 1
            else:
                skipped += 1
        conn.commit()
        remaining = conn.execute(
            "SELECT COUNT(*) FROM analyzed_articles WHERE relevance_score IS NULL"
        ).fetchone()[0]
    finally:
        conn.close()
        llm.close()
    return {
        "scanned": scanned,
        "analyzed": analyzed,
        "skipped": skipped,
        "errors": errors,
        "remaining": int(remaining),
        "engines": engines,
    }
