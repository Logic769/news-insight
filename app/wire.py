"""对外响应统一包装。

前端规范要求所有接口返回 ``{code, msg, data}``。本模块在 HTTP 层统一改写
``/v1/*`` 的 JSON 响应，路由函数本身继续返回内部的 ``{"data": ..., "meta": ...}``
结构，避免 90 多个返回点逐个改动。

约定：

- 成功：``{"code": 0, "msg": "ok", "data": <原 data>, "meta": <原 meta，若有>}``
- 失败：``{"code": <非 0 整数>, "msg": <可读消息>, "data": null}``，HTTP 状态码保留；
  原错误体里的 ``details``（参数校验明细）原样保留。
- ``/health``、``/docs``、``/redoc``、``/openapi.json`` 不包装。
"""

from __future__ import annotations

import copy
import json
import logging
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

LOG = logging.getLogger(__name__)

# 按错误名映射，保留「同一 HTTP 状态、不同业务含义」的区分
_CODE_BY_NAME = {
    "article_not_found": 40401,
    "checklist_not_found": 40402,
    "checklist_not_applicable": 40902,
    "too_many_jobs": 42901,
    "validation_error": 42201,
    "upstream_error": 50201,
}

# 按 HTTP 状态兜底。5 位业务码：前 3 位是 HTTP 状态，后 2 位是同类序号。
_CODE_BY_STATUS = {
    400: 40001,
    401: 40101,
    403: 40301,
    404: 40400,
    405: 40501,
    408: 40801,
    409: 40901,
    422: 42201,
    423: 42301,
    429: 42901,
    500: 50001,
    502: 50201,
    503: 50301,
    504: 50401,
}


def error_code(status: int, name: Any = None) -> int:
    """把 HTTP 状态与业务错误名归一成稳定的整数业务码。"""
    if isinstance(name, str) and name in _CODE_BY_NAME:
        return _CODE_BY_NAME[name]
    return _CODE_BY_STATUS.get(status, 50001 if status >= 500 else 40001)


def success_body(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict) and "data" in payload:
        body: dict[str, Any] = {"code": 0, "msg": "ok", "data": payload["data"]}
        if "meta" in payload:
            body["meta"] = payload["meta"]
        return body
    return {"code": 0, "msg": "ok", "data": payload}


def error_body(status: int, payload: Any) -> dict[str, Any]:
    message = "请求失败"
    details: Any = None
    name: Any = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            name = error.get("code")
            message = str(error.get("message") or message)
            details = error.get("details")
        elif payload.get("msg"):
            message = str(payload["msg"])
        elif payload.get("detail"):
            message = str(payload["detail"])
    body: dict[str, Any] = {"code": error_code(status, name), "msg": message, "data": None}
    if details is not None:
        body["details"] = details
    return body


def _rebuild(response: Response, content: dict[str, Any]) -> JSONResponse:
    headers = {
        key: value
        for key, value in response.headers.items()
        if key.lower() not in {"content-length", "content-type"}
    }
    return JSONResponse(status_code=response.status_code, content=content, headers=headers)


class UnifiedResponseMiddleware(BaseHTTPMiddleware):
    """把 ``/v1/*`` 的 JSON 响应改写为统一信封。"""

    async def dispatch(self, request: Request, call_next):
        try:
            response = await call_next(request)
        except Exception:  # noqa: BLE001 - 未处理异常也必须落在 /v1 统一信封里
            LOG.exception("未处理异常: %s %s", request.method, request.url.path)
            return JSONResponse(
                status_code=500,
                content=error_body(500, {"msg": "服务器内部错误"}),
            )
        path = request.url.path
        if not path.startswith("/v1/") or response.status_code in {204, 304}:
            return response

        chunks = [chunk async for chunk in response.body_iterator]
        raw = b"".join(chunks)
        content_type = response.headers.get("content-type", "").lower()

        if "application/json" not in content_type:
            if response.status_code >= 400:
                message = raw.decode("utf-8", "replace").strip() or "请求失败"
                return _rebuild(
                    response,
                    error_body(response.status_code, {"msg": message}),
                )
            return _rebuild_stream(response, raw, content_type)

        try:
            payload = json.loads(raw) if raw else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            LOG.warning("无法解析 %s %s 的响应体，按成功空数据包装", request.method, path)
            return _rebuild(response, success_body(None))

        if isinstance(payload, dict) and "code" in payload and "msg" in payload:
            # 已经是统一信封（例如未来的新路由直接返回信封），保留原业务码，
            # 不能因为 HTTP 4xx 被状态码兜底覆盖成通用码。
            return _rebuild(response, payload)
        if response.status_code >= 400:
            # 未匹配路由/方法由 Starlette 直接返回 {"detail": ...}，它没有 error 键，
            # 不能按成功包装，否则 HTTP 404/405 会带 code=0。
            return _rebuild(response, error_body(response.status_code, payload))
        if isinstance(payload, dict) and "error" in payload:
            return _rebuild(response, error_body(response.status_code, payload))
        return _rebuild(response, success_body(payload))


