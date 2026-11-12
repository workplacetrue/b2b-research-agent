"""Reproducible company views derived from durable messages and events."""

from __future__ import annotations

import csv
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class CompanyStats(BaseModel):
    account_id: str
    stats_version: str
    as_of: str
    last_processed_event_id: str | None = None
    stage: str = "unknown"
    is_terminal: bool = False
    closed_reason: str | None = None
    next_step_owner: str = "unknown"
    conversation_ids: list[str] = Field(default_factory=list)
    initial_messages: int = 0
    messages_sent: int = 0
    provider_accepted_messages: int = 0
    manual_confirmed_messages: int = 0
    send_attempts: int = 0
    delivery_errors: int = 0
    uncertain_messages: int = 0
    human_replies: int = 0
    auto_replies: int = 0
    operator_messages: int = 0
    refusals: int = 0
    optouts: int = 0
    hard_bounces: int = 0
    soft_bounces: int = 0
    substantive_interest: int = 0
    price_requests: int = 0
    case_requests: int = 0
    security_requests: int = 0
    timing_requests: int = 0
    integration_requests: int = 0
    meetings_proposed: int = 0
    meetings_scheduled: int = 0
    meetings_held: int = 0
    meetings_no_show: int = 0
    meetings_cancelled: int = 0
    complaints: int | None = None
    deliverability: str = "unknown"
    last_sent_at: str | None = None
    last_human_reply_at: str | None = None
    confirmed: list[Any] = Field(default_factory=list)
    unknowns: list[Any] = Field(default_factory=list)
    constraints: list[Any] = Field(default_factory=list)
    questions_asked: list[Any] = Field(default_factory=list)
    questions_answered: list[Any] = Field(default_factory=list)
    promises: list[Any] = Field(default_factory=list)
    next_action: Any = None
    next_review_at: str | None = None
    approval_state: str = "none"
    costs_by_currency: dict[str, float] = Field(default_factory=dict)
    suppression_scopes: list[dict] = Field(default_factory=list)
    observation_window: dict[str, str | None] = Field(default_factory=dict)
    response_rate: dict[str, Any] = Field(default_factory=dict)
    channel_hold: bool = False
    reputation_signals: list[dict] = Field(default_factory=list)


def canonical_account(record: dict) -> str:
    # Never identify a public-request customer by the marketplace's domain.
    return str(record.get("account_id") or record.get("company_id") or record.get("opportunity_id") or record.get("id") or "unknown")


def _rows(store, method: str, table: str) -> list[dict]:
    if hasattr(store, method):
        return [dict(value) for value in getattr(store, method)()]
    try:
        return [json.loads(row[0]) for row in store.db.execute(f"SELECT payload FROM {table}")]
    except Exception as error:
        if "no such table" in str(error):
            return []
        raise


def _labels(message: dict) -> set[str]:
    result = set()
    labels = message.get("labels", message.get("intents", []))
    if isinstance(labels, dict):
        labels = labels.get("labels", [])
    for label in labels:
        result.add(str(label.get("label", label.get("intent", ""))) if isinstance(label, dict) else str(label))
    return result


def _unique(values):
    seen = set()
    result = []
    for value in values:
        signature = json.dumps(value, sort_keys=True, default=str)
        if signature not in seen:
            result.append(value)
            seen.add(signature)
    return result


def _source_action(action: dict) -> dict:
    data = {k: v for k, v in action.items() if k not in {"approval_id", "approved_at", "updated_at", "last_checked_at", "assessment", "action_assessment"}}
    if data.get("status") in {"approved", "pending_approval", "approved_for_manual_send", "awaiting_manual_send"}:
        data["status"] = "prepared"
    return data


