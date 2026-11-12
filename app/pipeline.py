"""Orchestration: pull -> tag -> extract -> persist."""

from __future__ import annotations

import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any

from app import db
from app.checklist import build_checklist, to_dicts
from app.extraction import extract_article, load_schemas
from app.llm import LLMClient
from app.news_client import NewsClient, UpstreamError
from app.settings import Settings
from app.tagging import (
    FALLBACK_CATEGORY,
    classify_article,
    load_categories,
    load_vocab,
    tag_article,
    tag_with_rules,
    tags_for_category,
    tags_for_unclassified,
)

LOG = logging.getLogger("news_insight.pipeline")


def _field_payload(fields) -> tuple[dict, dict, dict]:
    """Split FieldValue objects into value/raw_text/verified maps for the checklist."""
    values = {f.name: f.value for f in fields}
    raw_texts = {f.name: f.raw_text for f in fields if f.raw_text}
    verified = {f.name: f.verified for f in fields}
    return values, raw_texts, verified


@dataclass
class _ChecklistRegen:
    """A skipped article whose derived checklist is missing and can be rebuilt."""

    article: Any
    fields: dict
    raw_texts: dict
    verified: dict


def _checklist_regen_source(conn, art) -> _ChecklistRegen | None:
    """Read what a missing checklist needs, on the main thread (SQLite owner)."""
    detail = db.get_article(conn, art.id)
    if not detail:
        return None
    if "project_application" not in {t["tag"] for t in detail.get("tags", [])}:
        return None
    if db.get_checklist(conn, art.id) is not None:
        return None
    block = detail.get("extractions", {}).get("project_application") or {}
    if not block or not art.body.strip():
        return None
    return _ChecklistRegen(
        article=art,
        fields={name: info["value"] for name, info in block.items()},
        raw_texts={name: info["raw_text"] for name, info in block.items() if info.get("raw_text")},
        verified={name: info["verified"] for name, info in block.items()},
    )


def _build_missing_checklist(settings: Settings, llm: LLMClient, regen: _ChecklistRegen):
    """LLM-only worker body; persistence stays on the main thread."""
    checklist = build_checklist(
        llm,
        regen.article.title,
        regen.article.body,
        regen.fields,
        raw_texts=regen.raw_texts,
        verified=regen.verified,
        body_chars=settings.extract_body_chars,
    )
    return regen.article.id, checklist


@dataclass
class _AnalysisResult:
    """Everything one worker produced for one article; persistence happens on the main thread."""

    article: Any
    tag_engine: str | None = None
    category: str = ""
    category_confidence: float | None = None
    category_engine: str = ""
    hits: list = field(default_factory=list)
    results: list = field(default_factory=list)
    checklist: tuple[str, list] | None = None
    error: str | None = None


def _analyze_one(settings: Settings, llm: LLMClient, tags, schemas, art, categories=()) -> _AnalysisResult:
    """Run all LLM calls for one article. Must not touch SQLite: workers are stateless.

    两级：先给文章定唯一大类，再（且只在该类允许的标签里）打关键词标签。
    'other' 这类没有下游标签的大类直接跳过第二级，省一次模型调用。
    """
    result = _AnalysisResult(article=art)
    try:
        scope = list(tags)
        if categories:
            hit = classify_article(settings, llm, categories, art.title, art.body)
            result.category = hit.category_id
            result.category_confidence = hit.confidence if hit.category_id else None
            result.category_engine = hit.engine
            scope = tags_for_category(categories, tags, hit.category_id)
            if hit.category_id == FALLBACK_CATEGORY:
                # 兜底类不做语义打标（省一次模型调用），只用规则引擎保住行业维度标签
                result.hits = tag_with_rules(
                    tags_for_unclassified(tags),
                    art.title,
                    art.body,
                    max_tags=settings.max_tags_per_article,
                    body_chars=settings.tag_body_chars,
                )
                result.tag_engine = "rules" if result.hits else ""
                return result
            if not scope:
                # 类内白名单为空：明确记为未打标，而不是遗留上一次的引擎值
                LOG.warning("大类 %r 的标签白名单为空，跳过二级打标", hit.category_id)
                result.tag_engine = ""
                return result
        hits, tag_engine = tag_article(settings, llm, scope, art.title, art.body)
        result.hits = hits
        result.tag_engine = tag_engine
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
        result.results = results
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
                result.checklist = (checklist.engine, to_dicts(checklist.items))
            except Exception:  # 清单失败不能把整篇标记为错误
                LOG.exception("生成申报清单失败 %s", art.id)
    except Exception as exc:  # one bad article must not sink the batch
        result.error = str(exc) or exc.__class__.__name__
    return result


