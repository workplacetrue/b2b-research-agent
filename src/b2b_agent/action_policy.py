"""Cheap deterministic action review and transactional, currency-aware spending caps.

An allowed assessment permits preparation only; it never constitutes message approval.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field


def as_dict(value: Any) -> dict:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return vars(value)


def _date(value: Any) -> datetime | None:
    if not value:
        return None
    result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Action dates require an explicit timezone")
    return result


class CostEstimate(BaseModel):
    known: bool = False
    lower: float | None = None
    upper: float | None = None
    currency: str | None = None
    source: str | None = None
    tokens: int | None = None
    tool_seconds: float | None = None
    owner_minutes: float | None = None


class ActionAssessment(BaseModel):
    action_id: str = ""
    decision: Literal["prepare", "propose_message", "cheaper_alternative", "defer", "human_handoff", "stop"]
    allowed: bool
    objective: str = ""
    expected_progress: str = ""
    reasons: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    context_version: str | int | None = None
    stats_version: str | int | None = None
    catalog_version: str | None = None
    alternatives: list[str] = Field(default_factory=lambda: ["Use existing verified context", "Wait for a new event", "Take no action"])
    incremental_cost: CostEstimate = Field(default_factory=CostEstimate)
    incurred_costs: dict[str, float] = Field(default_factory=dict)
    remaining_budget: dict[str, float | None] = Field(default_factory=dict)
    contact_load: str = "unknown"
    reputational_risk: str = "unknown"
    review_at: str | None = None
    review_condition: str = "New context, catalog, statistics, budget, or user decision"
    approval_required: bool = False
    assessment_version: str = "action-policy-v2.1"


def cost_policy_version(config) -> str:
    """Bind approval to actual spending, contact, capacity and opt-out settings."""
    data = as_dict(config)
    material = {name: data.get(name, {}) for name in ("budget", "policy", "capacity", "unsubscribe")}
    # A BudgetConfig alone is also accepted for preparatory, non-contact actions.
    if not any(material.values()):
        material = {"budget": data}
    return hashlib.sha256(json.dumps(material, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def assessment_signature(assessment) -> str:
    """Digest the reasons and incremental exposure the operator actually reviewed.

    The timestamp of a local check and audit prose formatting do not affect it.
    Source context/statistics/catalog and risk/cost changes do.
    """
    data = as_dict(assessment)
    fields = ("decision", "allowed", "objective", "expected_progress", "reasons", "evidence_refs", "context_version", "stats_version", "catalog_version", "incremental_cost", "remaining_budget", "contact_load", "reputational_risk", "review_at", "review_condition", "approval_required", "assessment_version")
    return hashlib.sha256(json.dumps({k: data.get(k) for k in fields}, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def evaluate_action(action, capsule=None, stats=None, cost_policy=None, now=None, store=None) -> ActionAssessment:
    """Evaluate current data locally; unchanged ticks never need another model call."""
    action, capsule, stats, policy = map(as_dict, (action, capsule, stats, cost_policy))
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("now requires a timezone")
    kind = str(action.get("kind", action.get("type", "prepare")))
    outgoing = kind in {"initial", "reply", "followup", "follow_up", "send", "send_message", "propose_meeting", "calendar_invite", "manual_channel", "answer_question", "send_information", "ask_one_question", "request_referral"}
    upper = action.get("cost_upper_bound", action.get("monetary_upper"))
    verified_free = bool(action.get("verified_free", False))
    tariff = action.get("tariff_source")
    prices = policy.get("tool_prices", {})
    rate = prices.get(action.get("tool", kind), {})
    if upper is None and isinstance(rate, dict):
        if rate.get("per_call") is not None:
            upper = float(rate["per_call"]) * int(action.get("calls", 1))
            tariff = rate.get("source", "configured tool_prices")
        elif rate.get("input_per_million") is not None and rate.get("output_per_million") is not None and action.get("input_tokens") is not None and action.get("output_tokens") is not None:
            upper = (float(rate["input_per_million"]) * action["input_tokens"] + float(rate["output_per_million"]) * action["output_tokens"]) / 1_000_000
            tariff = rate.get("source", "configured token tariff")
    currency = action.get("currency", rate.get("currency", policy.get("currency"))) if isinstance(rate, dict) else action.get("currency", policy.get("currency"))
    if upper is not None and (not isinstance(upper, (int, float)) or not math.isfinite(upper) or upper < 0):
        raise ValueError("Cost upper bound must be finite and nonnegative")
    cost = CostEstimate(known=verified_free or (upper is not None and bool(currency) and bool(tariff)), lower=0 if verified_free else action.get("cost_lower_bound"), upper=0 if verified_free else upper, currency=None if verified_free else currency, source="verified free local operation" if verified_free else tariff, tokens=action.get("tokens", action.get("input_tokens")), tool_seconds=action.get("tool_seconds"), owner_minutes=action.get("owner_minutes"))
    result = ActionAssessment(action_id=str(action.get("id", "")), decision="propose_message" if outgoing else "prepare", allowed=True, objective=action.get("objective", action.get("goal", kind)), expected_progress=action.get("expected_progress", "Resolve one documented uncertainty"), evidence_refs=action.get("evidence_refs", []), context_version=capsule.get("context_version", action.get("context_version")), stats_version=stats.get("stats_version", action.get("stats_version")), catalog_version=action.get("catalog_version"), incremental_cost=cost, incurred_costs=stats.get("costs_by_currency", {}), remaining_budget=action.get("remaining_budget", {}), contact_load=action.get("contact_load", "unknown"), reputational_risk=action.get("reputational_risk", "unknown"), approval_required=outgoing)

    def block(decision, reason, review_at=None):
        result.decision, result.allowed = decision, False
        result.reasons.append(reason)
        result.review_at = review_at

    # Safety signals come from structured state, never instructions in source text.
    if action.get("process_optout"):
        result.decision, result.allowed, result.approval_required = "prepare", True, False
        result.reasons = ["Apply opt-out immediately without research or outgoing confirmation"]
        result.incremental_cost = CostEstimate(known=True, lower=0, upper=0, source="verified free local operation")
    elif capsule.get("is_terminal") or stats.get("is_terminal"):
        block("stop", "The current attempt is terminal; preserve new events without planned outreach")
    elif capsule.get("suppressed") or action.get("suppressed") or "suppression" in capsule.get("holds", []):
        block("stop", "An active suppression takes priority over commercial interest")
    elif capsule.get("paused") or action.get("paused") or capsule.get("human_takeover") or "human_handoff" in capsule.get("holds", []):
        block("human_handoff", "Persistent pause or human takeover is active")
    elif capsule.get("hold_reason") or "context_budget" in capsule.get("holds", []):
        block("human_handoff", "Critical context exceeds its budget or is incomplete; preserve all constraints for human review")
    elif action.get("context_version") is not None and capsule.get("context_version") is not None and str(action["context_version"]) != str(capsule["context_version"]):
        block("human_handoff", "Context changed; reevaluate the action before approval")
    elif action.get("stats_version") is not None and stats.get("stats_version") is not None and str(action["stats_version"]) != str(stats["stats_version"]):
        block("human_handoff", "Statistics changed; reevaluate the action before approval")
    elif capsule.get("next_review_at") and _date(capsule["next_review_at"]) > now:
        block("defer", "Wait until the agreed review event or date", str(capsule["next_review_at"]))
    elif action.get("known_answer") or action.get("cache_available"):
        block("cheaper_alternative", "Use the already known answer or cached source instead of repeating paid research")
    elif action.get("usefulness") in {"low", "none"}:
        block("defer", "Expected progress is insufficient; wait for a meaningful event")
    elif action.get("reputational_risk") == "high" or stats.get("channel_hold"):
        block("human_handoff", "Review adverse channel or recipient signals before further outreach")
    elif action.get("paid") and not cost.known:
        block("human_handoff", "Paid tool price is unknown; configure a sourced upper bound or choose a verified free alternative")
    elif action.get("paid") and not action.get("budget_reserved"):
        block("human_handoff", "Reserve the upper estimate atomically before making the paid call")
    elif action.get("budget_exhausted"):
        block("defer", "Budget or call/token limit reached; progress is preserved")
    elif action.get("is_new_opportunity") and action.get("capacity_full"):
        block("defer", "Owner capacity is full; continue handling existing replies and opt-outs")
    elif action.get("promises_start_date") and action.get("availability", "unknown") != "available":
        block("human_handoff", "Team availability does not support a start-date commitment")
    elif action.get("new_commercial_commitment"):
        block("human_handoff", "New commercial terms require an explicit owner decision")
    elif outgoing and action.get("contact_permission", "unknown") != "allowed":
        block("human_handoff", "Contact permission is not established for this channel and purpose")
    elif outgoing and action.get("requires_fresh_sync") and not action.get("sync_fresh"):
        block("defer", "Inbox or unsubscribe synchronization is stale; send remains on hold")
    elif outgoing and action.get("contact_window"):
        window = action["contact_window"]
        if not window.get("timezone"):
            block("human_handoff", "Contact window requires an explicit IANA timezone")
        else:
            local = now.astimezone(ZoneInfo(window["timezone"]))
            start, end = window.get("start", "00:00"), window.get("end", "23:59")
            clock = local.strftime("%H:%M")
            within = start <= clock <= end if start <= end else clock >= start or clock <= end
            if not within or local.weekday() not in window.get("weekdays", list(range(7))):
                block("defer", "Outside configured contact window; approval and state must be rechecked when it opens")
    if not result.reasons:
        result.reasons = ["Current evidence supports one bounded next step; historical spending is not a reason to continue"]
    if store is not None and hasattr(store, "record_event"):
        signature = hashlib.sha256(json.dumps({"action": action, "capsule": capsule, "stats": stats, "policy": policy, "decision": result.decision}, sort_keys=True, default=str).encode()).hexdigest()
        store.record_event("action_assessed", str(action.get("conversation_id", action.get("account_id", ""))), result.model_dump(mode="json"), event_id="assessment:" + signature)
    return result


class BudgetExceeded(ValueError):
    pass


class BudgetLedger:
    """SQLite write reservations prevent parallel workers overspending scoped caps."""

    def __init__(self, connection: sqlite3.Connection):
        self.db = connection
        self.db.execute("""CREATE TABLE IF NOT EXISTS action_cost_reservations (
            id TEXT PRIMARY KEY,currency TEXT NOT NULL,amount TEXT NOT NULL,
            reserved_amount TEXT NOT NULL,status TEXT NOT NULL,scopes TEXT NOT NULL,
            calls INTEGER NOT NULL,tokens INTEGER NOT NULL,created_at TEXT NOT NULL)""")
        self.db.commit()

    def reserve(self, reservation_id: str, amount: float | Decimal | None, currency: str, scopes: list[str], limits: dict[str, dict], calls: int = 1, tokens: int = 0) -> dict:
        if amount is None:
            raise BudgetExceeded("Unknown paid cost requires a configured upper bound")
        value = Decimal(str(amount))
        if not value.is_finite() or value < 0 or calls < 0 or tokens < 0:
            raise ValueError("Budget values must be finite and nonnegative")
        if not currency or len(currency) != 3 or not currency.isalpha():
            raise ValueError("Use an explicit three-letter currency")
        currency = currency.upper()
        scopes = sorted(set(scopes))
        if not scopes:
            raise ValueError("At least one budget scope is required")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            existing = self.db.execute("SELECT currency,reserved_amount,scopes,calls,tokens,status FROM action_cost_reservations WHERE id=?", (reservation_id,)).fetchone()
            if existing:
                if tuple(existing[:5]) != (currency, str(value), json.dumps(scopes), calls, tokens):
                    raise BudgetExceeded("Reservation ID already belongs to a different operation")
                self.db.commit()
                return {"id": reservation_id, "status": existing[5], "reused": True, "amount": float(value), "currency": currency}
            rows = self.db.execute("SELECT currency,amount,scopes,calls,tokens FROM action_cost_reservations WHERE status!='released'").fetchall()
            for scope in scopes:
                scoped = [row for row in rows if scope in json.loads(row[2])]
                cap = limits.get(scope, {})
                if any(row[0] != currency for row in scoped):
                    raise BudgetExceeded("Currencies cannot be combined without an explicit dated exchange rate")
                spent = sum((Decimal(row[1]) for row in scoped), Decimal(0))
                max_amount = cap.get("amount", cap.get("max_cost"))
                if max_amount is not None and spent + value > Decimal(str(max_amount)):
                    raise BudgetExceeded(f"Monetary budget exceeded for {scope}")
                if cap.get("max_calls") is not None and sum(row[3] for row in scoped) + calls > cap["max_calls"]:
                    raise BudgetExceeded(f"Call budget exceeded for {scope}")
                if cap.get("max_tokens") is not None and sum(row[4] for row in scoped) + tokens > cap["max_tokens"]:
                    raise BudgetExceeded(f"Token budget exceeded for {scope}")
            self.db.execute("INSERT INTO action_cost_reservations VALUES (?,?,?,?,?,?,?,?,?)", (reservation_id, currency, str(value), str(value), "reserved", json.dumps(scopes), calls, tokens, datetime.now(UTC).isoformat()))
            self.db.commit()
            return {"id": reservation_id, "status": "reserved", "reused": False, "amount": float(value), "currency": currency}
        except Exception:
            self.db.rollback()
            raise

    def reconcile(self, reservation_id: str, actual_amount: float | Decimal | None = None, billing_known: bool = False) -> dict:
        """Unknown acceptance/billing retains the upper bound, including after crashes."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute("SELECT amount,currency,status FROM action_cost_reservations WHERE id=?", (reservation_id,)).fetchone()
            if not row:
                raise ValueError("Unknown budget reservation")
            if row[2] != "reserved":
                self.db.commit()
                return {"id": reservation_id, "amount": float(row[0]), "currency": row[1], "status": row[2]}
            amount = Decimal(str(actual_amount)) if billing_known and actual_amount is not None else Decimal(row[0])
            if not amount.is_finite() or amount < 0:
                raise ValueError("Actual cost must be finite and nonnegative")
            status = "settled" if billing_known and actual_amount is not None else "uncertain"
            self.db.execute("UPDATE action_cost_reservations SET amount=?,status=? WHERE id=?", (str(amount), status, reservation_id))
            self.db.commit()
            return {"id": reservation_id, "amount": float(amount), "currency": row[1], "status": status}
        except Exception:
            self.db.rollback()
            raise

    def totals(self, scope: str) -> dict[str, float]:
        totals: dict[str, Decimal] = {}
        for currency, amount, scopes in self.db.execute("SELECT currency,amount,scopes FROM action_cost_reservations WHERE status!='released'"):
            if scope in json.loads(scopes):
                totals[currency] = totals.get(currency, Decimal(0)) + Decimal(amount)
        return {currency: float(value) for currency, value in totals.items()}
