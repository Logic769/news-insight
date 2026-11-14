"""Relevance scoring for category browsing and the home page.

Two different questions share one score shape:

* Anonymous visitors have no profile, so "relevant" means "high-quality and
  recent": category confidence, freshness, body completeness and tag confidence.
* Logged-in users additionally get their industry preferences, favorite and
  browse-history tags folded in.  The output stays in [0, 1] so the frontend can
  reuse its existing green/yellow/red badge thresholds.

The module deliberately does not import FastAPI: it is pure data + scoring and
can be unit-tested without an HTTP client.
"""

from __future__ import annotations

import datetime as dt
import json
import math
from dataclasses import dataclass
from typing import Any

from app import db

# Weights for the profile-free score.  They sum to 1.0.
_W_CONFIDENCE = 0.45
_W_FRESHNESS = 0.25
_W_COMPLETENESS = 0.15
_W_TAG = 0.15

# Profile-aware blend.  Content quality still anchors the score so a stale
# article cannot win only because it shares an industry.
_P_CONTENT = 0.35
_P_INDUSTRY = 0.30
_P_TAG = 0.20
_P_CATEGORY = 0.10
_P_SOURCE = 0.05

# 截止日期按站点墙钟日比较。容器默认 UTC，直接用 date.today() 会在
# 东八区 00:00–08:00 把「还剩 1 天」算成「已过期」。
_CST = dt.timezone(dt.timedelta(hours=8))


def _parse_time(value: Any) -> dt.datetime | None:
    if not value:
        return None
    raw = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # 历史行多数带 +08:00；无时区时按 UTC 处理，避免本机时区影响排序。
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _freshness(row: dict[str, Any], *, now: dt.datetime | None = None) -> float:
    current = now or dt.datetime.now(dt.timezone.utc)
    published = _parse_time(row.get("published_at_utc") or row.get("published_at"))
    if published is None:
        return 0.35
    days = max(0.0, (current - published).total_seconds() / 86400.0)
    # 30 天半衰期：一个月前的文章仍然能被看到，但不再压过本周公告。
    return math.exp(-days / 30.0)


def _tag_strength(tags: list[dict[str, Any]]) -> float:
    return max((float(t.get("confidence") or 0.0) for t in tags), default=0.0)


def content_score(row: dict[str, Any], *, now: dt.datetime | None = None) -> float:
    confidence = float(row.get("category_confidence") or 0.0)
    completeness = min(1.0, max(0.0, float(row.get("content_length") or 0) / 2000.0))
    score = (
        _W_CONFIDENCE * confidence
        + _W_FRESHNESS * _freshness(row, now=now)
        + _W_COMPLETENESS * completeness
        + _W_TAG * _tag_strength(row.get("tags") or [])
    )
    return max(0.0, min(1.0, score))


@dataclass
class Profile:
    industries: set[str]
    categories: set[str]
    sources: set[str]
    tag_weights: dict[str, float]

    @property
    def has_signal(self) -> bool:
        return bool(self.industries or self.categories or self.sources or self.tag_weights)


def build_profile(conn: Any, user_id: int) -> Profile:
    """Build a lightweight interest profile from preferences + favorites + history."""
    industries: set[str] = set()
    categories: set[str] = set()
    sources: set[str] = set()
    tag_weights: dict[str, float] = {}

    pref_row = conn.execute(
        "SELECT industries_json FROM user_preferences WHERE user_id = ?", (user_id,)
    ).fetchone()
    if pref_row is not None:
        try:
            raw = json.loads(pref_row["industries_json"] or "[]")
        except (TypeError, ValueError):
            raw = []
        industries.update(str(item) for item in raw if item)

    # favorites weigh 3x history: explicit collection is a stronger signal.
    for table, weight in (("favorites", 3.0), ("browse_history", 1.0)):
        rows = conn.execute(
            f"""SELECT a.category, a.industry, a.source, t.tag, t.confidence
                FROM {table} x
                JOIN analyzed_articles a ON a.article_id = x.article_id
                LEFT JOIN article_tags t ON t.article_id = a.article_id
                WHERE x.user_id = ?""",
            (user_id,),
        ).fetchall()
        for row in rows:
            if row["category"]:
                categories.add(str(row["category"]))
            if row["industry"]:
                industries.add(str(row["industry"]))
            if row["source"]:
                sources.add(str(row["source"]))
            if row["tag"]:
                tag = str(row["tag"])
                tag_weights[tag] = tag_weights.get(tag, 0.0) + weight * float(
                    row["confidence"] or 0.0
                )

    if tag_weights:
        peak = max(tag_weights.values()) or 1.0
        tag_weights = {tag: value / peak for tag, value in tag_weights.items()}
    return Profile(
        industries=industries or set(),
        categories=categories or set(),
        sources=sources or set(),
        tag_weights=tag_weights,
    )


