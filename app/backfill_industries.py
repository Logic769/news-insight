"""Backfill the controlled industry field for already analyzed articles.

Analyzing 480 rows with `analyze --force` would re-run tagging and extraction
(expensive LLM calls whose answers already exist). This module only fills the
industry columns; every other derived field is left untouched.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from app import db
from app.llm import LLMClient
from app.pipeline import connect
from app.settings import Settings
from app.tagging import IndustryHit, classify_industry, load_industries

LOG = logging.getLogger("news_insight.backfill_industries")


def _missing_industries(conn, limit: int) -> list[dict[str, Any]]:
    limit = max(limit, 0)
    rows = conn.execute(
        "SELECT article_id, title, body FROM analyzed_articles"
        " WHERE industry IS NULL OR industry = ''"
        " ORDER BY COALESCE(published_at, analyzed_at) DESC, article_id DESC"
        " LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def _classify(settings: Settings, llm: LLMClient, industries, row: dict[str, Any]) -> tuple[str, IndustryHit]:
    hit = classify_industry(settings, llm, industries, row["title"] or "", row["body"] or "")
    return row["article_id"], hit


def backfill_industries(settings: Settings, *, limit: int = 500, workers: int | None = None) -> dict[str, Any]:
    if limit < 0:
        raise ValueError(f"limit 不能为负数（收到 {limit}）")
    db.init_db(settings.db_path)
    _, industries = load_industries(settings.vocab_dir)
    if not industries:
        raise RuntimeError("industries.json 不存在或为空，无法回填行业")
    conn = connect(settings)
    llm = LLMClient(settings)
    scanned = analyzed = skipped = errors = 0
    engines: dict[str, int] = {}
    try:
        rows = _missing_industries(conn, limit)
        scanned = len(rows)
        if not rows:
            return {"scanned": 0, "analyzed": 0, "skipped": 0, "errors": 0, "remaining": 0, "engines": {}}
        workers = workers if workers is not None else settings.analyze_concurrency
        workers = max(1, min(int(workers), 100, len(rows)))
        LOG.info("待回填行业 %d 篇，并发=%d", len(rows), workers)
        results: list[tuple[str, IndustryHit]] = []
        if workers == 1:
            for row in rows:
                try:
                    results.append(_classify(settings, llm, industries, row))
                except Exception:
                    LOG.exception("行业分类失败 %s", row["article_id"])
                    errors += 1
        else:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {executor.submit(_classify, settings, llm, industries, row): row for row in rows}
                for future in as_completed(futures):
                    row = futures[future]
                    try:
                        results.append(future.result())
                    except Exception:
                        LOG.exception("行业分类失败 %s", row["article_id"])
                        errors += 1
        for article_id, hit in results:
            if not hit.industry_id:
                skipped += 1
                continue
            if db.set_industry(
                conn,
                article_id=article_id,
                industry=hit.industry_id,
                confidence=hit.confidence,
                engine=hit.engine,
                commit=False,
            ):
                analyzed += 1
                engines[hit.engine] = engines.get(hit.engine, 0) + 1
            else:
                skipped += 1
        conn.commit()
        remaining = conn.execute(
            "SELECT COUNT(*) FROM analyzed_articles WHERE industry IS NULL OR industry = ''"
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