def get_company_stats(store, account_id: str, now=None) -> CompanyStats:
    account_id = str(account_id or "unknown")
    conversations = [c for c in _rows(store, "list_conversations", "workflow_conversations") if canonical_account(c) == account_id or c.get("id") == account_id]
    ids = {str(c["id"]) for c in conversations}
    opportunities = [o for o in _rows(store, "list_opportunities", "workflow_opportunities") if canonical_account(o) == account_id or o.get("id") == account_id]
    for o in opportunities:
        ids.update(str(c["id"]) for c in _rows(store, "list_conversations", "workflow_conversations") if c.get("opportunity_id") == o["id"])
    all_conversations = _rows(store, "list_conversations", "workflow_conversations")
    conversations = [c for c in all_conversations if str(c["id"]) in ids]
    actions = [a for a in _rows(store, "list_actions", "workflow_actions") if str(a.get("conversation_id")) in ids or a.get("account_id") == account_id]
    inbound = [m for m in _rows(store, "list_inbound", "workflow_inbound") if str(m.get("conversation_id")) in ids or m.get("account_id") == account_id]
    events = [e for e in _rows(store, "list_events", "workflow_events") if str(e.get("entity_id")) in ids | {account_id} or str(e.get("conversation_id", e.get("payload", {}).get("conversation_id"))) in ids]
    recipients = {str(c.get("recipient", "")).lower() for c in conversations}
    recipients.update(str(a.get("recipient", "")).lower() for a in actions)
    suppression_sources = []
    pause_sources = []
    budget_sources = []
    try:
        for row in store.db.execute("SELECT scope,target,channel,reason,created_at FROM workflow_suppressions"):
            item = dict(row)
            if item["scope"] == "account" and item["target"] == account_id.lower() or item["scope"] in {"contact", "channel"} and item["target"] in recipients or item["scope"] == "domain" and any(c.get("account_domain") == item["target"] for c in conversations):
                suppression_sources.append(item)
        for row in store.db.execute("SELECT scope,target,reason FROM workflow_pauses"):
            item = dict(row)
            if item["scope"] == "global" or item["scope"] == "account" and item["target"] == account_id or item["scope"] in {"conversation", "human_takeover"} and item["target"] in ids or item["scope"] == "campaign" and any(c.get("campaign_id") == item["target"] for c in conversations):
                pause_sources.append(item)
    except Exception as error:
        if "no such table" not in str(error):
            raise
    try:
        for row in store.db.execute("SELECT id,currency,amount,scopes,status FROM action_cost_reservations WHERE status!='released'"):
            if any(s in {account_id, "company:" + account_id, "account:" + account_id} or s in {"conversation:" + cid for cid in ids} for s in json.loads(row[3])):
                budget_sources.append(dict(row))
    except Exception as error:
        if "no such table" not in str(error):
            raise
    senders = {str(a.get("sender_account", "")).lower() for a in actions if a.get("sender_account")}
    signals = [e for e in _rows(store, "list_events", "workflow_events")
               if e.get("kind") in {"complaint", "channel_health", "reputation_signal"}
               and e.get("source") and (e.get("entity_id") in senders or str(e.get("sender_account", "")).lower() in senders)]
    events = _unique(events + signals)
    # Assessments are audit records of this view, not new source observations.
    source_events = [e for e in events if e.get("kind") not in {"action_assessed", "catalog_snapshot"}]
    # Draft/approval bookkeeping is shown in the view, but is not a newly
    # observed company outcome that invalidates its own approval card.
    prepared_statuses = {"pending_approval", "approved", "deferred", "approved_for_manual_send", "awaiting_manual_send"}
    source = {"conversations": conversations, "actions": [_source_action(a) for a in actions if a.get("status") not in prepared_statuses], "inbound": inbound, "events": source_events, "opportunities": opportunities, "suppressions": suppression_sources, "pauses": pause_sources, "costs": budget_sources}
    version = hashlib.sha256(json.dumps(source, sort_keys=True, default=str).encode()).hexdigest()[:20]
    stats = CompanyStats(account_id=account_id, stats_version=version, as_of=(now or datetime.now(UTC)).isoformat(), conversation_ids=sorted(ids))
    stats.suppression_scopes = suppression_sources
    stats.channel_hold = bool(suppression_sources)
    stats.reputation_signals = signals
    if signals:
        complaints = [e for e in signals if e.get("kind") == "complaint" or e.get("signal") == "complaint"]
        snapshots = [e for e in signals if isinstance(e.get("complaints"), int) and e["complaints"] >= 0]
        if complaints or snapshots:
            stats.complaints = max([len(complaints)] + [e["complaints"] for e in snapshots])
        latest_signal = max(signals, key=lambda e: str(e.get("created_at", "")))
        stats.deliverability = latest_signal.get("deliverability", "unknown")
        stats.channel_hold = stats.channel_hold or bool(complaints) or any(
            e.get("risk") == "high" or e.get("deliverability") in {"blocked", "restricted"} for e in signals)
    if conversations:
        current = max(conversations, key=lambda c: (str(c.get("updated_at", "")), str(c.get("context_version", ""))))
        stats.stage = current.get("stage", "unknown")
        stats.is_terminal = all(bool(c.get("is_terminal", False)) for c in conversations)
        stats.closed_reason = current.get("closed_reason")
        stats.next_action = current.get("next_action")
        stats.next_review_at = current.get("next_review_at")
        stats.next_step_owner = current.get("next_step_owner", "user" if any(a.get("status") == "pending_approval" for a in actions) else "unknown")
        for field in ("confirmed", "unknowns", "constraints", "questions_asked", "questions_answered", "promises"):
            setattr(stats, field, _unique([v for c in conversations for v in c.get(field, [])]))
    sent_statuses = {"sent", "sent_confirmed_by_user"}
    sent = [a for a in actions if a.get("status", a.get("delivery_status")) in sent_statuses]
    attempted = [a for a in actions if not a.get("rejection_actor") and a.get("status", a.get("delivery_status")) in sent_statuses | {"sending", "failed", "rejected", "uncertain", "bounced"}]
    stats.messages_sent = len(sent)
    stats.provider_accepted_messages = sum(a.get("status") == "sent" for a in sent)
    stats.manual_confirmed_messages = sum(a.get("status") == "sent_confirmed_by_user" for a in sent)
    stats.send_attempts = len(attempted)
    stats.initial_messages = sum(a.get("kind", "initial") in {"initial", "public_request", "cold"} for a in sent)
    stats.delivery_errors = sum(a.get("status") in {"failed", "rejected", "bounced"} and not a.get("rejection_actor") for a in actions)
    stats.uncertain_messages = sum(a.get("status") in {"uncertain", "sending"} for a in actions)
    dates = [a.get("sent_at", a.get("updated_at", a.get("created_at"))) for a in sent]
    stats.last_sent_at = max((d for d in dates if d), default=None)
    human = []
    answered_conversations = set()
    for message in inbound:
        labels = _labels(message)
        bounce = message.get("bounce")
        if message.get("is_operator") or "operator_message" in labels:
            stats.operator_messages += 1
        elif bounce in {"hard", "soft"} or labels & {"hard_bounce", "soft_bounce"}:
            stats.hard_bounces += int(bounce == "hard" or "hard_bounce" in labels)
            stats.soft_bounces += int(bounce == "soft" or "soft_bounce" in labels)
        elif message.get("is_auto_reply") or labels & {"auto_reply", "out_of_office", "ooo"}:
            stats.auto_replies += 1
        else:
            stats.human_replies += 1
            human.append(message)
            answered_conversations.add(str(message.get("conversation_id")))
        stats.refusals += int(bool(labels & {"not_interested", "refusal", "rejection"}))
        stats.optouts += int(bool(labels & {"optout", "opt_out", "unsubscribe", "do_not_contact", "account_optout"}))
        stats.substantive_interest += int(bool(labels & {"interest", "substantive_interest", "interested", "meeting_interest"}) and not labels & {"optout", "opt_out", "account_opt_out", "unsubscribe", "do_not_contact", "not_interested"})
        stats.price_requests += int(bool(labels & {"price", "price_question", "price_request"}))
        stats.case_requests += int(bool(labels & {"case_request", "proof_request", "portfolio"}))
        stats.security_requests += int(bool(labels & {"security", "security_question"}))
        stats.timing_requests += int(bool(labels & {"timing", "timeline", "timing_question", "timeline_question"}))
        stats.integration_requests += int(bool(labels & {"integration", "integration_question"}))
    stats.last_human_reply_at = max((str(m.get("received_at", m.get("created_at", ""))) for m in human), default=None)
    for metric, kinds in {"meetings_proposed": {"meeting_proposed"}, "meetings_scheduled": {"meeting_scheduled"}, "meetings_held": {"meeting_held"}, "meetings_no_show": {"meeting_no_show", "no_show"}, "meetings_cancelled": {"meeting_cancelled"}}.items():
        setattr(stats, metric, len({e.get("id") for e in source_events if e.get("kind") in kinds}))
    if source_events:
        latest = max(source_events, key=lambda e: (str(e.get("created_at", "")), str(e.get("id", ""))))
        stats.last_processed_event_id = latest.get("id")
    if actions:
        latest_action = max(actions, key=lambda a: (str(a.get("created_at", "")), str(a.get("id", ""))))
        stats.approval_state = latest_action.get("status", "unknown")
    for cost in budget_sources:
        currency = cost["currency"]
        stats.costs_by_currency[currency] = stats.costs_by_currency.get(currency, 0.0) + float(cost["amount"])
    all_dates = [str(m.get("received_at", m.get("created_at", ""))) for m in inbound] + [str(a.get("created_at", "")) for a in actions] + [str(e.get("created_at", "")) for e in source_events]
    all_dates = [date for date in all_dates if date]
    stats.observation_window = {"start": min(all_dates, default=None), "end": max(all_dates, default=None)}
    contacted = {str(a.get("conversation_id")) for a in sent}
    numerator = len(answered_conversations & contacted)
    denominator = len(contacted)
    stats.response_rate = {"numerator": numerator, "denominator": denominator, "unit": "contacted conversations with a human reply", "value": numerator / denominator if denominator else None, "window": stats.observation_window}
    return stats


