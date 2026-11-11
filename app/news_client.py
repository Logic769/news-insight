"""Read-only client for the upstream news-crawler `/v1` API.

The upstream contract (verified against news_api/models.py) is:

    GET /v1/articles?status=&page=&page_size=&order=&published_from=&published_to=
    -> {"data": [ArticleOut...], "meta": {page, page_size, total, total_pages, has_next}}

ArticleOut fields used here: id, source, url, title, published_at,
published_at_utc, timezone, content, raw_content, content_length, updated_at.

`raw_content` is only populated on the detail endpoint, so the list call is
cheap: we pull metadata first and fetch bodies for the articles we actually
analyse.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Iterable

import httpx

from app.settings import Settings

LOG = logging.getLogger("news_insight.news_client")


class UpstreamError(RuntimeError):
    """Raised when the news-crawler API is unreachable or returns an error."""


@dataclass
class NewsArticle:
    id: str
    source: str
    url: str
    title: str
    published_at: str | None
    published_at_utc: str | None
    timezone: str
    content: str | None
    raw_content: str | None
    content_length: int
    updated_at: str | None
    # False = 详情接口没拿到（正文为空不代表文章没正文），调用方不应据此定档
    body_ok: bool = True

    @property
    def body(self) -> str:
        """Best available body text, preferring the paragraph-preserving one."""
        return (self.raw_content or self.content or "").strip()

    @classmethod
    def from_payload(cls, row: dict[str, Any]) -> "NewsArticle":
        return cls(
            id=str(row.get("id") or ""),
            source=str(row.get("source") or ""),
            url=str(row.get("url") or ""),
            title=str(row.get("title") or row.get("list_title") or ""),
            published_at=row.get("published_at"),
            published_at_utc=row.get("published_at_utc"),
            timezone=str(row.get("timezone") or "Asia/Shanghai"),
            content=row.get("content"),
            raw_content=row.get("raw_content"),
            content_length=int(row.get("content_length") or 0),
            updated_at=row.get("updated_at"),
        )


class NewsClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        # auto 模式回退到演示数据时记录原因，供 pipeline 写进 runs.detail
        self.fallback_reason = ""

    # ------------------------------------------------------------------ live
    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.settings.news_api_key:
            headers["X-API-Key"] = self.settings.news_api_key
        return headers

    def _live_list(self, page: int, page_size: int, order: str) -> dict[str, Any]:
        url = f"{self.settings.news_base_url}/v1/articles"
        params = {"status": "ok", "page": page, "page_size": page_size, "order": order}
        try:
            with httpx.Client(timeout=self.settings.news_timeout) as client:
                resp = client.get(url, params=params, headers=self._headers())
        except httpx.HTTPError as exc:  # network/DNS/timeout
            raise UpstreamError(f"无法连接新闻 API {url}: {exc}") from exc
        if resp.status_code == 401:
            raise UpstreamError("新闻 API 拒绝访问：NEWS_API_KEY 缺失或无效（需 X-API-Key）")
        if resp.status_code >= 400:
            raise UpstreamError(f"新闻 API 返回 {resp.status_code}: {resp.text[:200]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise UpstreamError(f"新闻 API 返回非 JSON: {exc}") from exc

    def _live_detail(self, article_id: str) -> dict[str, Any]:
        url = f"{self.settings.news_base_url}/v1/articles/{article_id}"
        try:
            with httpx.Client(timeout=self.settings.news_timeout) as client:
                resp = client.get(url, headers=self._headers())
        except httpx.HTTPError as exc:
            raise UpstreamError(f"获取文章详情失败 {article_id}: {exc}") from exc
        if resp.status_code >= 400:
            raise UpstreamError(f"文章详情 {article_id} 返回 {resp.status_code}")
        return resp.json()

    # ------------------------------------------------------------------ demo
    def _demo_rows(self) -> list[dict[str, Any]]:
        path = self.settings.fixtures_dir / "articles.json"
        if not path.exists():
            raise UpstreamError(f"演示数据缺失: {path}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            raise UpstreamError(f"演示数据格式不正确: {path}")
        return rows

    # --------------------------------------------------------------- public
    def list_articles(self, limit: int, offset: int = 0) -> tuple[list[NewsArticle], str]:
        """Return up to `limit` articles and the mode actually used."""
        if self.settings.news_source_mode == "demo":
            rows = self._demo_rows()
            return [NewsArticle.from_payload(r) for r in rows[offset : offset + limit]], "demo"

        try:
            payload = self._live_list(page=1, page_size=max(limit, self.settings.news_page_size), order="published_desc")
            rows = payload.get("data") or []
            return [NewsArticle.from_payload(r) for r in rows][offset : offset + limit], "live"
        except UpstreamError as exc:
            if self.settings.news_source_mode == "live":
                raise
            LOG.warning("上游不可用，回退到内置演示数据: %s", exc)
            self.fallback_reason = str(exc)
            rows = self._demo_rows()
            return [NewsArticle.from_payload(r) for r in rows][offset : offset + limit], "demo"

    def hydrate_bodies(self, articles: Iterable[NewsArticle], mode: str) -> list[NewsArticle]:
        """Fill in body text. Demo rows already carry it; live rows need the detail call."""
        out: list[NewsArticle] = []
        for art in articles:
            if mode == "live" and not art.body:
                try:
                    detail = self._live_detail(art.id)
                except UpstreamError as exc:
                    LOG.warning("详情获取失败，标记待重试 %s: %s", art.id, exc)
                    art.body_ok = False
                    out.append(art)
                    continue
                if isinstance(detail, dict) and isinstance(detail.get("data"), dict):
                    detail = detail["data"]
                merged = NewsArticle.from_payload({**detail, "id": art.id})
                # keep list-level fields if the detail payload omits them
                merged.title = merged.title or art.title
                merged.published_at_utc = merged.published_at_utc or art.published_at_utc
                merged.published_at = merged.published_at or art.published_at
                out.append(merged)
            else:
                out.append(art)
        return out
