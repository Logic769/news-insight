"""HTTP surface for news-insight.

Kept deliberately small: trigger an analysis batch, read results back, and
query extracted facts (the "project application deadline" example).
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app import __version__, accounts, auth, checklist_service, db
from app.extraction import load_schemas
from app.jobs import ANALYZE_LOCK, JobManager, JobQueueFull
from app.pipeline import analyze_batch, connect
from app.prune import prune
from app.settings import Settings, load_settings
from app.tagging import load_categories, load_industries, load_vocab

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
LOG = logging.getLogger("news_insight.main")

_OPEN_PATHS = ("/health", "/docs", "/redoc", "/openapi.json")
LOGIN_MAX_ATTEMPTS = 5
LOGIN_LOCK_MINUTES = 15


def _split_multi(
    values: list[str] | None, *, empty_returns_none: bool = True
) -> list[str] | None:
    """把多值查询参数（?source=、?category=）归一成「去重、去空、支持逗号分隔」的列表。

    三类输入必须区分开，否则前端换个写法就静默查错：

    - ?source=a,b 原先整串被当成一个叫 "a,b" 的站点名，IN (...) 永不命中，
      静默返回 0 条。前端把多选值 join 起来传是很自然的写法，必须拆开。
    - ?source=（空串）是「清空多选」残留的空参数，一律当没传：否则用户清空筛选后
      看到的是「无结果」而不是全部数据。
    - 空片段（"a,,b"）同样忽略，与前者同一套规则：空值 = 不参与筛选。

    全部片段都为空时默认返回 None（不筛）。这与库层 sources=[]（筛完什么都不剩，
    返回空集）是两种语义：[] 只能由代码显式传入，HTTP 层永远不会产生它。

    /v1/articles 的 category/industry 需要保留「空串=筛一个不存在的值」的既有
    语义（见 test_empty_category_filter_returns_empty_not_everything），因此调用方
    传 empty_returns_none=False 时返回 []。

    source 与 category 共用本函数：两者都是前端的多选控件，用户看不出区别，
    只支持其中一种写法会让「站点多选生效、大类多选静默失效」。
    """
    if values is None:
        return None
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        for piece in raw.split(","):
            name = piece.strip()
            if name and name not in seen:
                seen.add(name)
                out.append(name)
    return out or (None if empty_returns_none else [])


class AnalyzeJobRequest(BaseModel):
    type: Literal["analyze"] = "analyze"
    limit: int = Field(default=100, ge=1, le=10000)
    force: bool = False
    concurrency: int | None = Field(default=None, ge=1, le=200)


def _checklist_data(row: dict, checklist: dict) -> dict:
    """The single wire shape shared by GET and POST /checklist."""
    return {
        "data": {
            "article_id": row["article_id"],
            "title": row["title"],
            "url": row["url"],
            "engine": checklist["engine"],
            "generated_at": checklist["generated_at"],
            "items": checklist["items"],
        }
    }


class ChecklistGenerateRequest(BaseModel):
    force: bool = False


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=200)


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=200)
    new_password: str = Field(min_length=1, max_length=200)


class ProfileUpdateRequest(BaseModel):
    display_name: str | None = Field(default=None, max_length=64)
    department: str | None = Field(default=None, max_length=64)


class FavoriteRequest(BaseModel):
    article_id: str = Field(min_length=1, max_length=128)


class HistoryRequest(BaseModel):
    article_id: str = Field(min_length=1, max_length=128)


class PreferencesRequest(BaseModel):
    industries: list[str] = Field(default_factory=list, max_length=20)
    push_enabled: bool = True
    digest_time: str = Field(default="09:00", pattern=r"^\d{2}:\d{2}$")


class AdminUserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str | None = Field(default=None, max_length=200)
    display_name: str = Field(default="", max_length=64)
    department: str = Field(default="", max_length=64)
    role: Literal["admin", "user"] = "user"
    must_change_password: bool = True


class AdminUserUpdate(BaseModel):
    display_name: str | None = Field(default=None, max_length=64)
    department: str | None = Field(default=None, max_length=64)
    role: Literal["admin", "user"] | None = None
    status: Literal["active", "disabled"] | None = None


class AdminSiteCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    list_url: str = Field(min_length=1, max_length=500)
    category: str = Field(default="", max_length=64)
    region: str = Field(default="", max_length=64)
    timezone: str = Field(default="Asia/Shanghai", max_length=64)
    enabled: bool = True


class AdminSiteUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    list_url: str | None = Field(default=None, min_length=1, max_length=500)
    category: str | None = Field(default=None, max_length=64)
    region: str | None = Field(default=None, max_length=64)
    timezone: str | None = Field(default=None, max_length=64)
    enabled: bool | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.settings
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    db.init_db(settings.db_path)
    LOG.info("db ready at %s", settings.db_path)
    try:
        admin_user = auth.bootstrap_admin(settings)
        if admin_user:
            LOG.info("已创建初始管理员: %s", admin_user["username"])
    except Exception:
        LOG.exception("初始化管理员失败（不影响服务启动）")
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
        """入站鉴权：用户 Bearer 会话优先，其次服务级 API key。

        无任何凭据时，只有在既未配置 API key、也未显式要求鉴权时才放行
        （本地单机/测试兼容模式）。生产环境设置 INSIGHT_REQUIRE_AUTH=1 后，
        除 /health、文档与 /v1/auth/login 外都必须先登录。
        """
        s: Settings = app.state.settings
        path = request.url.path
        request.state.user = None
        request.state.token = ""
        request.state.auth_kind = "anonymous"
        if path in _OPEN_PATHS or path.startswith("/docs") or path.startswith("/redoc"):
            return await call_next(request)
        if path == "/v1/auth/login":
            return await call_next(request)
        token = auth.token_from_headers(request.headers)
        if token and not (s.api_key and token == s.api_key):
            user = auth.resolve_session(s, token)
            if user is not None:
                request.state.user = user
                request.state.token = token
                request.state.auth_kind = "user"
                return await call_next(request)
        provided = request.headers.get("x-api-key") or ""
        if not provided and token:
            provided = token
        if s.api_key and provided == s.api_key:
            request.state.auth_kind = "api_key"
            return await call_next(request)
        if not (s.api_key or s.require_auth):
            return await call_next(request)
        return JSONResponse(
            status_code=401,
            content={"error": {"code": 401, "message": "未登录或登录已过期"}},
            headers={"WWW-Authenticate": "Bearer"},
        )

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


    def _current_user(request: Request) -> dict:
        user = getattr(request.state, "user", None)
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录")
        return user

    def _notification_reader(request: Request, *, write: bool = False) -> dict | None:
        """通知的用户上下文。

        已登录用户按 user_id 隔离；服务级 API key 视为受信系统调用，可读写全局。
        无鉴权部署下匿名 GET 保持只读兼容（没有副作用），但任何写操作都必须
        401——否则跨站页面可用无 body 的简单 POST 把所有人的通知标成已读。
        """
        user = getattr(request.state, "user", None)
        if user is not None:
            return user
        if getattr(request.state, "auth_kind", "") == "api_key":
            return None
        if write:
            raise HTTPException(status_code=401, detail="请先登录")
        return None

    def _require_admin(request: Request) -> dict | None:
        """管理员接口：允许服务级 API key，或 role=admin 的登录用户。"""
        if getattr(request.state, "auth_kind", "") == "api_key":
            return None
        user = _current_user(request)
        if user.get("role") != "admin":
            raise HTTPException(status_code=403, detail="需要管理员权限")
        return user

    def _require_operator(request: Request) -> None:
        """触发分析/作业等写操作：API key 或管理员；本地无鉴权模式保持开放。"""
        if getattr(request.state, "auth_kind", "") == "api_key":
            return
        user = getattr(request.state, "user", None)
        if user is not None and user.get("role") == "admin":
            return
        s: Settings = app.state.settings
        if not (s.api_key or s.require_auth):
            return
        raise HTTPException(
            status_code=401 if user is None else 403,
            detail="请先登录" if user is None else "需要管理员权限",
        )

    def _client_ip(request: Request) -> str:
        return request.client.host if request.client is not None else ""

    def _audit(
        request: Request,
        conn,
        action: str,
        *,
        target: str = "",
        detail: str = "",
    ) -> None:
        user = getattr(request.state, "user", None) or {}
        kind = getattr(request.state, "auth_kind", "anonymous")
        accounts.log_operation(
            conn,
            user_id=user.get("id"),
            username=user.get("username") or ("service" if kind == "api_key" else "anonymous"),
            action=action,
            target=target,
            detail=detail,
            ip=_client_ip(request),
            commit=False,
        )

    # ------------------------------------------------------------------ auth
    @app.post("/v1/auth/login")
    def login(payload: LoginRequest, request: Request) -> dict:
        s: Settings = app.state.settings
        conn = connect(s)
        try:
            user = accounts.get_user_by_username(conn, payload.username.strip())
            if user is None:
                accounts.log_operation(
                    conn,
                    user_id=None,
                    username=payload.username.strip()[:64],
                    action="auth.login_failed",
                    detail="unknown username",
                    ip=_client_ip(request),
                )
                raise HTTPException(status_code=401, detail="用户名或密码错误")
            if user.get("status") != "active":
                raise HTTPException(status_code=403, detail="账号已被禁用，请联系管理员")
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            locked_until = user.get("locked_until")
            if locked_until and str(locked_until) > now:
                raise HTTPException(
                    status_code=423,
                    detail=f"账号已锁定，请于 {locked_until} 后再试",
                )
            if not auth.verify_password(
                payload.password,
                salt=str(user["password_salt"]),
                expected_hash=str(user["password_hash"]),
                iterations=int(user["password_iterations"]),
            ):
                attempts, locked = accounts.record_login_failure(
                    conn,
                    int(user["id"]),
                    max_attempts=LOGIN_MAX_ATTEMPTS,
                    lock_minutes=LOGIN_LOCK_MINUTES,
                )
                accounts.log_operation(
                    conn,
                    user_id=int(user["id"]),
                    username=str(user["username"]),
                    action="auth.login_failed",
                    detail=f"attempt={attempts}",
                )
                if locked:
                    raise HTTPException(
                        status_code=423,
                        detail=f"连续 {LOGIN_MAX_ATTEMPTS} 次密码错误，账号已锁定 {LOGIN_LOCK_MINUTES} 分钟",
                    )
                remaining = max(0, LOGIN_MAX_ATTEMPTS - attempts)
                raise HTTPException(
                    status_code=401,
                    detail=f"用户名或密码错误，还可尝试 {remaining} 次",
                )
            accounts.record_login_success(conn, int(user["id"]))
            token, expires_at = auth.issue_session(s, conn, int(user["id"]))
            accounts.log_operation(
                conn,
                user_id=int(user["id"]),
                username=str(user["username"]),
                action="auth.login",
            )
            return {
                "data": {
                    "token": token,
                    "expires_at": expires_at,
                    "user": accounts.public_user(user),
                }
            }
        finally:
            conn.close()

    @app.get("/v1/auth/me")
    def auth_me(request: Request) -> dict:
        return {"data": accounts.public_user(_current_user(request))}

    @app.post("/v1/auth/logout")
    def auth_logout(request: Request) -> dict:
        s: Settings = app.state.settings
        token = getattr(request.state, "token", "")
        if token:
            auth.revoke_token(s, token)
        return {"data": {"logged_out": True}}

    @app.post("/v1/auth/password")
    def auth_change_password(payload: PasswordChangeRequest, request: Request) -> dict:
        s: Settings = app.state.settings
        user = _current_user(request)
        if payload.current_password == payload.new_password:
            raise HTTPException(status_code=400, detail="新密码不能与原密码相同")
        policy_error = auth.password_policy_error(payload.new_password)
        if policy_error:
            raise HTTPException(status_code=400, detail=policy_error)
        conn = connect(s)
        try:
            fresh = accounts.get_user(conn, int(user["id"]), include_secret=True)
            if fresh is None:
                raise HTTPException(status_code=404, detail="用户不存在")
            if not auth.verify_password(
                payload.current_password,
                salt=str(fresh["password_salt"]),
                expected_hash=str(fresh["password_hash"]),
                iterations=int(fresh["password_iterations"]),
            ):
                raise HTTPException(status_code=400, detail="原密码不正确")
            digest, salt, iterations = auth.hash_password(payload.new_password)
            accounts.update_user_password(
                conn,
                int(user["id"]),
                password_hash=digest,
                password_salt=salt,
                password_iterations=iterations,
                must_change_password=False,
            )
            token = getattr(request.state, "token", "")
            if token:
                token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
                accounts.revoke_user_sessions(
                    conn, int(user["id"]), except_token_hash=token_hash
                )
            _audit(request, conn, "auth.password_change", target=str(user["id"]))
            conn.commit()
        finally:
            conn.close()
        return {"data": {"changed": True}}

    # ------------------------------------------------------------ personal
    @app.patch("/v1/me/profile")
    def update_profile(payload: ProfileUpdateRequest, request: Request) -> dict:
        s: Settings = app.state.settings
        user = _current_user(request)
        conn = connect(s)
        try:
            updated = accounts.update_user(
                conn,
                int(user["id"]),
                display_name=payload.display_name,
                department=payload.department,
            )
            if updated is None:
                raise HTTPException(status_code=404, detail="用户不存在")
            _audit(request, conn, "profile.update", target=str(user["id"]))
            conn.commit()
            return {"data": accounts.public_user(updated)}
        finally:
            conn.close()

    @app.get("/v1/me/favorites")
    def my_favorites(
        request: Request,
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ) -> dict:
        s: Settings = app.state.settings
        user = _current_user(request)
        conn = connect(s)
        try:
            items, total = accounts.list_favorites(
                conn, user_id=int(user["id"]), limit=limit, offset=offset
            )
        finally:
            conn.close()
        return {
            "data": items,
            "meta": {"total": total, "returned": len(items), "limit": limit, "offset": offset},
        }

    @app.post("/v1/me/favorites")
    def add_my_favorite(payload: FavoriteRequest, request: Request) -> dict:
        s: Settings = app.state.settings
        user = _current_user(request)
        conn = connect(s)
        try:
            if db.get_article(conn, payload.article_id) is None:
                raise HTTPException(status_code=404, detail="未找到该文章")
            created = accounts.add_favorite(
                conn, user_id=int(user["id"]), article_id=payload.article_id
            )
            _audit(request, conn, "favorite.add", target=payload.article_id)
            conn.commit()
        finally:
            conn.close()
        return {"data": {"article_id": payload.article_id, "favorite": True, "created": created}}

    @app.delete("/v1/me/favorites/{article_id}")
    def delete_my_favorite(article_id: str, request: Request) -> dict:
        s: Settings = app.state.settings
        user = _current_user(request)
        conn = connect(s)
        try:
            removed = accounts.remove_favorite(
                conn, user_id=int(user["id"]), article_id=article_id
            )
            _audit(request, conn, "favorite.remove", target=article_id)
            conn.commit()
        finally:
            conn.close()
        return {"data": {"article_id": article_id, "favorite": False, "removed": removed}}

    @app.get("/v1/me/history")
    def my_history(
        request: Request,
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ) -> dict:
        s: Settings = app.state.settings
        user = _current_user(request)
        conn = connect(s)
        try:
            items, total = accounts.list_history(
                conn, user_id=int(user["id"]), limit=limit, offset=offset
            )
        finally:
            conn.close()
        return {
            "data": items,
            "meta": {"total": total, "returned": len(items), "limit": limit, "offset": offset},
        }

    @app.post("/v1/me/history")
    def record_my_history(payload: HistoryRequest, request: Request) -> dict:
        s: Settings = app.state.settings
        user = _current_user(request)
        conn = connect(s)
        try:
            if db.get_article(conn, payload.article_id) is None:
                raise HTTPException(status_code=404, detail="未找到该文章")
            accounts.record_history(
                conn, user_id=int(user["id"]), article_id=payload.article_id
            )
        finally:
            conn.close()
        return {"data": {"article_id": payload.article_id, "recorded": True}}

    @app.delete("/v1/me/history")
    def clear_my_history(request: Request) -> dict:
        s: Settings = app.state.settings
        user = _current_user(request)
        conn = connect(s)
        try:
            cleared = accounts.clear_history(conn, user_id=int(user["id"]))
        finally:
            conn.close()
        return {"data": {"cleared": cleared}}

    @app.get("/v1/me/preferences")
    def my_preferences(request: Request) -> dict:
        s: Settings = app.state.settings
        user = _current_user(request)
        conn = connect(s)
        try:
            prefs = accounts.get_preferences(conn, int(user["id"]))
        finally:
            conn.close()
        return {"data": prefs}

    @app.put("/v1/me/preferences")
    def save_my_preferences(payload: PreferencesRequest, request: Request) -> dict:
        s: Settings = app.state.settings
        user = _current_user(request)
        if not (0 <= int(payload.digest_time[:2]) <= 23 and 0 <= int(payload.digest_time[3:]) <= 59):
            raise HTTPException(status_code=400, detail="推送时间格式不正确")
        conn = connect(s)
        try:
            prefs = accounts.save_preferences(
                conn,
                int(user["id"]),
                industries=[x.strip() for x in payload.industries if x.strip()],
                push_enabled=payload.push_enabled,
                digest_time=payload.digest_time,
            )
            _audit(request, conn, "preferences.update", target=str(user["id"]))
            conn.commit()
        finally:
            conn.close()
        return {"data": prefs}

    # --------------------------------------------------------------- admin
    @app.get("/v1/admin/users")
    def admin_users(
        request: Request,
        q: str = Query(default="", max_length=64),
        role: str = Query(default="", max_length=16),
        status: str = Query(default="", max_length=16),
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ) -> dict:
        _require_admin(request)
        s: Settings = app.state.settings
        conn = connect(s)
        try:
            items, total = accounts.list_users(
                conn, q=q, role=role, status=status, limit=limit, offset=offset
            )
        finally:
            conn.close()
        return {
            "data": items,
            "meta": {"total": total, "returned": len(items), "limit": limit, "offset": offset},
        }

    @app.post("/v1/admin/users")
    def admin_create_user(payload: AdminUserCreate, request: Request) -> dict:
        _require_admin(request)
        s: Settings = app.state.settings
        username = payload.username.strip()
        temporary_password = None
        password = payload.password
        if not password:
            temporary_password = auth.generate_temp_password()
            password = temporary_password
        else:
            policy_error = auth.password_policy_error(password)
            if policy_error:
                raise HTTPException(status_code=400, detail=policy_error)
        conn = connect(s)
        try:
            user = auth.create_user(
                conn,
                username=username,
                password=password,
                display_name=payload.display_name,
                department=payload.department,
                role=payload.role,
                must_change_password=payload.must_change_password if not temporary_password else True,
            )
            _audit(request, conn, "admin.user_create", target=username, detail=f"role={payload.role}")
            conn.commit()
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="用户名已存在") from None
        finally:
            conn.close()
        data = dict(user)
        if temporary_password:
            data["temporary_password"] = temporary_password
        return {"data": data}

    @app.patch("/v1/admin/users/{user_id}")
    def admin_update_user(user_id: int, payload: AdminUserUpdate, request: Request) -> dict:
        admin = _require_admin(request)
        s: Settings = app.state.settings
        if admin is not None and int(admin["id"]) == user_id:
            if payload.status == "disabled" or payload.role == "user":
                raise HTTPException(status_code=400, detail="不能禁用或降级当前登录的管理员")
        conn = connect(s)
        try:
            updated = accounts.update_user(
                conn,
                user_id,
                display_name=payload.display_name,
                department=payload.department,
                role=payload.role,
                status=payload.status,
            )
            if updated is None:
                raise HTTPException(status_code=404, detail="用户不存在")
            if payload.status == "disabled":
                accounts.revoke_user_sessions(conn, user_id)
            _audit(request, conn, "admin.user_update", target=str(user_id))
            conn.commit()
        finally:
            conn.close()
        return {"data": updated}

    @app.post("/v1/admin/users/{user_id}/reset-password")
    def admin_reset_password(user_id: int, request: Request) -> dict:
        _require_admin(request)
        s: Settings = app.state.settings
        conn = connect(s)
        try:
            user = accounts.get_user(conn, user_id)
            if user is None:
                raise HTTPException(status_code=404, detail="用户不存在")
            temp = auth.generate_temp_password()
            digest, salt, iterations = auth.hash_password(temp)
            accounts.update_user_password(
                conn,
                user_id,
                password_hash=digest,
                password_salt=salt,
                password_iterations=iterations,
                must_change_password=True,
            )
            accounts.revoke_user_sessions(conn, user_id)
            _audit(request, conn, "admin.user_reset_password", target=str(user_id))
            conn.commit()
        finally:
            conn.close()
        return {"data": {"user_id": user_id, "temporary_password": temp}}

    @app.get("/v1/admin/sites")
    def admin_sites(request: Request) -> dict:
        _require_admin(request)
        s: Settings = app.state.settings
        conn = connect(s)
        try:
            items = accounts.list_sites(conn)
        finally:
            conn.close()
        return {"data": items, "meta": {"total": len(items)}}

    @app.post("/v1/admin/sites")
    def admin_create_site(payload: AdminSiteCreate, request: Request) -> dict:
        _require_admin(request)
        s: Settings = app.state.settings
        conn = connect(s)
        try:
            site = accounts.create_site(
                conn,
                name=payload.name.strip(),
                list_url=payload.list_url.strip(),
                category=payload.category,
                region=payload.region,
                timezone=payload.timezone,
                enabled=payload.enabled,
            )
            _audit(request, conn, "admin.site_create", target=payload.name)
            conn.commit()
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="站点名称已存在") from None
        finally:
            conn.close()
        return {"data": site}

    @app.patch("/v1/admin/sites/{site_id}")
    def admin_update_site(site_id: int, payload: AdminSiteUpdate, request: Request) -> dict:
        _require_admin(request)
        s: Settings = app.state.settings
        conn = connect(s)
        try:
            site = accounts.update_site(
                conn,
                site_id,
                name=payload.name,
                list_url=payload.list_url,
                category=payload.category,
                region=payload.region,
                timezone=payload.timezone,
                enabled=payload.enabled,
            )
            if site is None:
                raise HTTPException(status_code=404, detail="站点不存在")
            _audit(request, conn, "admin.site_update", target=str(site_id))
            conn.commit()
        finally:
            conn.close()
        return {"data": site}

    @app.delete("/v1/admin/sites/{site_id}")
    def admin_delete_site(site_id: int, request: Request) -> dict:
        _require_admin(request)
        s: Settings = app.state.settings
        conn = connect(s)
        try:
            removed = accounts.delete_site(conn, site_id)
            if not removed:
                raise HTTPException(status_code=404, detail="站点不存在或已停用")
            _audit(request, conn, "admin.site_delete", target=str(site_id))
            conn.commit()
        finally:
            conn.close()
        return {"data": {"site_id": site_id, "enabled": False}}

    @app.get("/v1/admin/stats")
    def admin_stats(
        request: Request, days: int = Query(default=7, ge=1, le=90)
    ) -> dict:
        _require_admin(request)
        s: Settings = app.state.settings
        conn = connect(s)
        try:
            summary = accounts.admin_summary(conn, days=days)
        finally:
            conn.close()
        return {"data": summary}

    @app.get("/v1/admin/operation-logs")
    def admin_operation_logs(
        request: Request,
        action: str = Query(default="", max_length=64),
        username: str = Query(default="", max_length=64),
        date_from: str = Query(default="", max_length=10),
        date_to: str = Query(default="", max_length=10),
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ) -> dict:
        _require_admin(request)
        s: Settings = app.state.settings
        conn = connect(s)
        try:
            items, total = accounts.list_operation_logs(
                conn,
                action=action,
                username=username,
                date_from=date_from,
                date_to=date_to,
                limit=limit,
                offset=offset,
            )
        finally:
            conn.close()
        return {
            "data": items,
            "meta": {"total": total, "returned": len(items), "limit": limit, "offset": offset},
        }

    @app.get("/v1/categories")
    def categories() -> dict:
        s: Settings = app.state.settings
        payload, cats = load_categories(s.vocab_dir)
        conn = connect(s)
        try:
            stats = {r["category"]: r for r in db.category_stats(conn)}
        finally:
            conn.close()
        data = [
            {
                "id": c.id,
                "name": c.name,
                "description": c.description,
                "keywords": list(c.keywords),
                "tags": list(c.tags),
                "article_count": stats.get(c.id, {}).get("count", 0),
            }
            for c in cats
        ]
        return {"data": data, "meta": {"total": len(data), "vocab_version": payload.get("version")}}

    @app.get("/v1/categories/stats")
    def categories_stats() -> dict:
        s: Settings = app.state.settings
        conn = connect(s)
        try:
            return {"data": db.category_stats(conn)}
        finally:
            conn.close()

    @app.get("/v1/industries")
    def industries() -> dict:
        s: Settings = app.state.settings
        payload, items = load_industries(s.vocab_dir)
        conn = connect(s)
        try:
            stats = {row["industry"]: row for row in db.industry_stats(conn)}
        finally:
            conn.close()
        data = [
            {
                "id": item.id,
                "name": item.name,
                "description": item.description,
                "keywords": list(item.keywords),
                "article_count": stats.get(item.id, {}).get("count", 0),
            }
            for item in items
        ]
        return {"data": data, "meta": {"total": len(data), "vocab_version": payload.get("version")}}

    # 路径与上游 news-crawler 的 GET /v1/sources（来源统计）保持一致，
    # 前端两个服务共用一个 api 层时不必记两套命名。
    # 不叫 /v1/sources/stats：categories/tags 之所以有「定义 + 统计」两级，
    # 是因为它们的候选集合写在词汇表文件里；来源没有静态定义，统计就是全部信息。
    @app.get("/v1/sources")
    def sources_stats() -> dict:
        """按抓取站点聚合：条数、平均分类置信度、最新发布时间。

        前端「发布单位」筛选下拉直接吃这个列表，管理端统计看板也用它，
        所以不额外做分页——站点数量是几十的量级。
        """
        s: Settings = app.state.settings
        conn = connect(s)
        try:
            rows = db.source_stats(conn)
        finally:
            conn.close()
        return {"data": rows}

    @app.get("/v1/tags")
    def tags(category: str | None = Query(default=None)) -> dict:
        s: Settings = app.state.settings
        payload, defs = load_vocab(s.vocab_dir)
        conn = connect(s)
        try:
            stats = {r["tag"]: r for r in db.tag_stats(conn, category=category)}
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
        request: Request,
        limit: int = Query(default=10, ge=1, le=50),
        force: bool = Query(default=False),
    ) -> dict:
        _require_operator(request)
        s: Settings = app.state.settings
        if not ANALYZE_LOCK.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="已有分析或清单生成任务在运行，请稍后再试")
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
        category: Annotated[
            list[str] | None, Query(description="只在这些大类里筛选，可重复或逗号分隔")
        ] = None,
        industry: Annotated[
            list[str] | None, Query(description="只在这些行业里筛选，可重复或逗号分隔")
        ] = None,
        source: Annotated[list[str] | None, Query(description="只在这些站点里筛选")] = None,
        published_from: str | None = Query(default=None, description="发布日期下限 YYYY-MM-DD"),
        published_to: str | None = Query(default=None, description="发布日期上限 YYYY-MM-DD（含当天）"),
        sort: str = Query(default="published_desc", description="排序键"),
        limit: int = Query(default=20, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ) -> dict:
        s: Settings = app.state.settings
        try:
            since = db.normalize_day(published_from)
            until = db.normalize_day_end(published_to)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if sort not in db.LIST_SORTS:
            raise HTTPException(status_code=422, detail=f"未知排序: {sort}")
        conn = connect(s)
        try:
            rows, total, undated = db.list_analyzed(
                conn,
                tag=tag,
                # 列表端点保留「空串=筛一个不存在的类」的既有语义；非空值统一
                # 支持重复参数与逗号分隔，和 /v1/search 的多选控件行为对齐。
                category=_split_multi(category, empty_returns_none=False),
                industry=_split_multi(industry, empty_returns_none=False),
                sources=_split_multi(source),
                published_from=since,
                published_to=until,
                sort=sort,
                limit=limit,
                offset=offset,
            )
        finally:
            conn.close()
        return {
            "data": rows,
            "meta": {
                "total": total,
                "limit": limit,
                "offset": offset,
                "sort": sort,
                # 两个时间列都为空的行无法判断是否落在区间内，会被排除；条数单独
                # 报出来，前端才能解释「为什么 total 比预期少」而不是当成丢数据。
                "undated_excluded": undated,
            },
        }

    @app.get("/v1/search")
    def search_articles(
        # pattern 要求至少一个非空白字符：q="" 会因 min_length 变成 422，但 q=" "
        # 能绕过它，进 resolve_query 后被 strip 成空、返回 200 空 groups —— 同一件事
        # 两种响应码，前端得写两条分支。这里让纯空白也在参数校验阶段被拒。
        q: str = Query(min_length=1, max_length=100, pattern=r"\S", description="用户关键词"),
        # 这里用 Annotated 而不是 Query(...) 作默认值：注解是可变容器（list），
        # ruff 的 B008 会判定"默认值里调用函数 + 可变类型"有问题；同文件其余
        # 参数都是 str/int/bool，不受该规则影响。
        category: Annotated[
            list[str] | None, Query(description="只在这些大类里检索")
        ] = None,
        industry: Annotated[
            list[str] | None, Query(description="只在这些行业里检索")
        ] = None,
        source: Annotated[list[str] | None, Query(description="只在这些站点里检索")] = None,
        published_from: str | None = Query(default=None, description="发布日期下限 YYYY-MM-DD"),
        published_to: str | None = Query(default=None, description="发布日期上限 YYYY-MM-DD（含当天）"),
        sort: str = Query(default="relevance", description="类内排序：relevance/published_desc/published_asc"),
        per_category: int = Query(default=20, ge=1, le=100, description="每个大类返回多少条"),
        include_body: bool = Query(default=False, description="是否返回正文（默认不返回，节省带宽）"),
    ) -> dict:
        """按关键词检索，结果按 6 大类分组、类内按相关度排序。

        排序相关的计算全在这里完成，前端只负责渲染。前端虽然能从 item 里读到标签
        置信度，但拿不到查询覆盖度（那要复算词表匹配），默认也拿不到正文，无法复
        算出同一个分数，所以 `groups[].items[]` 的顺序就是最终顺序。
        """
        s: Settings = app.state.settings
        if sort not in db.SEARCH_SORTS:
            raise HTTPException(status_code=422, detail=f"未知排序: {sort}")
        try:
            since = db.normalize_day(published_from)
            until = db.normalize_day_end(published_to)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        sources = _split_multi(source)
        _, tags = load_vocab(s.vocab_dir)
        _, categories = load_categories(s.vocab_dir)
        conn = connect(s)
        try:
            result = db.search_grouped(
                conn,
                raw_query=q,
                tags=tags,
                category_order=tuple(c.id for c in categories),
                # 与 source 共用 _split_multi：?category= 传空串时 FastAPI 给出 [""]，
                # 直接透传会让 SQL 变成 COALESCE(category,'') IN ('')，只命中「分类上线
                # 前的历史 NULL 行」，静默返回一堆与用户意图无关的结果。空串一律当「没传」
                # ——注意过滤后可能变成空列表，而 search_candidates 把空列表当作「筛选后
                # 无结果」，所以 _split_multi 收尾返回 None（不传），否则 ?category= 反而
                # 返回空集。逗号写法（?category=a,b）同样拆开，理由见 _split_multi。
                categories=_split_multi(category),
                industries=_split_multi(industry),
                sources=sources,
                published_from=since,
                published_to=until,
                per_category=per_category,
                include_body=include_body,
                sort=sort,
            )
        finally:
            conn.close()
        return {"data": result}

    @app.get("/v1/articles/{article_id}")
    def article_detail(article_id: str, request: Request) -> dict:
        s: Settings = app.state.settings
        conn = connect(s)
        try:
            row = db.get_article(conn, article_id)
            if row is not None:
                user = getattr(request.state, "user", None)
                if user is not None:
                    accounts.record_history(
                        conn, user_id=int(user["id"]), article_id=article_id
                    )
                    row["is_favorite"] = accounts.is_favorite(
                        conn, user_id=int(user["id"]), article_id=article_id
                    )
                else:
                    row["is_favorite"] = False
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
        return _checklist_data(row, checklist)

    @app.post("/v1/articles/{article_id}/checklist")
    def generate_article_checklist(article_id: str, payload: ChecklistGenerateRequest | None = None):
        """按需生成或重新生成申报准备清单。

        默认复用已有清单（幂等）；只有 force=true 才重新调用生成器。生成失败或
        文章不适用时返回可解释错误，不把「没有清单」伪装成 404 空结果。
        """
        s: Settings = app.state.settings
        force = bool(payload and payload.force)
        conn = connect(s)
        try:
            row = db.get_article(conn, article_id)
            existing = None if force else db.get_checklist(conn, article_id)
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
        if existing is not None:
            return _checklist_data(row, existing)
        source = checklist_service.checklist_input_from_detail(row)
        if source is None:
            return JSONResponse(
                status_code=409,
                content={
                    "error": {
                        "code": "checklist_not_applicable",
                        "status": 409,
                        "message": "该文章不是项目申报类，或缺少可用的申报要素/正文，无法生成清单",
                    }
                },
            )
        # 清单生成会调用 LLM，和批量分析共用同一把非阻塞锁：既防重复烧 token，
        # 也避免多个并发 force 请求把上游打满。拿不到锁时明确返回 409。
        if not ANALYZE_LOCK.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="已有分析或清单生成任务在运行，请稍后再试")
        try:
            # 等锁期间可能已有别的请求生成完成；非 force 必须在持锁后复查，避免重复调用 LLM。
            if not force:
                conn = connect(s)
                try:
                    existing = db.get_checklist(conn, article_id)
                finally:
                    conn.close()
                if existing is not None:
                    return _checklist_data(row, existing)
            result = checklist_service.build_for_input(s, source)
            items = checklist_service.items_payload(result)
            conn = connect(s)
            try:
                db.save_checklist(conn, article_id=article_id, engine=result.engine, items=items)
                checklist = db.get_checklist(conn, article_id)
            finally:
                conn.close()
        finally:
            ANALYZE_LOCK.release()
        if checklist is None:  # pragma: no cover - save_checklist 成功时不会发生
            raise HTTPException(status_code=500, detail="清单已生成但读取失败")
        return _checklist_data(row, checklist)

    @app.get("/v1/notifications")
    def notifications(
        request: Request,
        limit: int = Query(default=20, ge=1, le=100),
        unread_only: bool = Query(default=False),
    ) -> dict:
        """站内消息提醒：新分析出的项目申报类公告。

        unread 始终是「全库未读数」，不受 unread_only 影响——铃铛角标要的是它，
        下拉列表才需要只看未读。列表带文章跳转所需的 article_id/url。
        """
        s: Settings = app.state.settings
        conn = connect(s)
        try:
            user = _notification_reader(request)
            items, total, unread = db.list_notifications(
                conn,
                user_id=int(user["id"]) if user is not None else None,
                limit=limit,
                unread_only=unread_only,
            )
        finally:
            conn.close()
        return {
            "data": items,
            "meta": {"total": total, "unread": unread, "returned": len(items), "limit": limit},
        }

    @app.post("/v1/notifications/read-all")
    def notifications_read_all(request: Request) -> dict:
        s: Settings = app.state.settings
        conn = connect(s)
        try:
            user = _notification_reader(request, write=True)
            updated = db.mark_all_notifications_read(
                conn, user_id=int(user["id"]) if user is not None else None
            )
        finally:
            conn.close()
        return {"data": {"updated": updated, "unread": 0}}

    @app.post("/v1/notifications/{notification_id}/read")
    def notification_read(notification_id: int, request: Request) -> dict:
        s: Settings = app.state.settings
        conn = connect(s)
        try:
            user = _notification_reader(request, write=True)
            item = db.mark_notification_read(
                conn,
                notification_id,
                user_id=int(user["id"]) if user is not None else None,
            )
        finally:
            conn.close()
        if item is None:
            raise HTTPException(status_code=404, detail=f"未找到通知: {notification_id}")
        return {"data": item}

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
    def create_job(body: AnalyzeJobRequest, request: Request):
        """Submit an async analysis batch. Same payload while queued/running reuses the job."""
        _require_operator(request)
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