def _rebuild_stream(response: Response, raw: bytes, content_type: str) -> Response:
    """非 JSON 的成功响应原样透传（目前 /v1 下没有，保留以防未来加下载接口）。"""
    headers = {
        key: value
        for key, value in response.headers.items()
        if key.lower() != "content-length"
    }
    if content_type:
        headers["content-type"] = content_type
    return Response(
        content=raw,
        status_code=response.status_code,
        headers=headers,
        media_type=content_type or None,
    )


def install(app) -> None:
    """注册统一响应中间件，并让 OpenAPI 文档显示包装后的结构。"""
    app.add_middleware(UnifiedResponseMiddleware)
    # 先清掉可能存在的原始缓存，下面只缓存「包装后」的 schema；否则直接读
    # app.openapi_schema 的工具会拿到未包装的旧契约。
    app.openapi_schema = None
    original_openapi = app.openapi

    def openapi() -> dict[str, Any]:
        cached = getattr(app, "_wire_openapi_schema", None)
        if cached is not None:
            return cached
        schema = copy.deepcopy(original_openapi())
        components = schema.setdefault("components", {}).setdefault("schemas", {})
        components["ApiError"] = {
            "type": "object",
            "required": ["code", "msg", "data"],
            "properties": {
                "code": {"type": "integer", "example": 40101},
                "msg": {"type": "string", "example": "未登录或登录已过期"},
                "data": {"description": "错误响应恒为 null"},
                "details": {
                    "description": "参数校验明细等可选上下文",
                },
            },
        }
        for path, methods in schema.get("paths", {}).items():
            if not path.startswith("/v1/"):
                continue
            for method, operation in methods.items():
                if method.lower() not in {"get", "post", "put", "patch", "delete"}:
                    continue
                responses = operation.setdefault("responses", {})
                for status, response in responses.items():
                    if not status.startswith("2"):
                        continue
                    inner = (
                        response.get("content", {})
                        .get("application/json", {})
                        .get("schema")
                    )
                    # 路由返回裸 dict 时 FastAPI 生成
                    # {"type":"object","additionalProperties":true,...}，它描述的是
                    # 包装前的整个响应体，不是 data 的值。无法据此推断 data 结构时
                    # 不约束 data，避免把数组结果文档化成 object。
                    if (
                        isinstance(inner, dict)
                        and inner.get("type") == "object"
                        and inner.get("additionalProperties") is True
                        and "properties" not in inner
                    ):
                        inner = None
                    response["content"] = {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["code", "msg", "data"],
                                "properties": {
                                    "code": {"type": "integer", "example": 0},
                                    "msg": {"type": "string", "example": "ok"},
                                    "data": inner or {},
                                    "meta": {"type": "object"},
                                },
                            }
                        }
                    }
                for status in ("400", "401", "403", "404", "409", "422", "429", "500", "502", "503"):
                    # FastAPI 会为带参数的路由预置 422 + HTTPValidationError，
                    # 必须覆盖而不是 setdefault，否则文档仍描述旧的 detail 结构。
                    # 但已有自定义 schema 的路由保留其描述与 headers。
                    existing = responses.get(status)
                    existing_schema = (
                        existing.get("content", {})
                        .get("application/json", {})
                        .get("schema")
                        if isinstance(existing, dict)
                        else None
                    )
                    existing_description = (
                        existing.get("description") if isinstance(existing, dict) else None
                    )
                    custom_description = existing_description not in (
                        None,
                        "",
                        "Successful Response",
                    )
                    custom_schema = (
                        isinstance(existing_schema, dict)
                        and existing_schema.get("$ref")
                        not in (None, "#/components/schemas/HTTPValidationError")
                    )
                    if status != "422" and (custom_description or custom_schema):
                        continue
                    responses[status] = {
                        "description": "统一错误响应",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/ApiError"}
                            }
                        },
                    }
        app._wire_openapi_schema = schema
        app.openapi_schema = schema
        return schema

    app.openapi = openapi  # type: ignore[method-assign]
