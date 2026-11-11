"""申报准备清单：规则降级、LLM 校验与落库。"""

from __future__ import annotations

import pytest

from app import db
from app.checklist import MAX_ITEMS, build_checklist, build_with_rules, parse_llm_items
from app.llm import LLMError
from app.pipeline import analyze_batch
from app.settings import load_settings

FULL_FIELDS = {
    "program_name": "雄安新区科技创新专项",
    "eligible_applicants": "企业、高校、科研院所",
    "materials": ["项目申报书", "预算表"],
    "deadline": "2025-12-01",
    "deadline_raw": "2025年11月25日17:00至2025年12月1日9:00",
    "funding_amount": "500万元",
    "application_channel": "国科管系统",
    "contact": "010-58882999",
}

BODY = "申报截止时间为2025年12月1日9:00，请通过国科管系统提交项目申报书。"


class FakeLLM:
    def __init__(self, payload=None, error=None):
        self.available = True
        self.payload = payload
        self.error = error

    def chat_json(self, system, user):
        if self.error is not None:
            raise self.error
        return self.payload


def test_rules_follow_execution_order():
    items = build_with_rules(FULL_FIELDS)
    texts = [i.text for i in items]
    assert texts[0] == "确认申报项目：雄安新区科技创新专项"
    assert texts[1] == "核对申报资格：企业、高校、科研院所"
    assert texts[2:4] == ["准备材料：项目申报书", "准备材料：预算表"]
    assert "在 2025-12-01 前完成提交" in texts
    # 流程建议必须显式标记，不能冒充原文事实
    assert [i.basis for i in items if i.basis == "suggested"] == ["suggested", "suggested"]
    assert len(items) <= MAX_ITEMS
    # 没有传 verified 时，规则项一律视为未验证，不能冒充已核验事实
    assert all(i.verified is False for i in items if i.basis == "excerpt")


def test_rules_skip_missing_fields():
    items = build_with_rules({"deadline": "2025-12-01"})
    texts = [i.text for i in items]
    assert texts[0] == "在 2025-12-01 前完成提交"
    assert not any("准备材料" in t for t in texts)


def test_rules_carry_verified_flag_and_drop_unlocatable_source():
    items = build_with_rules(
        {"deadline": "2025-12-01", "deadline_raw": "2025年12月1日9:00"},
        body=BODY,
        raw_texts={"deadline": "2025年12月1日9:00"},
        verified={"deadline": False},
    )
    deadline_item = next(i for i in items if i.text.startswith("在 2025-12-01"))
    assert deadline_item.verified is False
    assert deadline_item.source_text in BODY
    dropped = build_with_rules(
        {"deadline": "2025-12-01", "deadline_raw": "2019年1月1日至2019年1月2日"},
        body=BODY,
        raw_texts={"deadline": "2019年1月1日至2019年1月2日"},
    )
    assert not any(i.text.startswith("在 2025-12-01") for i in dropped)


def test_rules_flatten_list_values():
    items = build_with_rules({"eligible_applicants": ["企业", "高校"]})
    assert any(i.text == "核对申报资格：企业、高校" for i in items)


def test_max_items_keeps_deadline_and_suggestions():
    fields = {"materials": [f"材料{i}" for i in range(30)], "deadline": "2025-12-01"}
    items = build_with_rules(fields)
    assert len(items) == MAX_ITEMS
    assert any(i.text.startswith("在 2025-12-01") for i in items)
    assert sum(1 for i in items if i.basis == "suggested") == 2


def test_llm_drops_unlocatable_excerpt():
    payload = {
        "items": [
            {"text": "在 2025-12-01 前提交", "basis": "excerpt", "source_text": "2025年12月1日9:00"},
            {"text": "准备 10 份纸质材料", "basis": "excerpt", "source_text": "需提交纸质材料 10 份"},
            {"text": "提交后保存回执", "basis": "suggested", "source_text": ""},
        ]
    }
    items = parse_llm_items(payload, BODY)
    assert [i.text for i in items] == ["在 2025-12-01 前提交", "提交后保存回执"]
    assert items[1].basis == "suggested"


def test_llm_drops_fabricated_fact_behind_real_snippet():
    # source_text 真实存在，但 text 里的金额/日期无法由它背书，必须丢弃
    payload = {
        "items": [
            {"text": "截止时间已延长至 2026-12-31", "basis": "excerpt", "source_text": "请通过国科管系统提交项目申报书"},
            {"text": "必须缴纳 2000 元评审费", "basis": "excerpt", "source_text": "请通过国科管系统提交项目申报书"},
            {"text": "提交后保存回执", "basis": "suggested", "source_text": ""},
            {"text": "预留 3 个工作日审核", "basis": "suggested", "source_text": ""},
        ]
    }
    items = parse_llm_items(payload, BODY)
    assert [i.text for i in items] == ["提交后保存回执"]


