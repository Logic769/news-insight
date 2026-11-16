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
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from app import db

# Weights for the profile-free score.  They sum to 1.0.
# 语义相关性占 20%：它是文章本身对申报/政策场景的价值，与用户画像无关，
# 因此匿名用户也能受益；分类置信度仍保留较大权重，避免打分模型单点主导。
_W_CONFIDENCE = 0.35
_W_FRESHNESS = 0.20
_W_COMPLETENESS = 0.10
_W_TAG = 0.15
_W_SEMANTIC = 0.20

# Profile-aware blend.  Content quality still anchors the score so a stale
# article cannot win only because it shares an industry.
_P_CONTENT = 0.25
_P_INDUSTRY = 0.25
_P_TAG = 0.15
_P_CATEGORY = 0.10
_P_SOURCE = 0.05
_P_SEMANTIC = 0.20
# 搜索是比「浏览过什么」更直接的意图信号；作为额外加分项，不挤占内容质量权重。
_P_SEARCH = 0.10

# 截止日期按站点墙钟日比较。容器默认 UTC，直接用 date.today() 会在
# 东八区 00:00–08:00 把「还剩 1 天」算成「已过期」。
_CST = dt.timezone(dt.timedelta(hours=8))

# 搜索词命中标题用的停用词表（casefold 后比较）。
#
# 生产库 481 篇文章的标题里「通知」出现 36.6%、「项目」23.1%、「申报」15.4%——
# 这几个词几乎出现在每一篇公告标题里，用户搜过一次「申报通知」之后，画像就把
# 它当成强信号，于是「匹配历史搜索」这个理由会挂到几乎所有文章上，排序退化成
# 按新鲜度排、理由却写着「匹配历史搜索」，属于「看起来个性化、其实没个性化」。
#
# 只挡「在标题里没有区分度、但单看像主题词」的通用政务/申报用语；具体的领域词
# （新能源、专精特新、数字化改造…）一律放行——那才是画像真正想要的信号。
_SEARCH_STOPWORDS = frozenset(
    {
        "通知", "公告", "公示", "通告", "通报", "发布", "印发", "关于", "有关",
        "项目", "申报", "申请", "申报通知", "项目申报", "工作", "工作通知",
        "有关工作", "要求", "事项", "事宜", "意见", "办法", "规定", "文件",
        "名单", "结果", "情况", "信息", "相关", "开展", "组织", "实施",
        "单位", "部门", "政府", "省市", "时间", "截止",
        "政策", "监督", "审核", "评审", "推荐", "报送",
        # 年份要连「年」一起挡：只放 "2026" 挡不住标题里实际写的 "2026年"。
        "2024", "2025", "2026", "2027", "2024年", "2025年", "2026年", "2027年",
        # 下面这些是「补齐子串形态」，不是新增泛词：政务标题里高频，且常以组合形式出现
        # （"申报工作"、"有关事项"、"以下简称"），等值匹配的单表挡不住。
        "年度", "申报工作", "有关事项", "以下简称", "通知如下", "现将",
    }
)

# 在整串之外还要拆出的通用子串。
#
# 停用词表是等值匹配，而中文查询没有空格——用户打「2026年度专精特新申报通知」时，
# build_profile 只产出一条整串，整串在标题里永远匹配不到（实测命中 False），这条
# 搜索就等于没发生。所以在整串之外，再按这些短语把它切一刀，让有区分度的部分
# （这里是「专精特新」）露出来。
#
# 只按「够长、够通用」的短语切，不按 2 字滑窗：滑窗会把「专精」「精特」「特新」
# 一类的碎片全部塞进画像，那是噪声不是信号。
_QUERY_SPLIT_PATTERNS = (
    "申报工作", "有关事项", "以下简称", "通知如下",
    "申报通知", "项目申报", "工作通知", "有关工作",
    "通知", "公告", "公示", "印发", "关于", "申报", "项目", "年度",
)

# 单字与两位数字没有区分度：搜「3」不该让全库带 3 的标题都算命中历史搜索。
_MIN_SEARCH_TERM_LEN = 2


