"""Orchestration: pull -> tag -> extract -> persist."""

from __future__ import annotations

import logging
import sqlite3

from app import db
from app.checklist import build_checklist, to_dicts
from app.extraction import extract_article, load_schemas
from app.llm import LLMClient
from app.news_client import NewsClient, UpstreamError
from app.settings import Settings
from app.tagging import load_vocab, tag_article

LOG = logging.getLogger("news_insight.pipeline")


def _field_payload(fields) -> tuple[dict, dict, dict]:
    """Split FieldValue objects into value/raw_text/verified maps for the checklist."""
    values = {f.name: f.value for f in fields}
    raw_texts = {f.name: f.raw_text for f in fields if f.raw_text}
    verified = {f.name: f.verified for f in fields}
    return values, raw_texts, verified


def _maybe_regenerate_checklist(settings: Settings, llm: LLMClient, conn, art) -> None:
    """Regenerate a missing checklist for an already-analyzed article.

    Idempotent skip would otherwise make a transient checklist failure permanent:
    the article is marked analyzed, so later runs never touch it again. Only the
    derived checklist is rebuilt -- tags and extractions stay untouched.
    """
    detail = db.get_article(conn, art.id)
    if not detail:
        return
    if "project_application" not in {t["tag"] for t in detail.get("tags", [])}:
        return
    if db.get_checklist(conn, art.id) is not None:
        return
    block = detail.get("extractions", {}).get("project_application") or {}
    if not block or not art.body.strip():
        return
    fields = {name: info["value"] for name, info in block.items()}
    raw_texts = {name: info["raw_text"] for name, info in block.items() if info.get("raw_text")}
    verified = {name: info["verified"] for name, info in block.items()}
    try:
        checklist = build_checklist(
            llm,
            art.title,
            art.body,
            fields,
            raw_texts=raw_texts,
            verified=verified,
            body_chars=settings.extract_body_chars,
        )
        db.save_checklist(
            conn,
            article_id=art.id,
            engine=checklist.engine,
            items=to_dicts(checklist.items),
        )
        LOG.info("补生成缺失清单 %s", art.id)
    except Exception:  # 补生成失败不影响本次 skip 语义
        LOG.exception("补生成清单失败 %s", art.id)


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
        if mode == "demo" and settings.news_source_mode == "auto" and client.fallback_reason:
            note = f"上游不可用，已回退演示数据: {client.fallback_reason}"
            error_notes.append(note)
            LOG.warning(note)

        for art in articles:
            scanned += 1
            if not art.id or not art.title:
                skipped += 1
                continue
            if not force and db.is_analyzed(conn, art.id):
                # 已分析但缺清单（上次生成失败）时补生成，否则清单会永久缺失
                _maybe_regenerate_checklist(settings, llm, conn, art)
                skipped += 1
                continue
            if not art.body.strip():
                # 只拿标题打标会把「确无截止日」永久定档：宁可这次不入库，让下次自然重试
                errors += 1
                error_notes.append(f"{art.id}: 正文缺失，待重试")
                LOG.warning("正文缺失，跳过分析（不入库，下次重试） %s", art.id)
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
                extract_engine = results[0].engine if results else "none"
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
                # 清单是派生视图：生成失败只记日志，不影响已入库的事实
                app_result = next(
                    (r for r in results if r.schema_id == "project_application" and r.fields),
                    None,
                )
                if app_result is not None:
                    try:
                        fields, raw_texts, verified = _field_payload(app_result.fields)
                        checklist = build_checklist(
                            llm,
                            art.title,
                            art.body,
                            fields,
                            raw_texts=raw_texts,
                            verified=verified,
                            body_chars=settings.extract_body_chars,
                        )
                        db.save_checklist(
                            conn,
                            article_id=art.id,
                            engine=checklist.engine,
                            items=to_dicts(checklist.items),
                        )
                    except Exception:  # 清单失败不能把整篇标记为错误
                        LOG.exception("生成申报清单失败 %s", art.id)
                elif not any(h.tag_id == "project_application" for h in hits):
                    # 只有本次打标也确认不再命中时才作废旧清单，避免抽取抖动误删
                    if db.get_checklist(conn, art.id) is not None:
                        db.delete_checklist(conn, art.id)
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
