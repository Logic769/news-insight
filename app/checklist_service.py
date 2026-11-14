"""Shared adapter from analyzed-article rows to the checklist builder.

The pipeline and the on-demand HTTP endpoint must feed the builder the exact
same facts. Keep the mapping here so a change to the extraction payload cannot
silently make the two paths diverge.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.checklist import ChecklistResult, build_checklist, to_dicts
from app.llm import LLMClient
from app.settings import Settings

PROJECT_SCHEMA = "project_application"


@dataclass(frozen=True)
class ChecklistInput:
    """Everything the checklist builder needs, detached from SQLite objects."""

    article_id: str
    title: str
    body: str
    fields: dict[str, Any]
    raw_texts: dict[str, str]
    verified: dict[str, bool]


def field_payload(fields) -> tuple[dict[str, Any], dict[str, str], dict[str, bool]]:
    """Split FieldValue objects into value/raw_text/verified maps."""
    values = {f.name: f.value for f in fields}
    raw_texts = {f.name: f.raw_text for f in fields if f.raw_text}
    verified = {f.name: f.verified for f in fields}
    return values, raw_texts, verified


def checklist_input_from_detail(detail: dict[str, Any] | None) -> ChecklistInput | None:
    """Build the builder input from one ``db.get_article`` row.

    Returns ``None`` when the article is not a project-application item, has no
    extracted facts, or has no body. Callers must translate that into an
    explicit "not applicable" response rather than pretending a checklist
    exists.
    """
    if not detail:
        return None
    tags = {item.get("tag") for item in detail.get("tags") or [] if isinstance(item, dict)}
    if PROJECT_SCHEMA not in tags:
        return None
    body = str(detail.get("body") or "")
    block = (detail.get("extractions") or {}).get(PROJECT_SCHEMA) or {}
    if not block or not body.strip():
        return None
    fields = {name: info.get("value") for name, info in block.items() if isinstance(info, dict)}
    raw_texts = {
        name: info["raw_text"]
        for name, info in block.items()
        if isinstance(info, dict) and info.get("raw_text")
    }
    verified = {
        name: bool(info.get("verified"))
        for name, info in block.items()
        if isinstance(info, dict)
    }
    return ChecklistInput(
        article_id=str(detail.get("article_id") or ""),
        title=str(detail.get("title") or ""),
        body=body,
        fields=fields,
        raw_texts=raw_texts,
        verified=verified,
    )


def items_payload(result: ChecklistResult) -> list[dict[str, Any]]:
    """Render builder output in the storage/wire shape."""
    return to_dicts(result.items)


def build_for_input(
    settings: Settings,
    source: ChecklistInput,
    *,
    llm: LLMClient | None = None,
) -> ChecklistResult:
    """Generate one checklist, reusing a caller-owned LLM client when given."""
    client = llm or LLMClient(settings)
    try:
        return build_checklist(
            client,
            source.title,
            source.body,
            source.fields,
            raw_texts=source.raw_texts,
            verified=source.verified,
            body_chars=settings.extract_body_chars,
        )
    finally:
        if llm is None:
            client.close()
