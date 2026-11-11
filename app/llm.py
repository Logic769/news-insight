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
import re
from typing import Any

import httpx

from app.settings import Settings

LOG = logging.getLogger("news_insight.llm")

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


class LLMError(RuntimeError):
    pass


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

    @property
    def available(self) -> bool:
        return self.settings.llm_enabled

    def chat_json(self, system: str, user: str, *, retries: int = 1) -> dict[str, Any]:
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
            try:
                with httpx.Client(timeout=self.settings.llm_timeout) as client:
                    resp = client.post(
                        url,
                        json=payload,
                        headers={
                            "Authorization": f"Bearer {self.settings.llm_api_key}",
                            "Content-Type": "application/json",
                        },
                    )
                if resp.status_code >= 400:
                    raise LLMError(f"LLM 返回 {resp.status_code}: {resp.text[:300]}")
                data = resp.json()
                content = (
                    data.get("choices", [{}])[0].get("message", {}).get("content", "")
                )
                parsed = _extract_json(content)
                if parsed is None:
                    raise LLMError("LLM 响应中没有可解析的 JSON")
                return parsed
            except (httpx.HTTPError, LLMError, ValueError, KeyError, IndexError) as exc:
                last_err = exc
                LOG.warning("LLM 调用失败(第 %s 次): %s", attempt + 1, exc)
        raise LLMError(str(last_err))
