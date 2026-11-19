"""规则引擎与日期归一化的回归用例。

覆盖三个曾经真实出错的地方：
- 区间「3月1日至4月20日」被取成左端（开始日期当截止日期）；
- 中文两位数「三十日」解析失败且静默丢弃；
- 「2月29日」跨到非闰年时 replace 抛 ValueError。
"""

import datetime as dt

from app.extraction import ExtractSchema, extract_with_llm, extract_with_rules, normalize_deadline


def _schema() -> ExtractSchema:
    return ExtractSchema(
        schema_id="project_application",
        name="项目申报",
        trigger_tags=("project_application",),
        fields=(
            {"name": "deadline", "type": "date", "description": "截止日期"},
            {"name": "deadline_raw", "type": "string", "description": "原文表述"},
            {"name": "funding_amount", "type": "string", "description": "资助金额"},
            {"name": "contact", "type": "string", "description": "联系人及联系电话"},
        ),
    )


def test_date_range_takes_right_end():
    body = "申报截止时间：2026年3月1日至2026年4月20日，逾期不予受理。"
    result = extract_with_rules(_schema(), body, dt.date(2026, 3, 1))
    assert {f.name: f.value for f in result.fields}["deadline"] == "2026-04-20"

def test_deadline_range_with_times_takes_end_date():
    # 政府公告常见「截止时间：2025年11月25日17:00至2025年12月1日9:00」，
    # 起始日期带时刻时不能把起始日当截止日。
    body = "截止时间：2025年11月25日17:00至2025年12月1日9:00，逾期不予受理。"
    result = extract_with_rules(_schema(), body, dt.date(2025, 11, 1))
    assert {f.name: f.value for f in result.fields}["deadline"] == "2025-12-01"


def test_deadline_range_with_spaced_times_takes_end_date():
    body = "截止时间：2025年11月25日 17:00 至 2025年12月1日 9:00。"
    result = extract_with_rules(_schema(), body, dt.date(2025, 11, 1))
    assert {f.name: f.value for f in result.fields}["deadline"] == "2025-12-01"


def test_llm_deadline_range_with_times_takes_end_date():
    class FakeLLM:
        def chat_json(self, system, user, **kwargs):
            return {
                "fields": {
                    "deadline": {
                        "value": "2025-11-25",
                        "raw_text": "截止时间：2025年11月25日17:00至2025年12月1日9:00",
                        "confidence": 0.9,
                    }
                }
            }

    result = extract_with_llm(
        FakeLLM(), _schema(), "申报通知", "正文略", base=dt.date(2025, 11, 1), body_chars=1000
    )
    field = {f.name: f for f in result.fields}["deadline"]
    assert field.value == "2025-12-01"


def test_bare_start_date_is_not_a_deadline():
    # 「申报时间」不是截止语义词，宁可抽不到也不能把开始日期当截止日期
    body = "申报时间：2026年3月1日，受理时间为2026年3月1日。"
    result = extract_with_rules(_schema(), body, dt.date(2026, 3, 1))
    assert "deadline" not in {f.name for f in result.fields}


def test_chinese_two_digit_days():
    base = dt.date(2026, 3, 5)
    assert normalize_deadline("自发布之日起三十日内", base) == "2026-04-04"
    assert normalize_deadline("自发布之日起十五日内", base) == "2026-03-20"


def test_leap_day_does_not_raise():
    assert normalize_deadline("截止2月29日", dt.date(2028, 12, 1)) is None


def test_llm_start_date_with_time_is_replaced_by_deadline_end():
    # 原文同时有「起始时间」和「截止时间」时，LLM 可能把起始日期当 deadline；
    # 截止日期带 HH:MM 时也必须能从「截止时间」分支取到结束日，而不是取第一个日期。
    class FakeLLM:
        def chat_json(self, system, user, **kwargs):
            return {
                "fields": {
                    "deadline": {
                        "value": "2016-09-22",
                        "raw_text": "项目网上填报起始时间为2016年9月22日9:00，截止时间为2016年10月17日16:30",
                        "confidence": 0.8,
                    }
                }
            }

    result = extract_with_llm(
        FakeLLM(), _schema(), "申报通知", "正文略", base=dt.date(2016, 9, 1), body_chars=1000
    )
    field = {f.name: f for f in result.fields}["deadline"]
    assert field.value == "2016-10-17"
    assert field.confidence <= 0.5
    assert field.verified is False


def test_rules_engine_never_claims_verified():
    """正则只证明文本出现过，不证明语义正确，所以规则引擎一律 verified=False。"""
    body = "申报截止时间为2026年4月20日，资助经费不超过500万元，联系人：王工 电话：0571-88886666。"
    result = extract_with_rules(_schema(), body, dt.date(2026, 3, 5))
    assert result.fields
    assert all(f.verified is False for f in result.fields)
    values = {f.name: f.value for f in result.fields}
    assert values["deadline"] == "2026-04-20"
    assert "88886666" in values["contact"]
