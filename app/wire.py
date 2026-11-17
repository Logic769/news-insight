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
    404: 40401,
    405: 40501,
    408: 40801,
    409: 40901,
    422: 42201,
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
        response = await call_next(request)
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

        if isinstance(payload, dict) and "error" in payload:
            return _rebuild(response, error_body(response.status_code, payload))
        if isinstance(payload, dict) and "code" in payload and "msg" in payload:
            # 已经是统一信封（例如未来的新路由直接返回信封），不重复包装
            return _rebuild(response, payload)
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
    original_openapi = app.openapi

    def openapi() -> dict[str, Any]:
        schema = original_openapi()
        components = schema.setdefault("components", {}).setdefault("schemas", {})
        components["ApiError"] = {
            "type": "object",
            "required": ["code", "msg", "data"],
            "properties": {
                "code": {"type": "integer", "example": 40101},
                "msg": {"type": "string", "example": "未登录或登录已过期"},
                "data": {"nullable": True},
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
                    responses.setdefault(
                        status,
                        {
                            "description": "统一错误响应",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/ApiError"}
                                }
                            },
                        },
                    )
        return schema

    app.openapi = openapi  # type: ignore[method-assign]