def list_company_stats(store) -> list[CompanyStats]:
    accounts = {canonical_account(c) for c in _rows(store, "list_conversations", "workflow_conversations")}
    accounts.update(canonical_account(o) for o in _rows(store, "list_opportunities", "workflow_opportunities"))
    return [get_company_stats(store, account_id) for account_id in sorted(accounts)]


def _csv_safe(value):
    text = str(value if value is not None else "unknown")
    return "'" + text if text.startswith(("=", "+", "-", "@", "\t", "\r")) else text


def export_stats(store, output: str | Path) -> list[Path]:
    directory = Path(output)
    directory.mkdir(parents=True, exist_ok=True)
    stats = list_company_stats(store)
    fields = ["account_id", "stage", "is_terminal", "messages_sent", "human_replies", "auto_replies", "refusals", "optouts", "meetings_proposed", "meetings_scheduled", "meetings_held", "meetings_no_show", "stats_version"]
    path = directory / "companies.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in stats:
            data = item.model_dump()
            writer.writerow({key: _csv_safe(data[key]) for key in fields})
    paths = [path]
    for item in stats:
        # Hash the file name rather than interpolate untrusted IDs as paths.
        name = hashlib.sha256(item.account_id.encode()).hexdigest()[:20]
        target = directory / (name + ".md")
        data = json.dumps(item.model_dump(mode="json"), ensure_ascii=False, indent=2)
        target.write_text(f"# Компания {item.account_id}\n\nСнимок из журнала; complaints и deliverability могут быть unknown.\n\n```json\n{data}\n```\n", encoding="utf-8")
        paths.append(target)
    return paths


