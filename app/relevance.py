"""文章级语义相关性评分。

与 `app.search` 的「查询-文章相关度」不同：这里给每篇文章打一个与具体查询
无关的 0-1 分，衡量它对「企业申报 / 产业政策情报」这个业务场景的语义价值。
分数在分析阶段由一次独立的 LLM 调用产生并落库，下游列表、检索、推荐都直接复用。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any

from app.llm import LLMClient, LLMError
from app.settings import Settings

LOG = logging.getLogger("news_insight.relevance")

RELEVANCE_SYSTEM_PROMPT = """你是企业申报与产业政策情报分析员。请评估给定新闻文章对
「企业申报、产业政策、人才申报、企业资质、资金补贴」业务场景的语义相关性。

评分标准（0-100 的整数）：
- 90-100：直接涉及具体申报/补贴/资质政策，信息具体、可行动（有申报条件、截止时间、主管部门等）。
- 70-89：主题高度相关，但信息较概括或只是政策解读/新闻通报。
- 40-69：与产业、企业、政策沾边，但不是申报或扶持信息。
- 0-39：无关内容（娱乐、体育、社会新闻、广告、招聘等）。

只输出 JSON 对象：{"score": <0-100 的整数>, "reason": "<不超过80字的中文依据>"}。
不要输出 markdown 代码块或额外说明。"""

_CATEGORY_PRIOR = {
    "project_application": 0.95,
    "funding_subsidy": 0.90,
    "talent_application": 0.86,
    "industry_policy": 0.82,
    "enterprise_certification": 0.80,
    "other": 0.20,
}

_KEYWORDS = (
    "申报", "补贴", "资助", "扶持", "奖励", "政策", "资质", "认定", "备案",
    "项目", "资金", "人才", "企业", "专精特新", "高新技术", "科技型", "中小企业",
    "截止", "条件", "主管部门", "通知", "公示", "实施细则",
)


@dataclass(frozen=True)
class RelevanceHit:
    score: float
    engine: str
    reason: str


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _rule_score(
    *,
    title: str,
    body: str,
    category: str,
    category_confidence: float | None,
) -> RelevanceHit:
    """确定性兜底：无 LLM 时仍能让下游拿到稳定、可解释的分数。"""
    text = f"{title or ''}\n{body or ''}"
    prior = _CATEGORY_PRIOR.get(category or "", 0.30)
    confidence = _clamp(float(category_confidence or 0.0))
    hits = sum(1 for keyword in _KEYWORDS if keyword in text)
    density = min(1.0, hits / 8.0)
    score = _clamp(0.55 * prior + 0.20 * confidence + 0.25 * density)
    prior_label = "分类先验" if category in _CATEGORY_PRIOR else "未分类缺省"
    reason = f"规则评分：{prior_label}{prior:.2f}、分类置信度{confidence:.2f}、关键词命中{hits}个"
    return RelevanceHit(round(score, 4), "rules", reason)


def _parse_llm_score(payload: dict[str, Any]) -> tuple[float, str]:
    raw = payload.get("score")
    if isinstance(raw, bool):
        raise ValueError("score 不能是布尔值")
    value = float(str(raw))
    if not math.isfinite(value):
        raise ValueError("score 必须是有限数值")
    if not 0 <= value <= 100:
        value = max(0.0, min(100.0, value))
    reason = str(payload.get("reason") or "").strip()
    return round(value / 100.0, 4), reason[:300]


def score_relevance(
    settings: Settings,
    llm: LLMClient,
    *,
    title: str,
    body: str,
    category: str = "",
    category_confidence: float | None = None,
) -> RelevanceHit:
    """一次独立 LLM 调用；失败时退回确定性规则分，绝不让分析流水线中断。"""
    if llm.available:
        body_text = (body or "")[: settings.relevance_body_chars]
        user = (
            f"文章标题：{title or ''}\n"
            f"文章正文（截取前 {settings.relevance_body_chars} 字）：\n{body_text}\n"
            f"分类提示：{category or '未知'}"
        )
        try:
            payload = llm.chat_json(RELEVANCE_SYSTEM_PROMPT, user, retries=1)
            score, reason = _parse_llm_score(payload)
            return RelevanceHit(score, "llm", reason or "LLM 语义相关性评分")
        except (LLMError, ValueError, TypeError, KeyError) as exc:
            LOG.warning("语义相关性 LLM 评分失败，降级规则：%s", exc)
    return _rule_score(
        title=title,
        body=body,
        category=category,
        category_confidence=category_confidence,
    )
