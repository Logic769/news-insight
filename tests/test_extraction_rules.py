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

    body = "截止时间：2025年11月25日17:00至2025年12月1日9:00，逾期不予受理。"
    result = extract_with_llm(
        FakeLLM(), _schema(), "申报通知", body, base=dt.date(2025, 11, 1), body_chars=1000
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

    # body 必须包含 raw_text，否则 _locate 失败、verified 恒为 False，
    # 下面的「not date_mismatch」断言就永远为真，测不出降级逻辑被删掉。
    body = "请按通知填报：项目网上填报起始时间为2016年9月22日9:00，截止时间为2016年10月17日16:30，逾期不予受理。"
    result = extract_with_llm(
        FakeLLM(), _schema(), "申报通知", body, base=dt.date(2016, 9, 1), body_chars=1000
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


def _policy_schema() -> ExtractSchema:
    return ExtractSchema(
        schema_id="industry_policy",
        name="行业政策",
        trigger_tags=(),
        fields=(
            {"name": "effective_date", "type": "date", "description": "施行日期"},
            {"name": "policy_name", "type": "string", "description": "政策名称"},
        ),
        category="industry_policy",
    )


def _llm_answering(name: str, value: object, raw_text: str, confidence: float = 0.9):
    """只吐单个字段的 stub LLM，本文件的日期用例共用。"""

    class _FakeLLM:
        def chat_json(self, system, user, **kwargs):
            return {
                "fields": {
                    name: {
                        "value": value,
                        "raw_text": raw_text,
                        "confidence": confidence,
                    }
                }
            }

    return _FakeLLM()


def _policy_result(
    value: str, raw: str, base: dt.date = dt.date(2026, 9, 1)
):
    """按行业政策 schema 跑一次 LLM 抽取，正文里一定包含 raw。"""
    body = "本办法" + raw + "，有效期三年。"
    return extract_with_llm(
        _llm_answering("effective_date", value, raw),
        _policy_schema(),
        "关于印发管理办法的通知",
        body,
        base=base,
        body_chars=1000,
    )


def _policy_field(value: str, raw: str) -> object:
    return {f.name: f for f in _policy_result(value, raw).fields}["effective_date"]


def test_effective_date_is_not_rolled_to_next_year():
    """9 月发布、「自1月1日起施行」指当年 1 月 1 日，不能被截止日期的跨年启发挪到次年。"""
    field = _policy_field("2026-01-01", "自1月1日起施行")
    assert field.value == "2026-01-01"
    # 原文确实写了这句话，代码也没有改写模型给出的日期
    assert field.confidence == 0.9
    assert field.verified is True


def test_effective_date_accepts_next_year_start():
    """次年 1 月 1 日施行是合理的：落在窗口内就采信模型日期，不推进也不降级。"""
    field = _policy_field("2027-01-01", "自1月1日起施行")
    assert field.value == "2027-01-01"
    assert field.confidence == 0.9
    assert field.verified is True


def test_effective_date_rejects_far_off_model_year():
    """模型编错年份（离发布年超过一年）不能包装成高置信事实，退回按原文重算并降级。"""
    field = _policy_field("2019-01-01", "自1月1日起施行")
    assert field.value == "2026-01-01"
    assert field.confidence <= 0.4
    assert field.verified is False


def test_effective_date_rejects_impossible_calendar_date():
    """2026-02-30 不是真实日期：不能因为「原文没写年份」就照单全收。"""
    field = _policy_field("2026-02-30", "自1月1日起施行")
    assert field.value == "2026-01-01"
    assert field.confidence <= 0.4
    assert field.verified is False


def test_deadline_still_rolls_over_to_next_year():
    """截止日期的既有跨年启发必须保留，生效日期则不套用。"""
    assert normalize_deadline("截止1月15日", dt.date(2025, 12, 31)) == "2026-01-15"
    assert normalize_deadline("自1月1日起施行", dt.date(2026, 9, 1), allow_past=True) == "2026-01-01"


def test_deadline_llm_still_rolls_over_without_explicit_year():
    """原文只写「截止1月10日」时截止日仍须推进到次年，防止 allow_past 误伤 deadline。"""
    # 注意别写「截止1月10日」：_pick_range 会把这类带截止语义的单日期当成
    # 起止同一的区间，走右端分支、绕过 allow_past，就测不出误伤了。
    # 模型也给不出年份（非 ISO 值）时才真正走 normalize_deadline 的跨年分支：
    # 若给完整 ISO，allow_past 的「采信模型日期」分支会把它接住，测不出误伤。
    # 也别写「截止1月10日」：_pick_range 会把带截止语义的单日期当区间、走右端分支。
    raw = "1月10日"
    body = "请于" + raw + "前提交材料。"
    result = extract_with_llm(
        _llm_answering("deadline", "1月10日", raw),
        _schema(),
        "申报通知",
        body,
        base=dt.date(2026, 12, 20),
        body_chars=1000,
    )
    field = {f.name: f for f in result.fields}["deadline"]
    assert field.value == "2027-01-10"


def test_effective_date_with_explicit_year_follows_text():
    """原文写了年份就仍以原文为准：模型给错年份必须降级待人工确认。"""
    field = _policy_field("2026-01-01", "自2025年1月1日起施行")
    assert field.value == "2025-01-01"
    assert field.confidence <= 0.4
    assert field.verified is False


def test_effective_date_without_model_iso_uses_base_year():
    """模型没给完整日期时按基准年补齐，同样不做跨年推进。"""
    field = _policy_field("1月1日", "自1月1日起施行")
    assert field.value == "2026-01-01"


def test_effective_date_relative_phrase_uses_publish_base():
    """「自发布之日起30日内」没有月日可对齐，必须按发布时间重算，不能采信模型 ISO。"""
    field = _policy_field("2026-12-31", "自发布之日起30日内")
    assert field.value == "2026-10-01"
    assert field.confidence <= 0.4
    assert field.verified is False


def test_effective_date_rejects_month_day_mismatch():
    """模型给 2026-03-15 而原文只写「1月1日」：月日对不上，不能采信模型补齐的年份。"""
    field = _policy_field("2026-03-15", "自1月1日起施行")
    assert field.value == "2026-01-01"
    assert field.confidence <= 0.4
    assert field.verified is False


def _past_ok_schema() -> ExtractSchema:
    return ExtractSchema(
        schema_id="past_ok_probe",
        name="带 past_ok 的截止字段",
        trigger_tags=(),
        fields=(
            {
                "name": "deadline",
                "type": "date",
                "description": "截止日期",
                "past_ok": True,
            },
        ),
    )


def test_past_ok_deadline_single_date_keeps_past_year():
    """声明 past_ok 的 deadline 单日期不能走区间路径被推进到次年。"""
    raw = "截止1月10日"
    body = "请于" + raw + "前提交材料。"
    result = extract_with_llm(
        _llm_answering("deadline", "2026-01-10", raw),
        _past_ok_schema(),
        "申报通知",
        body,
        base=dt.date(2026, 12, 20),
        body_chars=1000,
    )
    field = {f.name: f for f in result.fields}["deadline"]
    assert field.value == "2026-01-10"