def _persist_result(conn, result: _AnalysisResult) -> None:
    """Write one result using savepoints only; caller batches the real commits."""
    art = result.article
    extract_engine = result.results[0].engine if result.results else "none"
    flat = [
        (r.schema_id, f.name, f.value, f.raw_text, f.confidence, f.verified)
        for r in result.results
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
        tag_engine=result.tag_engine or "",
        extract_engine=extract_engine,
        tags=[(h.tag_id, h.confidence, h.evidence, h.engine) for h in result.hits],
        extractions=flat,
        category=result.category,
        category_confidence=result.category_confidence,
        category_engine=result.category_engine,
        commit=False,
    )
    if result.checklist is not None:
        engine, items = result.checklist
        try:
            db.save_checklist(conn, article_id=art.id, engine=engine, items=items, commit=False)
        except Exception:  # 清单是派生视图，失败不影响已抽取事实
            LOG.exception("保存申报清单失败 %s", art.id)
    elif not any(h.tag_id == "project_application" for h in result.hits):
        # 只有本次打标也确认不再命中时才作废旧清单，避免抽取抖动误删
        try:
            if db.get_checklist(conn, art.id) is not None:
                db.delete_checklist(conn, art.id, commit=False)
        except Exception:
            LOG.exception("删除旧清单失败 %s", art.id)


