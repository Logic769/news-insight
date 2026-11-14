"""文章级语义相关性评分：LLM 主路径、规则兜底与解析边界。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.llm import LLMError
from app.relevance import _parse_llm_score, score_relevance


class FakeLLM:
    def __init__(self, *, available=True, payload=None, error=None):
        self.available = available
        self.payload = payload
        self.error = error
        self.calls: list[tuple[str, str, int]] = []

    def chat_json(self, system: str, user: str, *, retries: int = 2) -> dict:
        self.calls.append((system, user, retries))
        if self.error is not None:
            raise self.error
        return self.payload or {}


def _settings(*, relevance_body_chars: int = 4000):
    # score_relevance 只读这两个字段，避免为纯函数测试构造完整 Settings。
    return SimpleNamespace(relevance_body_chars=relevance_body_chars)


def test_rule_score_is_bounded_and_prefers_actionable_category() -> None:
    project = score_relevance(
        _settings(), FakeLLM(available=False),
        title="关于组织申报2026年度重点研发计划项目的通知",
        body="申报截止时间为2026年4月20日，单个项目资助不超过500万元。",
        category="project_application",
        category_confidence=0.95,
    )
    other = score_relevance(
        _settings(), FakeLLM(available=False),
        title="国产大模型企业完成B轮融资",
        body="本轮融资将用于技术研发和市场推广。",
        category="other",
        category_confidence=0.9,
    )
    assert project.engine == other.engine == "rules"
    assert 0.0 <= other.score < project.score <= 1.0
    assert "分类先验" in project.reason


def test_llm_score_is_parsed_and_body_is_truncated() -> None:
    llm = FakeLLM(payload={"score": 88.6, "reason": "政策信息具体"})
    hit = score_relevance(
        _settings(relevance_body_chars=2), llm,
        title="申报通知", body="abcdef", category="project_application",
    )
    assert hit.engine == "llm"
    assert hit.score == pytest.approx(0.886)
    assert hit.reason == "政策信息具体"
    assert len(llm.calls) == 1, "语义评分必须是独立的一次调用"
    _, user, retries = llm.calls[0]
    assert "正文（截取前 2 字）：\nab" in user
    assert "abcdef" not in user
    assert retries == 1


def test_llm_failure_falls_back_to_rules_without_raising() -> None:
    hit = score_relevance(
        _settings(), FakeLLM(error=LLMError("网关不可用")),
        title="专精特新中小企业申报通知", body="申报条件与奖励标准",
        category="project_application", category_confidence=0.9,
    )
    assert hit.engine == "rules"
    assert 0.0 <= hit.score <= 1.0


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(0, 0.0), (100, 1.0), (-5, 0.0), (105, 1.0), ("67.5", 0.675)],
)
def test_parse_llm_score_clamps_and_normalizes(raw, expected) -> None:
    score, reason = _parse_llm_score({"score": raw, "reason": "依据"})
    assert score == pytest.approx(expected)
    assert reason == "依据"


def test_parse_llm_score_rejects_bool_and_truncates_reason() -> None:
    with pytest.raises(ValueError):
        _parse_llm_score({"score": True})
    score, reason = _parse_llm_score({"score": 80, "reason": "x" * 400})
    assert score == pytest.approx(0.8)
    assert len(reason) == 300