def test_llm_drops_too_short_source():
    payload = {"items": [{"text": "通过国科管系统提交", "basis": "excerpt", "source_text": "申报"}]}
    assert parse_llm_items(payload, BODY) == []


def test_llm_rejects_date_that_is_numeric_subsequence_of_another_date():
    body = "申报截止时间为2026年4月20日，请通过国科管系统提交项目申报书。"
    payload = {
        "items": [
            {
                "text": "截止时间 2026-04-02",
                "basis": "excerpt",
                "source_text": "申报截止时间为2026年4月20日",
            }
        ]
    }
    # 旧实现按数字子串匹配，04-02 的三个数字都能在 2026-04-20 里找到而误通过。
    assert parse_llm_items(payload, body) == []


def test_llm_accepts_same_date_in_different_separator_style():
    body = "申报截止时间为2026年4月20日，请通过国科管系统提交项目申报书。"
    payload = {
        "items": [
            {
                "text": "截止时间 2026-04-20",
                "basis": "excerpt",
                "source_text": "申报截止时间为2026年4月20日",
            }
        ]
    }
    assert len(parse_llm_items(payload, body)) == 1


def test_llm_checklist_engine():
    payload = {
        "items": [
            {"text": "通过国科管系统提交", "basis": "excerpt", "source_text": "请通过国科管系统提交项目申报书"}
        ]
    }
    result = build_checklist(FakeLLM(payload=payload), "标题", BODY, FULL_FIELDS)
    assert result.engine == "llm"
    assert result.items[0].text == "通过国科管系统提交"
    assert result.items[0].verified is True


def test_llm_failure_falls_back_to_rules():
    result = build_checklist(FakeLLM(error=LLMError("boom")), "标题", BODY, FULL_FIELDS)
    assert result.engine == "rules"
    assert any(i.text.startswith("准备材料") for i in result.items)


def test_llm_empty_items_falls_back_to_rules():
    result = build_checklist(FakeLLM(payload={"items": []}), "标题", BODY, FULL_FIELDS)
    assert result.engine == "rules"


@pytest.fixture()
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NEWS_SOURCE_MODE", "demo")
    monkeypatch.setenv("LLM_API_KEY", "")
    return load_settings()


def test_pipeline_persists_checklist_only_for_applications(settings):
    analyze_batch(settings, limit=4)
    conn = db.connect(settings.db_path)
    try:
        checked = db.get_checklist(conn, "a1f0c3e2b7d94a51")
        assert checked is not None
        assert checked["engine"] == "rules"
        # 规则引擎只抽 deadline/funding/contact，清单随之只含这几项 + 流程建议
        texts = [i["text"] for i in checked["items"]]
        assert any("2026-04-20" in t for t in texts)
        assert any(t.startswith("核对资助额度") for t in texts)
        assert any(i["basis"] == "suggested" for i in checked["items"])
        # 非申报文章不应有清单
        assert db.get_checklist(conn, "c3a2e5f4d9b06c73") is None
    finally:
        conn.close()


def test_pipeline_regenerates_missing_checklist(settings):
    analyze_batch(settings, limit=4)
    conn = db.connect(settings.db_path)
    try:
        db.delete_checklist(conn, "a1f0c3e2b7d94a51")
        assert db.get_checklist(conn, "a1f0c3e2b7d94a51") is None
    finally:
        conn.close()
    # 第二次运行会 skip 已分析文章，但缺失的清单应被补生成
    result = analyze_batch(settings, limit=4)
    assert result["skipped"] == 4
    conn = db.connect(settings.db_path)
    try:
        assert db.get_checklist(conn, "a1f0c3e2b7d94a51") is not None
    finally:
        conn.close()


def test_checklist_json_corrupt_returns_empty(settings):
    analyze_batch(settings, limit=4)
    conn = db.connect(settings.db_path)
    try:
        conn.execute("UPDATE article_checklists SET items_json = 'not-json' WHERE article_id = 'a1f0c3e2b7d94a51'")
        conn.commit()
        checked = db.get_checklist(conn, "a1f0c3e2b7d94a51")
        assert checked["items"] == []
    finally:
        conn.close()