def analyze_batch(
    settings: Settings,
    *,
    limit: int,
    force: bool = False,
    concurrency: int | None = None,
) -> dict:
    workers = concurrency if concurrency is not None else settings.analyze_concurrency
    workers = max(1, min(int(workers), 200))

    _, tags = load_vocab(settings.vocab_dir)
    _, categories = load_categories(settings.vocab_dir)
    schemas = load_schemas(settings.vocab_dir / "schemas")
    llm = LLMClient(settings)
    client = NewsClient(settings)

    # 幂等建表：API 走 lifespan 已建过，但 CLI/库调用不应依赖调用方先初始化
    db.init_db(settings.db_path)
    conn = db.connect(settings.db_path)
    run_id: int | None = None
    run_finished = False
    scanned = analyzed = skipped = errors = 0
    # 已真正 COMMIT 的篇数：异常路径用它写 runs，避免把回滚掉的篇数算进 analyzed。
    analyzed_committed = 0
    try:
        mode_guess = settings.news_source_mode
        run_id = db.record_run_start(conn, mode_guess)

        try:
            articles, mode = client.list_articles(limit=limit)
        except UpstreamError as exc:
            db.record_run_finish(conn, run_id, scanned=0, analyzed=0, skipped=0, errors=1, detail=str(exc))
            run_finished = True
            raise

        conn.execute("UPDATE runs SET mode = ? WHERE id = ?", (mode, run_id))
        conn.commit()

        articles = client.hydrate_bodies(articles, mode)

        scanned = len(articles)
        engine_counts = {"llm": 0, "rules": 0}
        error_notes: list[str] = []
        if mode == "demo" and settings.news_source_mode == "auto" and client.fallback_reason:
            note = f"上游不可用，已回退演示数据: {client.fallback_reason}"
            error_notes.append(note)
            LOG.warning(note)

        analyzed_set = set() if force else db.analyzed_ids(conn)
        pending: list[Any] = []
        regen_pending: list[_ChecklistRegen] = []
        for art in articles:
            if not art.id or not art.title:
                skipped += 1
                continue
            if art.id in analyzed_set:
                # 已分析但缺清单（上次生成失败）时补生成，否则清单会永久缺失
                regen = _checklist_regen_source(conn, art)
                if regen is not None:
                    regen_pending.append(regen)
                skipped += 1
                continue
            if not art.body_ok or not art.body.strip():
                # 只拿标题打标会把「确无截止日」永久定档：宁可这次不入库，让下次自然重试
                errors += 1
                error_notes.append(f"{art.id}: 正文缺失，待重试")
                LOG.warning("正文缺失，跳过分析（不入库，下次重试） %s", art.id)
                continue
            pending.append(art)

        if regen_pending:
            # 历史文章缺清单不应串行阻塞整批：与正常分析共用同一并发池的容量。
            regen_workers = max(1, min(workers, len(regen_pending)))
            LOG.info("补生成缺失清单 %d 篇，并发=%d", len(regen_pending), regen_workers)
            built = []
            if regen_workers == 1 or len(regen_pending) == 1:
                for item in regen_pending:
                    try:
                        built.append(_build_missing_checklist(settings, llm, item))
                    except Exception:
                        LOG.exception("补生成清单失败 %s", item.article.id)
            else:
                with ThreadPoolExecutor(max_workers=regen_workers) as regen_executor:
                    futures = {
                        regen_executor.submit(_build_missing_checklist, settings, llm, item): item
                        for item in regen_pending
                    }
                    for future in as_completed(futures):
                        item = futures[future]
                        try:
                            built.append(future.result())
                        except Exception:
                            LOG.exception("补生成清单失败 %s", item.article.id)
            for article_id, checklist in built:
                try:
                    db.save_checklist(
                        conn,
                        article_id=article_id,
                        engine=checklist.engine,
                        items=to_dicts(checklist.items),
                        commit=False,
                    )
                    LOG.info("补生成缺失清单 %s", article_id)
                except Exception:  # 补生成失败不影响本次 skip 语义
                    LOG.exception("保存补生成清单失败 %s", article_id)
            conn.commit()

        LOG.info("开始分析 %d 篇，并发=%d", len(pending), workers)
        executor: ThreadPoolExecutor | None = None
        if workers == 1 or len(pending) <= 1:
            results_iter = (_analyze_one(settings, llm, tags, schemas, art, categories) for art in pending)
        else:
            executor = ThreadPoolExecutor(max_workers=min(workers, len(pending)))
            analyze_futures = [
                executor.submit(_analyze_one, settings, llm, tags, schemas, art, categories) for art in pending
            ]
            results_iter = (f.result() for f in as_completed(analyze_futures))

        uncommitted = 0
        done = 0
        try:
            for result in results_iter:
                done += 1
                if result.tag_engine:
                    engine_counts[result.tag_engine] = engine_counts.get(result.tag_engine, 0) + 1
                if result.error is not None:
                    errors += 1
                    error_notes.append(f"{result.article.id}: {result.error}")
                    LOG.error("分析文章失败 %s: %s", result.article.id, result.error)
                else:
                    try:
                        _persist_result(conn, result)
                        analyzed += 1
                        uncommitted += 1
                    except Exception as exc:  # savepoint rolls back this article only
                        errors += 1
                        error_notes.append(f"{result.article.id}: 落库失败: {exc}")
                        LOG.exception("落库失败 %s", result.article.id)
                if uncommitted >= settings.analyze_write_batch:
                    conn.commit()
                    analyzed_committed = analyzed
                    uncommitted = 0
                if done % 100 == 0:
                    LOG.info("分析进度 %d/%d（成功 %d，失败 %d）", done, len(pending), analyzed, errors)
        finally:
            if executor is not None:
                executor.shutdown(wait=True)
        conn.commit()
        analyzed_committed = analyzed

        detail = "; ".join(error_notes[:10])
        db.record_run_finish(
            conn, run_id, scanned=scanned, analyzed=analyzed, skipped=skipped, errors=errors, detail=detail
        )
        run_finished = True
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
    except Exception as exc:
        # 任何中途异常（详情补全、落库、提交）都要收尾 runs，避免永久“运行中”孤儿行。
        try:
            conn.rollback()
        except Exception:
            LOG.exception("回滚未提交事务失败")
        if run_id is not None and not run_finished:
            try:
                db.record_run_finish(
                    conn,
                    run_id,
                    scanned=scanned,
                    analyzed=analyzed_committed,
                    skipped=skipped,
                    errors=max(errors, 1),
                    detail=f"{type(exc).__name__}: {exc}",
                )
            except Exception:
                LOG.exception("记录失败运行状态失败 run_id=%s", run_id)
        raise
    finally:
        client.close()
        llm.close()
        conn.close()


def connect(settings: Settings) -> sqlite3.Connection:
    return db.connect(settings.db_path)
