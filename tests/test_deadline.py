import datetime as dt

from app.extraction import normalize_deadline

BASE = dt.date(2026, 3, 5)


def test_absolute_date():
    assert normalize_deadline("申报截止时间为2026年4月20日，逾期不再受理", BASE) == "2026-04-20"


def test_relative_from_publish_date_uses_article_base():
    # 「自发布之日起30日内」必须以文章发布时间为基准，而不是抓取时间
    assert normalize_deadline("自发布之日起30日内提交材料", BASE) == "2026-04-04"


def test_relative_without_base_is_not_guessed():
    assert normalize_deadline("自发布之日起30日内提交材料", None) is None


def test_month_day_rolls_over_to_next_year():
    assert normalize_deadline("截止1月10日", dt.date(2026, 12, 20)) == "2027-01-10"


def test_month_end():
    assert normalize_deadline("请于本月底前报送", BASE) == "2026-03-31"


def test_unknown_text_returns_none():
    assert normalize_deadline("另行通知", BASE) is None