def _usable_search_term(term: str) -> bool:
    """这个词是否值得进画像：长度够、且不是通用政务用语。

    长度用 casefold 之后的字符数判断；中文没有词边界，只能按字数和停用词表挡，
    这是刻意的取舍——宁可少收一个信号，也不要让「通知」这种词把排序带偏。
    """
    if not (_MIN_SEARCH_TERM_LEN <= len(term) <= 40):
        return False
    return term not in _SEARCH_STOPWORDS


def _query_fragments(raw_query: str) -> list[str]:
    """把无空格的中文长查询切成有区分度的片段。

    只按 _QUERY_SPLIT_PATTERNS 里的通用短语切，返回**除整串之外**的片段：
    「2026年度专精特新申报通知」→ ["专精特新"]。切不出东西时返回空列表
    （「新能源汽车」这种情况），调用方据此决定是否保留整串。
    """
    pattern = "|".join(re.escape(item) for item in _QUERY_SPLIT_PATTERNS)
    whole = raw_query.casefold()
    fragments: list[str] = []
    for fragment in re.split(pattern, raw_query):
        fragment = fragment.strip()
        if not (_MIN_SEARCH_TERM_LEN <= len(fragment) <= 40):
            continue
        if fragment.casefold() == whole or fragment.casefold() in {
            item.casefold() for item in fragments
        }:
            continue
        fragments.append(fragment)
    return fragments


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
    semantic = max(0.0, min(1.0, float(row.get("relevance_score") or 0.0)))
    score = (
        _W_CONFIDENCE * confidence
        + _W_FRESHNESS * _freshness(row, now=now)
        + _W_COMPLETENESS * completeness
        + _W_TAG * _tag_strength(row.get("tags") or [])
        + _W_SEMANTIC * semantic
    )
    return max(0.0, min(1.0, score))