def score_row(
    row: dict[str, Any],
    profile: Profile | None,
    *,
    now: dt.datetime | None = None,
) -> tuple[float, str]:
    base = content_score(row, now=now)
    if profile is None or not profile.has_signal:
        if base >= 0.75:
            return base, "高置信度近期公告"
        return base, "内容质量综合评分"

    industry = str(row.get("industry") or "")
    category = str(row.get("category") or "")
    source = str(row.get("source") or "")
    tag_hit = max(
        (
            profile.tag_weights.get(str(tag.get("tag")), 0.0) * float(tag.get("confidence") or 0.0)
            for tag in (row.get("tags") or [])
        ),
        default=0.0,
    )
    industry_hit = 1.0 if industry and industry in profile.industries else 0.0
    category_hit = 1.0 if category and category in profile.categories else 0.0
    source_hit = 1.0 if source and source in profile.sources else 0.0
    score = (
        _P_CONTENT * base
        + _P_INDUSTRY * industry_hit
        + _P_TAG * tag_hit
        + _P_CATEGORY * category_hit
        + _P_SOURCE * source_hit
    )
    reasons: list[str] = []
    if industry_hit:
        reasons.append("匹配关注行业")
    if tag_hit >= 0.5:
        reasons.append("与收藏/浏览主题相近")
    if category_hit:
        reasons.append("常看分类")
    if not reasons:
        reasons.append("内容质量综合评分")
    return max(0.0, min(1.0, score)), "、".join(reasons[:2])


def annotate_rows(
    rows: list[dict[str, Any]], profile: Profile | None, *, now: dt.datetime | None = None
) -> list[dict[str, Any]]:
    for row in rows:
        score, reason = score_row(row, profile, now=now)
        row["relevance"] = round(score, 4)
        row["relevance_reason"] = reason
    return rows


def recommendations(conn: Any, *, user_id: int | None, limit: int = 10) -> list[dict[str, Any]]:
    """Return the top-N ranked articles from a recent candidate pool.

    The candidate pool is intentionally recent-first and capped: ranking every
    historical row would make the home page slower as the archive grows, while
    users expect "recommended" to mean current material.
    """
    pool_size = max(limit * 8, 200)
    rows, _total, _undated = db.list_analyzed(
        conn, tag=None, limit=min(800, pool_size), offset=0, sort="published_desc"
    )
    profile = build_profile(conn, user_id) if user_id is not None else None
    annotate_rows(rows, profile)
    rows.sort(key=lambda item: (float(item.get("relevance") or 0.0), item.get("article_id", "")), reverse=True)
    return rows[: max(1, min(limit, 50))]


def upcoming_deadlines(conn: Any, *, limit: int = 5, today: dt.date | None = None) -> list[dict[str, Any]]:
    """Project-application deadlines in the near future, soonest first."""
    base = today or dt.datetime.now(_CST).date()
    rows = conn.execute(
        """SELECT e.article_id, e.value_json, e.raw_text AS deadline_raw,
                  a.title, a.source, a.url, a.published_at, a.category,
                  a.category_confidence
           FROM extractions e
           JOIN analyzed_articles a ON a.article_id = e.article_id
           WHERE e.schema_id = 'project_application' AND e.field = 'deadline'"""
    ).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        try:
            value = json.loads(row["value_json"] or "null")
        except (TypeError, ValueError):
            continue
        if not isinstance(value, str):
            continue
        try:
            deadline = dt.date.fromisoformat(value.strip()[:10])
        except ValueError:
            continue
        days_left = (deadline - base).days
        if days_left < 0:
            continue
        item = dict(row)
        item.pop("value_json", None)
        item["deadline"] = deadline.isoformat()
        item["days_left"] = days_left
        items.append(item)
    items.sort(key=lambda item: (item["deadline"], item["article_id"]))
    return items[: max(1, min(limit, 50))]
