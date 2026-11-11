"""Minimal OpenAI-compatible chat client that always answers with JSON.

Design notes (borrowed from refuel-ai/autolabel's closed-set flow):

* The caller supplies the allowed output space; this layer never invents labels.
* Responses are parsed defensively: models wrap JSON in prose or fences often.
* When no API key is configured we report `available == False` so callers can
  fall back to deterministic rules instead of failing the whole run.
"""

from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from app.settings import Settings

LOG = logging.getLogger("news_insight.llm")

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)

# 429/5xx 是网关限流或瞬时故障，退避后重试；401/400 等配置错误重试没有意义。
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class LLMError(RuntimeError):
    pass


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    """Parse Retry-After (seconds or HTTP date); None when absent/unparseable."""
    raw = (resp.headers.get("retry-after") or "").strip()
    if not raw:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        try:
            when = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
    return max(0.0, seconds)


def _extract_json(text: str) -> dict[str, Any] | None:
    text = (text or "").strip()
    if not text:
        return None
    candidates = [text]
    fenced = _FENCE_RE.search(text)
    if fenced:
        candidates.insert(0, fenced.group(1))
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for cand in candidates:
        try:
            parsed = json.loads(cand)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


class LLMClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        # 全进程共享一个连接池：每次调用新建 httpx.Client 会在 100 并发下
        # 反复做 TCP/TLS 握手，并把 TIME_WAIT 堆满。httpx.Client 本身线程安全。
        self._client = httpx.Client(
            timeout=settings.llm_timeout,
            limits=httpx.Limits(
                max_connections=max(20, settings.analyze_concurrency * 2),
                max_keepalive_connections=min(100, max(10, settings.analyze_concurrency)),
            ),
        )
        self._semaphore = threading.BoundedSemaphore(settings.llm_max_concurrency)

    @property
    def available(self) -> bool:
        return self.settings.llm_enabled

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "LLMClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def chat_json(self, system: str, user: str, *, retries: int = 2) -> dict[str, Any]:
        if not self.available:
            raise LLMError("未配置 LLM_API_KEY")
        url = f"{self.settings.llm_base_url}/chat/completions"
        payload = {
            "model": self.settings.llm_model,
            "temperature": self.settings.llm_temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
        }
        last_err: Exception | None = None
        for attempt in range(retries + 1):
            resp: httpx.Response | None = None
            retry_after: float | None = None
            try:
                with self._semaphore:
                    resp = self._client.post(
                        url,
                        json=payload,
                        headers={
                            "Authorization": f"Bearer {self.settings.llm_api_key}",
                            "Content-Type": "application/json",
                        },
                    )
            except httpx.HTTPError as exc:
                last_err = exc
                LOG.warning("LLM 请求失败(第 %s 次): %s", attempt + 1, exc)
            else:
                if resp.status_code < 400:
                    try:
                        data = resp.json()
                        if not isinstance(data, dict):
                            raise ValueError(f"LLM 响应 JSON 不是对象: {type(data).__name__}")
                        content = (
                            data.get("choices", [{}])[0].get("message", {}).get("content", "")
                        )
                        parsed = _extract_json(content)
                    except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
                        last_err = exc
                        LOG.warning("LLM 响应解析失败(第 %s 次): %s", attempt + 1, exc)
                    else:
                        if parsed is not None:
                            return parsed
                        last_err = LLMError("LLM 响应中没有可解析的 JSON")
                        LOG.warning("LLM 响应解析失败(第 %s 次): 没有 JSON", attempt + 1)
                else:
                    err = LLMError(f"LLM 返回 {resp.status_code}: {resp.text[:300]}")
                    last_err = err
                    retry_after = _retry_after_seconds(resp)
                    LOG.warning("LLM 调用失败(第 %s 次): %s", attempt + 1, err)
                    if resp.status_code not in _RETRYABLE_STATUS:
                        raise err
            if attempt < retries:
                # 优先尊重网关的 Retry-After；否则指数退避，并加 ±20% 抖动避免
                # 100 个线程在同一秒一起重试（惊群）。
                base = retry_after if retry_after is not None else 0.5 * (2**attempt)
                delay = min(30.0, max(0.1, base)) * (0.8 + random.random() * 0.4)
                time.sleep(delay)
        raise LLMError(str(last_err))