def build_report(store) -> dict:
    companies = list_company_stats(store)
    conversations, opportunities = store.list_conversations(), store.list_opportunities()
    actions, inbound, events = store.list_actions(), store.list_inbound(), store.list_events()
    sent = [a for a in actions if a.get("status") == "sent"]
    manual = [a for a in actions if a.get("status") == "sent_confirmed_by_user"]
    human = [m for m in inbound if not m.get("is_auto_reply") and not m.get("is_operator") and not m.get("bounce") and not _labels(m) & {"auto_reply", "ooo", "out_of_office", "hard_bounce", "soft_bounce", "operator_message"}]
    contacted = {a.get("conversation_id") for a in sent if a.get("conversation_id")}
    answered = {m.get("conversation_id") for m in human if m.get("conversation_id")} & contacted
    dates = [str(a["created_at"]) for a in actions if a.get("created_at")]
    dates += [str(m.get("received_at", m.get("created_at"))) for m in inbound if m.get("received_at", m.get("created_at"))]
    window = {"start": min(dates, default=None), "end": max(dates, default=None)}
    counters = ["messages_sent", "send_attempts", "delivery_errors", "uncertain_messages", "human_replies", "auto_replies", "refusals", "optouts", "substantive_interest", "meetings_proposed", "meetings_scheduled", "meetings_held", "meetings_no_show"]
    meeting_kinds = {"meeting_proposed", "meeting_scheduled", "meeting_held", "meeting_no_show", "meeting_cancelled"}
    result = {"as_of": datetime.now(UTC).isoformat(), "window_start": window["start"], "window_end": window["end"], "companies": len({c.get("account_id") for c in conversations if c.get("account_id")}), "customers": len(companies), "opportunities": len(opportunities), "contacts": len({a["recipient"] for a in actions if a.get("recipient")}), "requests": len({o["request_id"] for o in opportunities if o.get("request_id")}), "actions": len(actions), "provider_accepted_messages": len(sent), "manual_confirmed_messages": len(manual), "simulated_messages": sum(a.get("status") == "simulated_sent" for a in actions), "operator_rejections": sum(bool(a.get("rejection_actor")) for a in actions), "metrics": {name: sum(getattr(company, name) for company in companies) for name in counters}, "human_replies": len(human), "auto_replies": sum(bool(m.get("is_auto_reply") or _labels(m) & {"auto_reply", "ooo", "out_of_office"}) for m in inbound), "substantive_interest": sum(bool(_labels(m) & {"interest", "meeting_interest"}) and not _labels(m) & {"opt_out", "account_opt_out", "not_interested", "unsubscribe"} for m in human), "refusals": sum(bool(_labels(m) & {"opt_out", "account_opt_out", "not_interested", "unsubscribe"}) for m in human), "meeting_events": [e for e in events if e.get("kind") in meeting_kinds], "human_reply_rate": {"numerator": len(answered), "denominator": len(contacted), "unit": "contacted conversations", "value": len(answered) / len(contacted) if contacted else None, "window": window}, "company_stats": [c.model_dump(mode="json") for c in companies], "deliverability": "unknown", "complaints": "unknown", "limits": ["Sent means provider acceptance; manually confirmed and simulated messages are separate", "Scheduled, held, no-show and sales outcomes are separate events", "Complaints and deliverability remain unknown without monitoring", "Every rate includes its denominator and observation window"]}
    for kind in meeting_kinds:
        result[kind] = len({e["id"] for e in events if e.get("kind") == kind})
    result["errors"] = sum(a.get("status") in {"failed", "uncertain", "bounced", "rejected"} and not a.get("rejection_actor") for a in actions)
    return result
