"""First-call readiness and owner-recorded outcomes; no calendar assumptions."""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from .engagement import as_dict
from .models import MeetingBrief, MeetingReadiness


def assess_meeting_readiness(conversation, opportunity=None) -> MeetingReadiness:
    c, o = as_dict(conversation), as_dict(opportunity)
    task = c.get("confirmed_task") or c.get("task") or o.get("task")
    labels = set(c.get("last_labels", []))
    blocked = c.get("is_terminal") or set(c.get("holds", [])) & {"suppression", "manual_review", "human_handoff"}
    related = bool(c.get("participant_related", c.get("participants")))
    interest = c.get("interest") == "confirmed_in_conversation" or bool(labels & {"interest", "meeting_interest"})
    ready = bool(task and task != "unknown" and related and interest and not blocked and "satisfied" not in labels)
    return MeetingReadiness(
        ready=ready, reasons=["Конкретная задача, связанный собеседник и предметный интерес"] if ready else ["Оснований для встречи пока недостаточно"],
        unknowns=[] if ready else ["Уточнить релевантную задачу, участника или предметный интерес"],
        goal="Разобрать один реальный пример процесса и определить применимость следующего шага" if ready else "",
        participants=[str(p) for p in c.get("participants", [])],
    )


def build_meeting_brief(store, conversation_id: str, catalog=None) -> MeetingBrief:
    c = store.get_conversation(conversation_id)
    if not c:
        raise ValueError("Unknown conversation")
    o = store.get_opportunity(c.get("opportunity_id", "")) or {}
    boundaries = []
    if catalog and c.get("service_id", o.get("service_id")):
        offer = catalog.service(c.get("service_id", o.get("service_id")))
        boundaries = list(offer.limitations) + list(offer.unsupported_tasks)
    restrictions = [str(item) if not isinstance(item, dict) else f"{item.get('value', '')} [{item.get('source_ref', '')}]"
                    for item in c.get("constraints", [])]
    return MeetingBrief(
        conversation_id=conversation_id, opportunity_id=o.get("id", c.get("opportunity_id", "")),
        company=o.get("company_name") or o.get("account_id") or "unknown",
        participants=[str(item) for item in c.get("participants", [])], origin=o.get("origin", "unknown"),
        occasion=c.get("occasion", o.get("request_id") or ""),
        customer_task=c.get("confirmed_task", c.get("task", o.get("task", "unknown"))),
        message_references=list(dict.fromkeys(c.get("task_source_refs", []) + c.get("rfc_message_ids", []))),
        confirmed=[str(item) for item in c.get("confirmed", [])], hypotheses=[str(item) for item in c.get("hypotheses", [])],
        unknowns=c.get("unknowns", o.get("unknowns", [])), service_id=c.get("service_id", o.get("service_id")) or "",
        service_boundaries=boundaries, questions=c.get("meeting_questions", c.get("questions_asked", [])),
        promises=c.get("promises", []), useful_outcome="Проверить применимость решения и согласовать обоснованный следующий шаг",
        restrictions=restrictions + [str(item) for item in c.get("holds", [])],
    )


def mark_meeting(store, conversation_id: str, event: str, local_time: str | None = None,
                 timezone: str | None = None, actor: str = "operator", confirmed: bool = False,
                 event_id: str | None = None, notes: str = "") -> dict:
    """A concrete confirmed slot establishes scheduled; held is a separate owner event."""
    if actor.lower() in {"agent", "llm", "model", "assistant"} or not actor.strip():
        raise ValueError("Meeting outcomes require owner or trusted integration identity")
    c = store.get_conversation(conversation_id)
    if not c:
        raise ValueError("Unknown conversation")
    aliases = {"proposed": "meeting_proposed", "scheduled": "meeting_scheduled", "held": "meeting_held",
               "no-show": "meeting_no_show", "no_show": "meeting_no_show", "cancelled": "meeting_cancelled"}
    kind = aliases.get(event, event)
    if kind not in set(aliases.values()):
        raise ValueError("Unsupported meeting event")
    details = {"actor": actor, "notes": notes, "conversation_id": conversation_id}
    if kind == "meeting_scheduled":
        if not confirmed or not local_time or not timezone:
            raise ValueError("Scheduling requires explicit confirmation, date/time and IANA timezone")
        zone = ZoneInfo(timezone)
        local = datetime.fromisoformat(local_time)
        if local.tzinfo is not None:
            # Require the supplied timezone to agree, rather than silently guessing.
            if local.utcoffset() != local.astimezone(zone).utcoffset():
                raise ValueError("Timestamp offset conflicts with IANA timezone")
            local = local.astimezone(zone)
        else:
            local = local.replace(tzinfo=zone)
            roundtrip = local.astimezone(UTC).astimezone(zone)
            if roundtrip.replace(tzinfo=None) != local.replace(tzinfo=None):
                raise ValueError("Local time does not exist due to timezone transition")
            if local.replace(fold=0).utcoffset() != local.replace(fold=1).utcoffset():
                raise ValueError("Ambiguous local time requires an explicit UTC offset")
        details.update(local_time=local.isoformat(), timezone=timezone, utc=local.astimezone(UTC).isoformat(),
                       availability="owner_proposed_unchecked_calendar")
    elif kind in {"meeting_held", "meeting_no_show"} and c.get("stage") != "meeting_scheduled":
        raise ValueError("Record a confirmed meeting before held/no-show")
    event_id = event_id or "meeting_" + hashlib.sha256(f"{conversation_id}:{kind}:{details}".encode()).hexdigest()
    existing = [e for e in store.list_events(conversation_id) if e["id"] == event_id]
    if existing:
        return c
    stage = kind if kind in {"meeting_proposed", "meeting_scheduled", "meeting_held"} else c.get("stage", "new")
    updated = {**c, "stage": stage, "meeting": {"status": kind, **details},
               "context_version": c.get("context_version", 1) + 1, "stats_version": c.get("stats_version", 0) + 1}
    store.record_event(kind, conversation_id, details, event_id=event_id)
    store.put_conversation(updated)
    for action in store.list_actions():
        if action.get("conversation_id") == conversation_id and action["status"] in {"pending_approval", "approved", "deferred"}:
            store.update_action(action["id"], status="cancelled", cancellation_reason="Meeting context changed")
    return updated
