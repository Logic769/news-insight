"""LLM 抽取的日期区间守卫。

政府公告的填报时间几乎总是「开始至结束」，而模型习惯把区间起点当成截止日
（实测科技部公告「网络填报时间：2025年11月25日17:00至2025年12月1日9:00」
模型返回 2025-11-25）。这里用桩模型固定复现该错误，确保代码层强制取右端。
"""

import datetime as dt

from app.extraction import ExtractSchema, extract_with_llm


class _StubLLM:
    """最小 chat_json 桩：直接返回预设的 fields。"""

    def __init__(self, fields: dict):
        self._fields = fields

    def chat_json(self, system: str, user: str) -> dict:
        return {"fields": self._fields}


def _schema() -> ExtractSchema:
    return ExtractSchema(
        schema_id="project_application",
        name="项目申报",
        trigger_tags=("project_application",),
        fields=(
            {"name": "deadline", "type": "date", "description": "截止日期"},
            {"name": "deadline_raw", "type": "string", "description": "原文表述"},
        ),
    )


def test_llm_range_start_is_replaced_by_right_end():
    raw = "网络填报时间：2025年11月25日17:00至2025年12月1日9:00"
    body = "请按通知填报。" + raw + "，逾期不予受理。"
    llm = _StubLLM(
        {"deadline": {"value": "2025-11-25", "raw_text": raw, "confidence": 0.9}}
    )
    result = extract_with_llm(
        llm,
        _schema(),
        title="关于填报储备项目正式申报书的通知",
        body=body,
        base=dt.date(2025, 11, 25),
        body_chars=6000,
    )
    values = {f.name: f for f in result.fields}
    assert values["deadline"].value == "2025-12-01"
    # 值被代码从模型给的起点改写成右端：verified 仍成立，但置信度封顶 0.85，
    # 避免下游把「正则推出来的值」当成模型 0.9 的高置信结论。
    assert values["deadline"].confidence == 0.85
    assert values["deadline"].verified is True


def test_llm_single_date_is_kept():
    raw = "申报截止时间为2026年4月20日"
    llm = _StubLLM(
        {"deadline": {"value": "2026-04-20", "raw_text": raw, "confidence": 0.9}}
    )
    result = extract_with_llm(
        llm,
        _schema(),
        title="申报通知",
        body=raw + "，逾期不予受理。",
        base=dt.date(2026, 3, 5),
        body_chars=6000,
    )
    values = {f.name: f for f in result.fields}
    assert values["deadline"].value == "2026-04-20"


def test_llm_range_right_end_without_year_prefix():
    raw = "网上填报时间：2023年9月12日08:00至10月12日16:00。"
    llm = _StubLLM(
        {"deadline": {"value": "2023-09-12", "raw_text": raw, "confidence": 0.9}}
    )
    result = extract_with_llm(
        llm,
        _schema(),
        title="申报通知",
        body=raw + "逾期不再受理。",
        base=dt.date(2023, 9, 12),
        body_chars=6000,
    )
    values = {f.name: f for f in result.fields}
    assert values["deadline"].value == "2023-10-12"


def test_llm_range_right_end_day_only():
    """「11月25日至30日」：右端只写「日」，必须借用左端的年月。"""
    raw = "申报时间：2025年11月25日至30日。"
    llm = _StubLLM(
        {"deadline": {"value": "2025-11-25", "raw_text": raw, "confidence": 0.9}}
    )
    result = extract_with_llm(
        llm,
        _schema(),
        title="申报通知",
        body=raw + "逾期不再受理。",
        base=dt.date(2025, 11, 20),
        body_chars=6000,
    )
    values = {f.name: f for f in result.fields}
    assert values["deadline"].value == "2025-11-30"


def test_llm_range_without_base_skips_day_only():
    """没有发布时间基准时，「11月25日至30日」无法确定年份：宁可不要字段。"""
    raw = "申报时间：11月25日至30日。"
    llm = _StubLLM(
        {"deadline": {"value": "2025-11-25", "raw_text": raw, "confidence": 0.9}}
    )
    result = extract_with_llm(
        llm,
        _schema(),
        title="申报通知",
        body=raw + "逾期不再受理。",
        base=None,
        body_chars=6000,
    )
    assert all(f.name != "deadline" for f in result.fields)


def test_llm_range_without_base_keeps_full_year_right_end():
    """右端带完整年份时不依赖 base，base=None 也要用右端。"""
    raw = "填报时间：2025年11月25日至2025年12月1日。"
    llm = _StubLLM(
        {"deadline": {"value": "2025-11-25", "raw_text": raw, "confidence": 0.9}}
    )
    result = extract_with_llm(
        llm,
        _schema(),
        title="申报通知",
        body=raw + "逾期不再受理。",
        base=None,
        body_chars=6000,
    )
    values = {f.name: f for f in result.fields}
    assert values["deadline"].value == "2025-12-01"


def test_llm_multiple_ranges_takes_last():
    """raw_text 同时含培训与申报两个区间：取最后一个（公告把申报写在最后）。"""
    raw = "培训时间 9月1日至9月5日，申报时间 11月25日至12月1日。"
    llm = _StubLLM(
        {"deadline": {"value": "2025-09-01", "raw_text": raw, "confidence": 0.9}}
    )
    result = extract_with_llm(
        llm,
        _schema(),
        title="申报通知",
        body=raw,
        base=dt.date(2025, 8, 20),
        body_chars=6000,
    )
    values = {f.name: f for f in result.fields}
    assert values["deadline"].value == "2025-12-01"


def test_llm_deadline_range_wins_over_earlier_range():
    """前缀带「截止」的区间优先于文本里更早的普通区间。"""
    raw = "培训时间 9月1日至9月5日，截止时间 11月25日至12月1日。"
    llm = _StubLLM(
        {"deadline": {"value": "2025-09-01", "raw_text": raw, "confidence": 0.9}}
    )
    result = extract_with_llm(
        llm,
        _schema(),
        title="申报通知",
        body=raw,
        base=dt.date(2025, 8, 20),
        body_chars=6000,
    )
    values = {f.name: f for f in result.fields}
    assert values["deadline"].value == "2025-12-01"


def test_range_regex_ignores_phone_and_clock():
    """电话与「8:00-17:00」不能被当成日期区间。"""
    from app.extraction import _pick_range

    assert _pick_range("联系人 0571-88886666") is None
    assert _pick_range("每天 8:00-17:00 开放") is None