@dataclass
class Profile:
    industries: set[str]
    categories: set[str]
    sources: set[str]
    tag_weights: dict[str, float]
    search_terms: dict[str, float] = field(default_factory=dict)

    @property
    def has_signal(self) -> bool:
        return bool(
            self.industries
            or self.categories
            or self.sources
            or self.tag_weights
            or self.search_terms
        )


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

    # 搜索历史是显式意图：查询词本身进 search_terms，点击过的文章再按 2x
    # 权重补标签/分类/行业/来源。旧库尚未建表时跳过，不让画像拖垮首页。
    search_terms: dict[str, float] = {}
    clicked_ids: list[str] = []
    try:
        history_rows = conn.execute(
            "SELECT query, clicked_article_id FROM user_search_history"
            " WHERE user_id = ? ORDER BY created_at DESC, id DESC LIMIT 100",
            (user_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        history_rows = []
    for row in history_rows:
        raw_query = str(row["query"] or "").strip()
        if raw_query:
            # 中文查询没有空格，整串保留；同时拆出用空格/标点分隔的短词。
            fragments = _query_fragments(raw_query)
            # 只剔除「无空格、且被切开了」的整串。
            #
            # 中文长查询「2026年度专精特新申报通知」原样出现在标题里的概率接近零，
            # 留着它只是一个永远命不中的键；但带空格的查询（「新能源 申报」）整串
            # 本来就是用户显式分隔的形态，语义完整，必须保留——那是既有行为。
            whole_is_redundant = len(fragments) > 0 and not re.search(r"\s", raw_query.strip())
            parts = [raw_query, *re.split(r"[\s,，;；、|/]+", raw_query)]
            for part in parts:
                term = part.strip().casefold()
                if whole_is_redundant and term == raw_query.casefold():
                    continue
                if _usable_search_term(term):
                    search_terms[term] = search_terms.get(term, 0.0) + 1.0
            # 无空格的中文长串再切一次，让有区分度的部分露出来（实测
            # 「2026年度专精特新申报通知」整串命中 False，「专精特新」命中 True）。
            # 片段照样过 _usable_search_term，通用词被滤掉。
            for fragment in fragments:
                term = fragment.strip().casefold()
                if _usable_search_term(term):
                    search_terms[term] = search_terms.get(term, 0.0) + 1.0
        clicked = str(row["clicked_article_id"] or "").strip()
        if clicked:
            clicked_ids.append(clicked)
    if clicked_ids:
        for start in range(0, len(clicked_ids), 500):
            chunk = clicked_ids[start : start + 500]
            placeholders = ",".join("?" for _ in chunk)
            for row in conn.execute(
                "SELECT a.category, a.industry, a.source, t.tag, t.confidence"
                " FROM analyzed_articles a"
                " LEFT JOIN article_tags t ON t.article_id = a.article_id"
                " WHERE a.article_id IN (" + placeholders + ")",
                chunk,
            ):
                if row["category"]:
                    categories.add(str(row["category"]))
                if row["industry"]:
                    industries.add(str(row["industry"]))
                if row["source"]:
                    sources.add(str(row["source"]))
                if row["tag"]:
                    tag = str(row["tag"])
                    tag_weights[tag] = tag_weights.get(tag, 0.0) + 2.0 * float(
                        row["confidence"] or 0.0
                    )
    if tag_weights:
        peak = max(tag_weights.values()) or 1.0
        tag_weights = {tag: value / peak for tag, value in tag_weights.items()}
    if search_terms:
        peak = max(search_terms.values()) or 1.0
        search_terms = {term: value / peak for term, value in search_terms.items()}
    return Profile(
        industries=industries or set(),
        categories=categories or set(),
        sources=sources or set(),
        tag_weights=tag_weights,
        search_terms=search_terms,
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
    semantic = max(0.0, min(1.0, float(row.get("relevance_score") or 0.0)))
    title = str(row.get("title") or "").casefold()
    search_hit = 0.0
    for term, weight in profile.search_terms.items():
        # 画像里已经滤过停用词，这里再判一次是因为 search_terms 可能由
        # 老库/直接构造的 Profile 传进来（测试与历史数据都走过这条路）。
        if not _usable_search_term(term):
            continue
        if term in title:
            search_hit = max(search_hit, min(1.0, weight))
    score = (
        _P_CONTENT * base
        + _P_INDUSTRY * industry_hit
        + _P_TAG * tag_hit
        + _P_CATEGORY * category_hit
        + _P_SOURCE * source_hit
        + _P_SEMANTIC * semantic
        + _P_SEARCH * search_hit
    )
    reasons: list[str] = []
    if industry_hit:
        reasons.append("匹配关注行业")
    if search_hit > 0:
        reasons.append("匹配历史搜索")
    if tag_hit >= 0.5:
        reasons.append("与收藏/浏览主题相近")
    if category_hit:
        reasons.append("常看分类")
    if semantic >= 0.7:
        reasons.append("语义相关度高")
    if not reasons:
        reasons.append("内容质量综合评分")
    return max(0.0, min(1.0, score)), "、".join(reasons[:2])


def annotate_rows(
    rows: list[dict[str, Any]], profile: Profile | None, *, now: dt.datetime | None = None
) -> list[dict[str, Any]]:
    for row in rows:
        score, reason = score_row(row, profile, now=now)
        row["relevance"] = round(score, 4)
        # relevance_reason 属于文章级语义评分（relevance_score）；画像排序理由
        # 单独放 profile_reason，避免同一字段被两套分数互相覆盖。
        row["profile_reason"] = reason
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


def _future_deadline_items(
    conn: Any, *, today: dt.date | None = None
) -> list[dict[str, Any]]:
    """All project-application deadlines still ahead, soonest first."""
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
    return items


def upcoming_deadlines(
    conn: Any, *, limit: int = 5, today: dt.date | None = None
) -> list[dict[str, Any]]:
    """Project-application deadlines in the near future, soonest first."""
    return _future_deadline_items(conn, today=today)[: max(1, min(limit, 50))]


def count_upcoming_deadlines(conn: Any, *, today: dt.date | None = None) -> int:
    """Total number of deadlines still ahead (the endpoint's meta.total)."""
    return len(_future_deadline_items(conn, today=today))
