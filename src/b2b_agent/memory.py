"""Small source-linked working context; critical fields are held, never truncated."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel, Field

from .action_policy import as_dict
from .company_stats import canonical_account, get_company_stats


class ContextBudgetExceeded(ValueError):
    pass


class CompanyCapsule(BaseModel):
    account_id: str
    conversation_ids: list[str] = Field(default_factory=list)
    participants: list[Any] = Field(default_factory=list)
    stage: str = "unknown"
    is_terminal: bool = False
    goal: str | None = None
    confirmed_task: Any = None
    unknowns: list[Any] = Field(default_factory=list)
    latest_significant: list[dict] = Field(default_factory=list)
    holds: list[str] = Field(default_factory=list)
    suppressed: bool = False
    paused: bool = False
    human_takeover: bool = False
    allowed_channels: list[Any] = Field(default_factory=list)
    constraints: list[Any] = Field(default_factory=list)
    promises: list[Any] = Field(default_factory=list)
    expected_event: Any = None
    next_action: Any = None
    next_review_at: str | None = None
    context_version: str | int | None = None
    stats_version: str | None = None
    source_refs: list[str] = Field(default_factory=list)
    capsule_version: str = ""
    estimated_tokens: int = 0
    estimation_method: str = "conservative UTF-8 byte upper bound; one byte per token"
    hold_reason: str | None = None


def estimate_tokens(value: Any) -> int:
    """Explicit upper bound for byte-based tokenizers, including Cyrillic text.

    This intentionally overestimates normal model token use. It is not a
    character-to-token approximation. Protocol/system/tool overhead must be
    included in the value passed to the check.
    """
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    return len(text.encode("utf-8"))


def check_context_budget(payload: Any, budget: int = 4000, model_window: int = 16000, response_reserve: int = 1000, buffer: int = 256) -> int:
    if min(budget, model_window, response_reserve, buffer) < 0:
        raise ValueError("Context limits must be nonnegative")
    effective = min(budget, model_window - response_reserve - buffer)
    count = estimate_tokens(payload)
    if effective <= 0 or count > effective:
        raise ContextBudgetExceeded(f"Critical action context needs at most {count} tokens by conservative UTF-8 bound, exceeding {effective}; retrieve targeted sources or request human review")
    return count


def _unique(values: list) -> list:
    seen = set()
    result = []
    for value in values:
        signature = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        if signature not in seen:
            seen.add(signature)
            result.append(value)
    return result


def build_company_capsule(store, account_id: str, context_budget: int = 700) -> CompanyCapsule:
    account_id = str(account_id or "unknown")
    conversations = [c for c in store.list_conversations() if canonical_account(c) == account_id or c.get("id") == account_id]
    stats = get_company_stats(store, account_id)
    if not conversations:
        return CompanyCapsule(account_id=account_id, stats_version=stats.stats_version)
    current = max(conversations, key=lambda c: (str(c.get("updated_at", "")), str(c.get("context_version", ""))))
    ids = [str(c["id"]) for c in conversations]
    holds = _unique([h for c in conversations for h in c.get("holds", [])])
    capsule = CompanyCapsule(account_id=account_id, conversation_ids=ids, participants=_unique([p for c in conversations for p in c.get("participants", [])]), stage=current.get("stage", "unknown"), is_terminal=all(c.get("is_terminal", False) for c in conversations), goal=current.get("goal"), confirmed_task=current.get("confirmed_task", current.get("task")), unknowns=_unique([v for c in conversations for v in c.get("unknowns", [])]), holds=holds, suppressed="suppression" in holds or any(c.get("suppressed", False) for c in conversations), paused=any(c.get("paused", False) for c in conversations), human_takeover=any(c.get("human_takeover", False) for c in conversations), allowed_channels=_unique([v for c in conversations for v in c.get("allowed_channels", [])]), constraints=_unique([v for c in conversations for v in c.get("constraints", [])]), promises=_unique([v for c in conversations for v in c.get("promises", [])]), expected_event=current.get("expected_event"), next_action=current.get("next_action"), next_review_at=current.get("next_review_at"), context_version=current.get("context_version"), stats_version=stats.stats_version, source_refs=_unique([str(v) for c in conversations for v in c.get("source_refs", c.get("evidence_ids", []))]))
    capsule.suppressed = capsule.suppressed or bool(stats.suppression_scopes)
    if hasattr(store, "pause_reason"):
        capsule.paused = capsule.paused or any(bool(store.pause_reason({**c, "conversation_id": c["id"]})) for c in conversations)
    try:
        capsule.human_takeover = capsule.human_takeover or any(row[0] in ids for row in store.db.execute("SELECT target FROM workflow_pauses WHERE scope='human_takeover'"))
    except Exception as error:
        if "no such table" not in str(error):
            raise
    # Retrieve references to the last significant observations, not full archives.
    messages = [m for m in store.list_inbound() if str(m.get("conversation_id")) in ids]
    messages.sort(key=lambda m: str(m.get("received_at", m.get("created_at", ""))))
    for message in messages[-2:]:
        capsule.latest_significant.append({"ref": message.get("id", message.get("provider_message_id")), "labels": message.get("labels", []), "at": message.get("received_at"), "confirmed": message.get("confirmed", []), "constraints": message.get("constraints", [])})
    critical = capsule.model_dump(exclude={"estimated_tokens", "capsule_version", "estimation_method", "hold_reason"}, exclude_none=True)
    version = hashlib.sha256(json.dumps(critical, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()[:20]
    capsule.capsule_version = version
    capsule.estimated_tokens = estimate_tokens(critical)
    if capsule.estimated_tokens > context_budget:
        capsule.hold_reason = "Critical structured capsule exceeds the configured conservative token budget; use targeted retrieval or human handoff"
        # Keep critical fields for operator review. Do not silently cut constraints.
        if "context_budget" not in capsule.holds:
            capsule.holds.append("context_budget")
    return capsule


def build_action_context(capsule, stats, offer, new_text: str, system: str = "", tools=None, total_budget: int = 4000, model_window: int = 16000, response_reserve: int = 1000, buffer: int = 256) -> dict:
    capsule_data, stats_data, offer_data = map(as_dict, (capsule, stats, offer))
    if capsule_data.get("hold_reason") or "context_budget" in capsule_data.get("holds", []):
        raise ContextBudgetExceeded(capsule_data.get("hold_reason", "Capsule context requires manual review"))
    # Selected fields only: the full CompanyStats registry or catalog never enters.
    selected_stats = {k: stats_data.get(k) for k in ("stats_version", "stage", "is_terminal", "messages_sent", "human_replies", "auto_replies", "refusals", "optouts", "meetings_held", "costs_by_currency")}
    selected_offer = {k: offer_data[k] for k in ("id", "name", "supported_tasks", "unsupported_tasks", "limitations", "allowed_claims", "price", "pricing", "first_steps", "required_inputs") if k in offer_data}
    payload = {"system": system, "tools": tools or [], "capsule": capsule_data, "stats": selected_stats, "offer": selected_offer, "new_author_text": new_text}
    payload["context_estimate"] = {"upper_bound_tokens": 0, "method": "conservative UTF-8 byte upper bound", "includes_system_and_tools": True}
    # Include even our metadata if callers serialize the returned object verbatim.
    for _ in range(4):
        count = check_context_budget(payload, total_budget, model_window, response_reserve, buffer)
        if payload["context_estimate"]["upper_bound_tokens"] == count:
            break
        payload["context_estimate"]["upper_bound_tokens"] = count
    check_context_budget(payload, total_budget, model_window, response_reserve, buffer)
    return payload
