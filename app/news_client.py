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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Iterable

import httpx

from app.settings import Settings

LOG = logging.getLogger("news_insight.news_client")

# 上游 /v1/articles 的 page_size 上限是 100（news_api/app.py: Query(20, ge=1, le=100)）。
# 这里不能跟随 limit 放大，否则 limit>100 会被上游 422 拒绝并静默回退演示数据。
_MAX_PAGE_SIZE = 100


class UpstreamError(RuntimeError):
    """Raised when the news-crawler API is unreachable or returns an error."""

    def __init__(self, message: str, *, status_code: int | None = None, fallbackable: bool = True):
        super().__init__(message)
        self.status_code = status_code
        # 4xx 是本端参数/鉴权问题，回退演示数据只会掩盖错误；只有网络/5xx 才允许 auto 回退。
        self.fallbackable = fallbackable


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


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
            content_length=_to_int(row.get("content_length")),
            updated_at=row.get("updated_at"),
        )


class NewsClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        # auto 模式回退到演示数据时记录原因，供 pipeline 写进 runs.detail
        self.fallback_reason = ""
        # 懒创建：测试会替换 httpx.Client，构造时不要提前实例化。
        self._http: httpx.Client | None = None

    def _client(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(timeout=self.settings.news_timeout)
        return self._http

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None

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
            resp = self._client().get(url, params=params, headers=self._headers())
        except httpx.HTTPError as exc:  # network/DNS/timeout
            raise UpstreamError(f"无法连接新闻 API {url}: {exc}") from exc
        if resp.status_code == 401:
            raise UpstreamError(
                "新闻 API 拒绝访问：NEWS_API_KEY 缺失或无效（需 X-API-Key）",
                status_code=401,
                fallbackable=False,
            )
        if resp.status_code >= 400:
            raise UpstreamError(
                f"新闻 API 返回 {resp.status_code}: {resp.text[:200]}",
                status_code=resp.status_code,
                fallbackable=resp.status_code >= 500,
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise UpstreamError(f"新闻 API 返回非 JSON: {exc}") from exc

    def _live_detail(self, article_id: str) -> dict[str, Any]:
        url = f"{self.settings.news_base_url}/v1/articles/{article_id}"
        try:
            resp = self._client().get(url, headers=self._headers())
        except httpx.HTTPError as exc:
            raise UpstreamError(f"获取文章详情失败 {article_id}: {exc}") from exc
        if resp.status_code >= 400:
            raise UpstreamError(
                f"文章详情 {article_id} 返回 {resp.status_code}",
                status_code=resp.status_code,
                fallbackable=resp.status_code >= 500,
            )
        try:
            payload = resp.json()
        except ValueError as exc:
            raise UpstreamError(f"文章详情 {article_id} 返回非 JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise UpstreamError(f"文章详情 {article_id} 返回格式不正确")
        return payload

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
        """Return up to limit articles and the mode actually used.

        Live mode pages through /v1/articles until enough rows are collected.
        A single request would silently cap a 10k-article batch at one page.
        """
        if self.settings.news_source_mode == "demo":
            rows = self._demo_rows()
            return [NewsArticle.from_payload(r) for r in rows[offset : offset + limit]], "demo"

        try:
            page_size = max(1, min(max(limit, self.settings.news_page_size), _MAX_PAGE_SIZE))
            rows: list[dict[str, Any]] = []
            page = 1
            while len(rows) < offset + limit:
                payload = self._live_list(page=page, page_size=page_size, order="published_desc")
                batch = payload.get("data") or []
                if not batch:
                    break
                rows.extend(batch)
                meta = payload.get("meta") or {}
                total_pages = _to_int(meta.get("total_pages")) or 0
                if not meta.get("has_next") or (total_pages and page >= total_pages):
                    break
                page += 1
            return [NewsArticle.from_payload(r) for r in rows][offset : offset + limit], "live"
        except UpstreamError as exc:
            if self.settings.news_source_mode == "live" or not exc.fallbackable:
                raise
            LOG.warning("上游不可用，回退到内置演示数据: %s", exc)
            self.fallback_reason = str(exc)
            rows = self._demo_rows()
            return [NewsArticle.from_payload(r) for r in rows][offset : offset + limit], "demo"

    def hydrate_bodies(self, articles: Iterable[NewsArticle], mode: str) -> list[NewsArticle]:
        """Fill in body text. Demo rows already carry it; live rows need the detail call."""
        items = list(articles)
        if mode != "live":
            return items
        # 同一个 article_id 不应因上游分页抖动被分析两次（也会重复烧 LLM 额度）。
        seen: set[str] = set()
        unique: list[NewsArticle] = []
        for art in items:
            if art.id and art.id in seen:
                continue
            seen.add(art.id)
            unique.append(art)
        items = unique
        pending = [art for art in items if not art.body]
        if not pending:
            return items

        def fill(art: NewsArticle) -> None:
            try:
                detail = self._live_detail(art.id)
            except (UpstreamError, ValueError, KeyError, TypeError) as exc:
                LOG.warning("详情获取失败，标记待重试 %s: %s", art.id, exc)
                art.body_ok = False
                return
            if isinstance(detail, dict) and isinstance(detail.get("data"), dict):
                detail = detail["data"]
            merged = NewsArticle.from_payload({**detail, "id": art.id})
            # keep list-level fields if the detail payload omits them
            art.title = merged.title or art.title
            art.published_at_utc = merged.published_at_utc or art.published_at_utc
            art.published_at = merged.published_at or art.published_at
            art.content = merged.content
            art.raw_content = merged.raw_content
            art.content_length = merged.content_length or art.content_length

        concurrency = max(1, min(self.settings.news_detail_concurrency, len(pending)))
        if concurrency == 1:
            for art in pending:
                fill(art)
        else:
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                list(pool.map(fill, pending))
        return items
