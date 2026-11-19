"""五类关键信息 schema 的类别路由与规则降级。

核心约束：一篇新闻只能命中它所属大类的 schema，不能因为标签白名单重叠
（例如人才/资质类也允许 project_application 标签）而套用项目申报模板。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from app.extraction import ExtractSchema, TagHit, extract_with_rules, load_schemas, trigger_schemas

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "app" / "vocab" / "schemas"


def _tag(tag_id: str = "project_application") -> TagHit:
    return TagHit(tag_id=tag_id, confidence=0.9, evidence="测试", engine="rules")


def _schema(schema_id: str, category: str, fields: tuple[dict, ...] | None = None) -> ExtractSchema:
    return ExtractSchema(
        schema_id=schema_id,
        name=schema_id,
        trigger_tags=("project_application",),
        fields=fields or (),
        category=category,
    )


def test_every_category_has_exactly_one_schema() -> None:
    schemas = load_schemas(SCHEMA_DIR)
    by_category = {schema.category: schema.schema_id for schema in schemas}
    # dict 会掩盖重复 category：必须显式保证 schema 数与类别数一致
    assert len(schemas) == 5
    assert len(by_category) == len(schemas)
    assert by_category == {
        "project_application": "project_application",
        "industry_policy": "industry_policy",
        "talent_application": "talent_application",
        "enterprise_certification": "enterprise_certification",
        "funding_subsidy": "funding_subsidy",
    }
    assert all(schema.fields for schema in schemas)
    assert all(schema.category for schema in schemas)


def test_category_routing_does_not_use_shared_tag() -> None:
    schemas = [
        _schema("project_application", "project_application", ({"name": "deadline"},)),
        _schema("industry_policy", "industry_policy", ({"name": "policy_name"},)),
    ]
    hits = [_tag("project_application")]
    assert [s.schema_id for s in trigger_schemas(schemas, hits, "industry_policy")] == [
        "industry_policy"
    ]
    assert [s.schema_id for s in trigger_schemas(schemas, hits, "talent_application")] == []


def test_legacy_tag_routing_still_works_without_category() -> None:
    schemas = [
        _schema("project_application", "project_application", ({"name": "deadline"},)),
        _schema("industry_policy", "industry_policy", ({"name": "policy_name"},)),
    ]
    assert [s.schema_id for s in trigger_schemas(schemas, [_tag()])] == [
        "project_application",
        "industry_policy",
    ]


def test_industry_policy_rules_do_not_invent_deadline() -> None:
    schema = _schema(
        "industry_policy",
        "industry_policy",
        ({"name": "policy_name"}, {"name": "key_measures"}),
    )
    result = extract_with_rules(
        schema,
        "本政策自2026年4月20日起施行，有效期三年。",
        dt.date(2026, 3, 5),
    )
    assert result.fields == []


def test_talent_rules_extract_deadline_amount_and_contact() -> None:
    schema = _schema(
        "talent_application",
        "talent_application",
        (
            {"name": "deadline"},
            {"name": "deadline_raw"},
            {"name": "funding_amount"},
            {"name": "contact"},
        ),
    )
    result = extract_with_rules(
        schema,
        "申报截止时间为2026年4月20日，资助经费不超过500万元，联系人：王工 电话：0571-88886666。",
        dt.date(2026, 3, 5),
    )
    values = {f.name: f.value for f in result.fields}
    assert values["deadline"] == "2026-04-20"
    assert values["funding_amount"] == "500万元"
    assert "88886666" in values["contact"]
