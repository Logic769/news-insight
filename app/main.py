"""HTTP surface for news-insight.

Kept deliberately small: trigger an analysis batch, read results back, and
query extracted facts (the "project application deadline" example).
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app import __version__, db
from app.extraction import load_schemas
from app.jobs import ANALYZE_LOCK, JobManager, JobQueueFull
from app.pipeline import analyze_batch, connect
from app.prune import prune
from app.settings import Settings, load_settings
from app.tagging import load_vocab

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
LOG = logging.getLogger("news_insight.main")

_OPEN_PATHS = ("/health", "/docs", "/redoc", "/openapi.json")


class AnalyzeJobRequest(BaseModel):
    type: Literal["analyze"] = "analyze"
    limit: int = Field(default=100, ge=1, le=10000)
    force: bool = False
    concurrency: int | None = Field(default=None, ge=1, le=200)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.settings
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    db.init_db(settings.db_path)
    LOG.info("db ready at %s", settings.db_path)
    manager = JobManager(settings)
    manager.start()
    app.state.jobs = manager
    try:
        result = prune(settings)
        if not result.get("skipped"):
            LOG.info("启动清理: %s", result.get("deleted"))
    except Exception:
        LOG.exception("启动清理失败（不影响服务启动）")
    yield
    manager.shutdown()


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(
        title="news-insight",
        version=__version__,
        description="闭集标签打标 + 关键线索抽取，数据源为 news-crawler /v1",
        lifespan=lifespan,
    )
    app.state.settings = settings or load_settings()

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        """入站鉴权。NEWS_API_KEY 只管出站，与本服务的入站认证无关。"""
        s: Settings = app.state.settings
        path = request.url.path
        if path in _OPEN_PATHS or path.startswith("/docs") or path.startswith("/redoc"):
            return await call_next(request)
        if not (s.api_key or s.require_auth):
            return await call_next(request)  # 未配置 key 且未强制：保持开放（有 warning）
        if not s.api_key:
            return JSONResponse(
                status_code=401,
                content={"error": {"code": 401, "message": "服务要求鉴权但未配置 INSIGHT_API_KEY"}},
            )
        provided = request.headers.get("x-api-key") or ""
        auth = request.headers.get("authorization") or ""
        if not provided and auth.lower().startswith("bearer "):
            provided = auth[7:].strip()
        if provided != s.api_key:
            return JSONResponse(
                status_code=401,
                content={"error": {"code": 401, "message": "API key 无效"}},
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)

    @app.get("/health")
    def health() -> dict:
        s: Settings = app.state.settings
        conn = connect(s)
        try:
            conn.execute("SELECT 1 FROM analyzed_articles LIMIT 1").fetchone()
        finally:
            conn.close()
        # 不回显 news_base_url：健康检查无需鉴权，内网地址不该从这里漏出去
        return {
            "status": "ok",
            "version": __version__,
            "llm_enabled": s.llm_enabled,
            "news_source_mode": s.news_source_mode,
            "auth_enabled": bool(s.api_key or s.require_auth),
        }

    @app.get("/v1/tags")
    def tags() -> dict:
        s: Settings = app.state.settings
        payload, defs = load_vocab(s.vocab_dir)
        conn = connect(s)
        try:
            stats = {r["tag"]: r for r in db.tag_stats(conn)}
        finally:
            conn.close()
        data = [
            {
                "id": t.id,
                "name": t.name,
                "group": t.group,
                "keywords": list(t.keywords),
                "article_count": stats.get(t.id, {}).get("count", 0),
            }
            for t in defs
        ]
        return {"data": data, "meta": {"total": len(data), "vocab_version": payload.get("version")}}

    @app.get("/v1/tags/stats")
    def tags_stats() -> dict:
        s: Settings = app.state.settings
        conn = connect(s)
        try:
            return {"data": db.tag_stats(conn)}
        finally:
            conn.close()

    @app.post("/v1/analyze")
    def analyze(
        limit: int = Query(default=10, ge=1, le=50),
        force: bool = Query(default=False),
    ) -> dict:
        s: Settings = app.state.settings
        if not ANALYZE_LOCK.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="已有分析任务在运行，请稍后再试")
        try:
            result = analyze_batch(s, limit=limit, force=force)
        except Exception as exc:
            # 不要把上游/LLM 响应体回传给调用方（可能含内部地址与提示词）
            LOG.exception("分析失败")
            raise HTTPException(status_code=502, detail=f"分析失败，内部错误: {type(exc).__name__}") from exc
        finally:
            ANALYZE_LOCK.release()
        # 清理是维护动作：失败只记日志，不能把已成功的分析结果改写成 502。
        try:
            prune(s)
        except Exception:
            LOG.exception("清理过期数据失败（不影响本次分析结果）")
        return {"data": result}

    @app.get("/v1/articles")
    def articles(
        tag: str | None = Query(default=None),
        limit: int = Query(default=20, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ) -> dict:
        s: Settings = app.state.settings
        conn = connect(s)
        try:
            rows, total = db.list_analyzed(conn, tag=tag, limit=limit, offset=offset)
        finally:
            conn.close()
        return {"data": rows, "meta": {"total": total, "limit": limit, "offset": offset}}

    @app.get("/v1/articles/{article_id}")
    def article_detail(article_id: str) -> dict:
        s: Settings = app.state.settings
        conn = connect(s)
        try:
            row = db.get_article(conn, article_id)
        finally:
            conn.close()
        if row is None:
            raise HTTPException(status_code=404, detail=f"未找到已分析文章: {article_id}")
        return {"data": row}

    @app.get("/v1/articles/{article_id}/checklist")
    def article_checklist(article_id: str):
        """申报准备清单：由已抽取事实派生，按执行顺序排列。"""
        s: Settings = app.state.settings
        conn = connect(s)
        try:
            row = db.get_article(conn, article_id)
            checklist = db.get_checklist(conn, article_id)
        finally:
            conn.close()
        if row is None:
            return JSONResponse(
                status_code=404,
                content={
                    "error": {
                        "code": "article_not_found",
                        "status": 404,
                        "message": f"未找到已分析文章: {article_id}",
                    }
                },
            )
        if checklist is None:
            return JSONResponse(
                status_code=404,
                content={
                    "error": {
                        "code": "checklist_not_found",
                        "status": 404,
                        "message": "该文章没有申报准备清单（未命中申报标签或生成失败）",
                    }
                },
            )
        return {
            "data": {
                "article_id": article_id,
                "title": row["title"],
                "url": row["url"],
                "engine": checklist["engine"],
                "generated_at": checklist["generated_at"],
                "items": checklist["items"],
            }
        }

    @app.get("/v1/schemas")
    def schemas() -> dict:
        s: Settings = app.state.settings
        data = [
            {
                "schema_id": sc.schema_id,
                "name": sc.name,
                "trigger_tags": list(sc.trigger_tags),
                "fields": [f["name"] for f in sc.fields],
            }
            for sc in load_schemas(s.vocab_dir / "schemas")
        ]
        return {"data": data}

    @app.get("/v1/extractions")
    def extractions(
        schema: str = Query(default="project_application"),
        field: str | None = Query(default=None),
        due_before: str | None = Query(
            default=None, description="仅返回截止日期不晚于该值的记录 (YYYY-MM-DD)"
        ),
        limit: int = Query(default=50, ge=1, le=200),
    ) -> dict:
        s: Settings = app.state.settings
        conn = connect(s)
        try:
            rows = db.list_extractions(conn, schema_id=schema, field=field, due_before=due_before, limit=limit)
        finally:
            conn.close()
        return {"data": rows, "meta": {"returned": len(rows), "limit": limit, "schema": schema}}

    @app.get("/v1/runs")
    def runs(limit: int = Query(default=20, ge=1, le=100)) -> dict:
        s: Settings = app.state.settings
        conn = connect(s)
        try:
            return {"data": db.list_runs(conn, limit)}
        finally:
            conn.close()

    @app.post("/v1/jobs")
    def create_job(body: AnalyzeJobRequest):
        """Submit an async analysis batch. Same payload while queued/running reuses the job."""
        s: Settings = app.state.settings
        jobs: JobManager = app.state.jobs
        try:
            job = jobs.submit(limit=body.limit, force=body.force, concurrency=body.concurrency)
        except JobQueueFull as exc:
            return JSONResponse(
                status_code=429,
                content={
                    "error": {
                        "code": "too_many_jobs",
                        "status": 429,
                        "message": str(exc),
                    }
                },
            )
        return JSONResponse(status_code=202, content={"data": job})

    @app.get("/v1/jobs")
    def list_jobs(limit: int = Query(default=20, ge=1, le=100)) -> dict:
        s: Settings = app.state.settings
        conn = connect(s)
        try:
            return {"data": db.list_analysis_jobs(conn, limit)}
        finally:
            conn.close()

    @app.get("/v1/jobs/{job_id}")
    def get_job(job_id: str) -> dict:
        s: Settings = app.state.settings
        conn = connect(s)
        try:
            job = db.get_analysis_job(conn, job_id)
        finally:
            conn.close()
        if job is None:
            raise HTTPException(status_code=404, detail=f"未找到分析任务: {job_id}")
        return {"data": job}

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={
                "error": {"code": 422, "status": 422, "message": "参数校验失败", "details": exc.errors()},
            },
        )

    @app.exception_handler(HTTPException)
    async def http_exc_handler(request, exc: HTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.status_code, "message": exc.detail}},
        )

    return app


app = create_app()
