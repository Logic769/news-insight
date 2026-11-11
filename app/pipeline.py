"""Orchestration: pull -> tag -> extract -> persist."""

from __future__ import annotations

import logging
import sqlite3

from app import db
from app.extraction import extract_article, load_schemas
from app.llm import LLMClient
from app.news_client import NewsClient, UpstreamError
from app.settings import Settings
from app.tagging import load_vocab, tag_article

LOG = logging.getLogger("news_insight.pipeline")


def analyze_batch(settings: Settings, *, limit: int, force: bool = False) -> dict:
    _, tags = load_vocab(settings.vocab_dir)
    schemas = load_schemas(settings.vocab_dir / "schemas")
    llm = LLMClient(settings)
    client = NewsClient(settings)

    # 幂等建表：API 走 lifespan 已建过，但 CLI/库调用不应依赖调用方先初始化
    db.init_db(settings.db_path)
    conn = db.connect(settings.db_path)
    try:
        mode_guess = settings.news_source_mode
        run_id = db.record_run_start(conn, mode_guess)

        try:
            articles, mode = client.list_articles(limit=limit)
        except UpstreamError as exc:
            db.record_run_finish(conn, run_id, scanned=0, analyzed=0, skipped=0, errors=1, detail=str(exc))
            raise

        conn.execute("UPDATE runs SET mode = ? WHERE id = ?", (mode, run_id))
        conn.commit()

        articles = client.hydrate_bodies(articles, mode)

        scanned = analyzed = skipped = errors = 0
        engine_counts = {"llm": 0, "rules": 0}
        error_notes: list[str] = []

        for art in articles:
            scanned += 1
            if not art.id or not art.title:
                skipped += 1
                continue
            if not force and db.is_analyzed(conn, art.id):
                skipped += 1
                continue
            try:
                hits, tag_engine = tag_article(settings, llm, tags, art.title, art.body)
                engine_counts[tag_engine] = engine_counts.get(tag_engine, 0) + 1
                results = extract_article(
                    settings,
                    llm,
                    schemas,
                    hits,
                    art.title,
                    art.body,
                    published_at_utc=art.published_at_utc,
                    published_at=art.published_at,
                )
                extract_engine = results[0].engine if results else ""
                flat = [
                    (r.schema_id, f.name, f.value, f.raw_text, f.confidence, f.verified)
                    for r in results
                    for f in r.fields
                ]
                db.save_analysis(
                    conn,
                    article_id=art.id,
                    source=art.source,
                    url=art.url,
                    title=art.title,
                    published_at=art.published_at,
                    published_at_utc=art.published_at_utc,
                    content_length=art.content_length,
                    tag_engine=tag_engine,
                    extract_engine=extract_engine,
                    tags=[(h.tag_id, h.confidence, h.evidence, h.engine) for h in hits],
                    extractions=flat,
                )
                analyzed += 1
            except Exception as exc:  # one bad article must not sink the batch
                errors += 1
                error_notes.append(f"{art.id}: {exc}")
                LOG.exception("分析文章失败 %s", art.id)

        detail = "; ".join(error_notes[:10])
        db.record_run_finish(
            conn, run_id, scanned=scanned, analyzed=analyzed, skipped=skipped, errors=errors, detail=detail
        )
        return {
            "run_id": run_id,
            "mode": mode,
            "scanned": scanned,
            "analyzed": analyzed,
            "skipped": skipped,
            "errors": errors,
            "tag_engines": engine_counts,
            "llm_enabled": llm.available,
        }
    finally:
        conn.close()


def connect(settings: Settings) -> sqlite3.Connection:
    return db.connect(settings.db_path)
