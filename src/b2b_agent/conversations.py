"""Idempotent inbound interpretation with conservative deterministic stop handling."""
from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from .engagement import as_dict, recommend_next_action


class ReplyClassification(BaseModel):
    labels: list[str] = Field(default_factory=list)
    evidence: dict[str, list[str]] = Field(default_factory=dict)
    confidence: str = "medium"
    unknowns: list[str] = Field(default_factory=list)
    new_text: str = ""


def authored_text(text: str) -> str:
    """Discard quoted history/signatures without truncating the author's new tail."""
    lines = []
    for line in text.splitlines():
        if re.match(r"^\s*(?:On .+wrote:|В .+писал|.+ написал(?:а)?:|[-_]{3,}\s*(?:Original Message|Пересылаемое))", line, re.I):
            break
        if line.strip() in {"--", "-- "}:
            break
        if not line.lstrip().startswith(">"):
            lines.append(line)
    return "\n".join(lines).strip()


PATTERNS = {
    "account_opt_out": r"(?:не\s+(?:обращайтесь|пишите|связывайтесь).{0,45}(?:компани|организац|сотрудник)|do not contact (?:our )?company)",
    "opt_out": r"(?:не\s+(?:пишите|писать|присылайте|отправляйте|беспокойте)|не\s+(?:надо|нужно)\s+(?:писать|присылать)|не\s+нужны\s+(?:ваши\s+)?(?:письма|сообщения)|удалите\s+меня(?:\b|$)|please remove me|(?:удалите|исключите).{0,35}(?:рассылк|адрес|контакт)|отпишите\s+меня|прошу\s+отписать|\bunsubscribe\b|do not (?:email|write|contact)|stop (?:emailing|contacting))",
    "no_calls": r"(?:не\s+(?:звоните|звонить)|do not call)",
    "not_interested": r"(?:\bнеинтересно\b|не\s+интересует|не\s*актуальн|не\s+нужно|not interested)",
    "information_request": r"(?:пришлите|отправьте|можно\s+(?:получить|посмотреть)).{0,40}(?:информац|подробност|описани|материал)|send (?:me |us )?(?:information|details)",
    "price_question": r"(?:сколько\s+(?:стоит|будет)|какая\s+цен|стоимост|расценк|\bпрайс\b|what.{0,15}(?:price|cost))",
    "timeline_question": r"(?:какие\s+сроки|за\s+какой\s+срок|когда\s+(?:сможете|можно)\s+начать|how long)",
    "case_request": r"(?:кейс|портфолио|референс|рекомендаци.{0,20}клиент|client case|portfolio|reference)",
    "automated": r"(?:у\s+нас.{0,30}(?:полностью\s+)?автоматизирован|уже\s+автоматизирован|already automated)",
    "existing_system": r"(?:уже\s+(?:используем|внедрили)|already using)",
    "task_statement": r"[^\n.!?]*(?:нам\s+(?:нужно|нужна|нужен)|наша\s+задача|хотим\s+автоматизировать|нужно\s+автоматизировать)[^\n.!?]*",
    "satisfied": r"(?:вс[её]\s+устраивает|нас\s+устраивает|вс[её]\s+работает|happy with)",
    "wrong_role": r"(?:не\s+мо[яё]\s+(?:зона|ответственност)|я\s+не\s+отвечаю|not my (?:role|responsibility))",
    "referral": r"(?:обратитесь|напишите|свяжитесь).{0,80}(?:@|коллег|отдел|сотрудник)|contact.{0,40}@",
    "defer": r"(?:не\s+сейчас|вернитесь|напишите\s+(?:в|через|после)|позже|not now|come back)",
    "no_budget": r"(?:нет\s+бюджета|бюджет.{0,15}нет|без\s+бюджета|no budget)",
    "meeting_interest": r"(?:давайте\s+(?:созвонимся|обсудим|встретимся)|готов[аы]?\s+(?:созвониться|к\s+встрече)|let'?s (?:meet|have a call))",
    "interest": r"(?:\bинтересно\b|интересует|готов[аы]?\s+обсудить|давайте\s+обсудим|interested)",
    "commercial_exception": r"(?:скидк|гаранти(?:руете|я|и)|(?:подписать|подпиш\w*|примите|принимаете|согласны).{0,30}(?:nda|договор|услови)|\bnda\b|бесплатн.{0,15}пилот|(?:сделайте|обещаете|обязуетесь).{0,40}(?:за\s+\d+|срок|безопасност|данные)|discount|guarantee)",
    "privacy_question": r"(?:откуда.{0,25}(?:адрес|контакт|данные)|персональн.{0,15}данн|where did you get)",
}


def classify_reply(event: Any) -> ReplyClassification:
    data = as_dict(event)
    text = authored_text(str(data.get("full_author_text") or data.get("author_text") or data.get("new_text") or data.get("body") or data.get("text") or ""))
    labels, evidence = [], {}
    # Headers are parsed by the adapter; service notifications cannot be sales interest.
    category = data.get("kind") or data.get("message_type")
    if data.get("hard_bounce") or data.get("bounce") == "hard" or category == "hard_bounce":
        return ReplyClassification(labels=["hard_bounce"], new_text=text, confidence="high")
    if data.get("soft_bounce") or data.get("bounce") == "soft" or category == "soft_bounce":
        return ReplyClassification(labels=["soft_bounce"], new_text=text, confidence="high")
    if data.get("is_auto_reply") or data.get("auto_reply") or category in {"auto_reply", "auto"}:
        return ReplyClassification(labels=["auto_reply"], new_text=text, confidence="high")
    if not text and re.fullmatch(r"\s*(?:не писать|не пишите|unsubscribe)\s*", str(data.get("subject", "")), re.I):
        return ReplyClassification(labels=["opt_out"], evidence={"opt_out": [str(data["subject"])]},
                                   new_text="", confidence="high")
    for label, pattern in PATTERNS.items():
        matches = []
        for match in re.finditer(pattern, text, re.I | re.S):
            prefix = text[max(0, match.start() - 32):match.start()].lower()
            # "Не надо отписывать", "не прошу отписать" negate an unsubscribe request.
            if label == "opt_out" and re.search(r"(?:не\s+(?:нужно|надо|прошу)|не\s+хочу|не\s+требуется)\s*$", prefix):
                continue
            if label == "interest" and re.search(r"\bне\s*$", prefix):
                continue
            matches.append(match.group(0))
        if matches:
            labels.append(label)
            evidence[label] = matches
    if "account_opt_out" in labels and "opt_out" not in labels:
        labels.append("opt_out")
    uncertain_quote = data.get("quote_parse_uncertain", data.get("author_text_uncertain", False))
    if uncertain_quote and "ambiguous" not in labels:
        labels.append("ambiguous")
    return ReplyClassification(labels=labels or ["ambiguous"], evidence=evidence,
                               confidence="high" if labels and not uncertain_quote else "low", new_text=text,
                               unknowns=["Неоднозначная граница нового авторского текста/цитаты; нужен ручной разбор"] if uncertain_quote else ([] if labels else ["Нужен ручной разбор смысла ответа"]))


def _participants(conversation: dict) -> set[str]:
    participants = conversation.get("participants", [])
    return {str(p.get("email", p.get("value", "")) if isinstance(p, dict) else p).lower()
            for p in participants} | {str(conversation.get("recipient", "")).lower()}


def match_conversation(store, event: Any) -> tuple[dict | None, str]:
    e = as_dict(event)
    sender = str(e.get("sender", e.get("from_email", ""))).lower()
    references = e.get("references", [])
    if isinstance(references, str):
        references = references.split()
    refs = set(references) | {e.get("in_reply_to", "")}
    candidates = []
    for conv in store.list_conversations():
        if e.get("mailbox_id", "default") != conv.get("mailbox_id", "default"):
            continue
        operator = e.get("is_operator", e.get("operator_sent", False))
        participants = _participants(conv)
        known_senders = {str(conv.get("sender_account", "")).lower()}
        known_senders.update(str(a.get("sender_account", "")).lower() for a in store.list_actions()
                             if a.get("conversation_id") == conv["id"])
        actual_recipients = {str(address).lower() for address in e.get("recipients", [])}
        valid_operator = bool(operator and sender in known_senders and actual_recipients & participants)
        delivery_recipient = str(e.get("delivery_recipient", "")).lower()
        valid_dsn = bool(e.get("bounce") and delivery_recipient in participants)
        if sender not in participants and not valid_operator and not valid_dsn:
            continue
        known = set(conv.get("rfc_message_ids", []))
        if conv.get("last_rfc_message_id"):
            known.add(conv["last_rfc_message_id"])
        for action in store.list_actions():
            if action.get("conversation_id") == conv["id"]:
                known.update(filter(None, [action.get("rfc_message_id"), action.get("reply_to_message_id")]))
        thread_match = bool(e.get("thread_id") and e["thread_id"] == conv.get("provider_thread_id", conv.get("thread_id")))
        explicit = bool(e.get("conversation_id") == conv["id"] and e.get("operator_verified"))
        if thread_match or bool(refs & known) or explicit:
            candidates.append(conv)
    if len(candidates) == 1:
        return candidates[0], "matched"
    return None, "ambiguous" if candidates else "unmatched"


def _event_id(e: dict) -> str:
    provider = e.get("provider_message_id") or e.get("id") or e.get("rfc_message_id")
    if not provider:
        raise ValueError("Inbound event requires a stable provider or RFC message ID")
    return hashlib.sha256(f"{e.get('mailbox_id', 'default')}:{provider}".encode()).hexdigest()


def apply_inbound_event(store, event: Any, catalog=None, config=None) -> dict:
    """Apply one complete author message before considering any outbound draft."""
    e = dict(as_dict(event))
    e["id"] = _event_id(e)
    e.setdefault("mailbox_id", "default")
    e.setdefault("provider_message_id", e.get("rfc_message_id", e["id"]))
    e.setdefault("sender", e.get("from_email", ""))
    classification = classify_reply(e)
    e["labels"] = classification.labels
    e["author_text"] = classification.new_text
    conv, match_status = match_conversation(store, e)
    if conv is None:
        # Unmatched opt-out still blocks its literal author; never infer an account.
        stop = "contact" if "opt_out" in classification.labels else None
        applied = store.apply_inbound(e, None, labels=classification.labels, stop_scope=stop)
        store.record_event("inbound_manual_review", e["sender"],
                           {**e, "match_status": match_status}, event_id="manual-review:" + e["id"])
        return {"status": "manual_review" if applied else "duplicate", "match_status": match_status,
                "event_id": e["id"], "labels": classification.labels}
    labels = set(classification.labels)
    e["account_id"] = conv.get("account_id")
    e["channel"] = conv.get("channel", "email")
    if "hard_bounce" in labels and e.get("delivery_recipient"):
        e["suppression_target"] = e["delivery_recipient"]
    holds = set(conv.get("holds", []))
    updates = {"last_labels": classification.labels, "last_inbound_id": e["id"],
               "last_author": e["sender"], "last_response_excerpt": classification.new_text[:1200],
               "source_refs": list(dict.fromkeys(conv.get("source_refs", []) + [e["id"]]))}
    if "meeting_interest" in labels:
        updates["stage"] = "meeting_proposed"
    refs = list(dict.fromkeys(conv.get("rfc_message_ids", []) + [e.get("rfc_message_id", "")]))
    updates["rfc_message_ids"] = [ref for ref in refs if ref]
    if e.get("thread_id"):
        updates["provider_thread_id"] = e["thread_id"]
    stop_scope = None
    critical = []
    for sentence in re.split(r"[\n!?]+|(?<!\d)\.(?!\d)", classification.new_text):
        if re.search(r"(?:\d[\d\s.,]*\s*(?:₽|руб|RUB|USD|EUR|доллар|евро)|\b20\d{2}-\d{2}-\d{2}\b|\d{1,2}:\d{2}|(?:Europe|Asia|America)/[A-Za-z_]+|\bне\s+(?:звон|пис|соглас|готов)|бюджет|срок|услови|nda|безопасност)", sentence, re.I):
            critical.append({"value": sentence.strip(), "source_ref": e["id"], "author": e["sender"], "role": e.get("author_role", "unknown"), "scope": "statement"})
    if critical:
        updates["constraints"] = conv.get("constraints", []) + critical
        conflicts = list(conv.get("conflicts", []))
        for new in critical:
            new_text = new["value"].lower()
            category = "budget" if re.search(r"бюджет|(?:rub|usd|eur|руб|₽)", new_text) else "timing" if re.search(r"срок|20\d{2}-\d{2}-\d{2}", new_text) else None
            for old in conv.get("constraints", []):
                if not isinstance(old, dict) or old.get("author") == e["sender"]:
                    continue
                old_text = str(old.get("value", "")).lower()
                same = (category == "budget" and re.search(r"бюджет|(?:rub|usd|eur|руб|₽)", old_text)) or (category == "timing" and re.search(r"срок|20\d{2}-\d{2}-\d{2}", old_text))
                if same and re.findall(r"\d+", old_text) != re.findall(r"\d+", new_text):
                    conflicts.append({"kind": category, "sources": [old, new], "resolution": "unknown"})
        if conflicts:
            holds.add("manual_review")
            updates["conflicts"] = conflicts
    if "case_request" in labels and catalog is not None and conv.get("service_id"):
        from .messaging import case_answer
        try:
            _, _, blockers = case_answer(catalog, catalog.service(conv["service_id"]), classification.new_text, context=conv)
        except ValueError:
            blockers = ["Услуга не найдена в текущем каталоге"]
        if blockers:
            holds.add("manual_review")
            updates["unknowns"] = list(dict.fromkeys(conv.get("unknowns", []) + blockers))
    if labels & {"opt_out", "account_opt_out", "hard_bounce"}:
        stop_scope = "account" if "account_opt_out" in labels and conv.get("account_id") else "contact"
        holds.add("suppression")
        updates.update(stage="closed", is_terminal=True, closed_reason="opt_out" if "opt_out" in labels else "hard_bounce",
                       closed_by=e["sender"], closed_at=datetime.now(UTC).isoformat(), closed_event_id=e["id"], activity="closed")
    elif "no_calls" in labels:
        # Retain specific prohibition; it never grants email permission.
        store.suppress("channel", e["sender"], "inbound no calls", channel="phone")
        updates["constraints"] = updates.get("constraints", conv.get("constraints", [])) + [{"value": "Не звонить", "source_ref": e["id"], "author": e["sender"], "scope": "phone"}]
    if "not_interested" in labels and stop_scope is None:
        updates.update(stage="closed", is_terminal=True, closed_reason="not_interested",
                       closed_by=e["sender"], closed_at=datetime.now(UTC).isoformat(), closed_event_id=e["id"], activity="closed")
    if e.get("is_operator") or e.get("operator_sent"):
        holds.add("human_handoff")
        updates["activity"] = "human_handoff"
    elif not labels & {"auto_reply", "hard_bounce", "soft_bounce"} and not updates.get("is_terminal"):
        updates.update(stage="meeting_proposed" if "meeting_interest" in labels else "replied", activity="waiting_for_user_approval")
        updates["last_human_reply_at"] = e.get("received_at", e.get("date", datetime.now(UTC).isoformat()))
    if labels & {"ambiguous", "privacy_question", "commercial_exception"}:
        holds.add("manual_review")
    if "soft_bounce" in labels:
        holds.add("technical_error")
    if "defer" in labels and not updates.get("is_terminal"):
        updates["activity"] = "deferred"
        # Preserve exact request. An unqualified month/year never becomes an invented date.
        updates["defer_condition"] = {"text": classification.new_text, "source_ref": e["id"]}
        dates = re.findall(r"\b20\d{2}-\d{2}-\d{2}\b", classification.new_text)
        if len(dates) == 1:
            updates["next_review_at"] = dates[0] + "T00:00:00+00:00"
        else:
            holds.add("manual_review")
    if "automated" in labels:
        updates["need_status"] = "disproved"
        updates["constraints"] = updates.get("constraints", conv.get("constraints", [])) + [{"value": classification.new_text, "source_ref": e["id"], "author": e["sender"], "scope": "process"}]
    if "interest" in labels and not labels & {"opt_out", "not_interested", "satisfied"}:
        updates["interest"] = "confirmed_in_conversation"
        updates["interest_source_ref"] = e["id"]
        updates["unknowns"] = [value for value in conv.get("unknowns", [])
            if not ("интерес" in str(value).lower() and "не подтвержд" in str(value).lower())]
        updates["confirmed"] = conv.get("confirmed", []) + [{
            "value": classification.evidence.get("interest", ["Предметный интерес"])[0],
            "source_ref": e["id"], "author": e["sender"], "kind": "interest"}]
    opportunity_update = None
    opportunity = store.get_opportunity(conv.get("opportunity_id", "")) or {}
    current_need = as_dict(opportunity.get("need"))
    if labels & {"automated", "satisfied"}:
        contradiction = {"id": "conversation:" + e["id"], "kind": "contradiction",
            "quote": classification.new_text, "inbound_message_id": e["id"], "author_id": e["sender"],
            "observed_at": datetime.now(UTC).isoformat()}
        opportunity_update = {"need": {**current_need, "status": "disproved", "description": classification.new_text,
            "evidence_ids": list(dict.fromkeys(current_need.get("evidence_ids", []) + [contradiction["id"]])),
            "confirmed_by": e["sender"], "confirmed_at": datetime.now(UTC).isoformat()},
            "evidence": opportunity.get("evidence", []) + [contradiction]}
    elif "task_statement" in labels:
        task_fragment = classification.evidence["task_statement"][0].strip()
        ref = "conversation:" + e["id"]
        confirmation = {"id": ref, "kind": "conversation_confirmation", "quote": task_fragment,
            "inbound_message_id": e["id"], "author_id": e["sender"], "observed_at": datetime.now(UTC).isoformat()}
        opportunity_update = {"need": {"status": "confirmed_in_conversation", "description": task_fragment,
            "evidence_ids": [ref], "confirmed_by": e["sender"], "confirmed_at": datetime.now(UTC).isoformat()},
            "evidence": opportunity.get("evidence", []) + [confirmation]}
        updates["confirmed_task"] = task_fragment
        updates["task_source_refs"] = [e["id"]]
        updates["confirmed"] = updates.get("confirmed", conv.get("confirmed", [])) + [{"value": task_fragment, "source_ref": e["id"], "author": e["sender"]}]
    updates["holds"] = sorted(holds)
    proposed = recommend_next_action({**conv, **updates}, classification)
    pause_reason = store.pause_reason({"conversation_id": conv["id"], "account_id": conv.get("account_id"), "campaign_id": conv.get("campaign_id")})
    if pause_reason and proposed["requires_approval"]:
        proposed = {"action": "human_handoff", "reason": pause_reason, "requires_approval": False}
    updates["next_action"] = proposed
    applied = store.apply_inbound(e, conv["id"], labels=classification.labels,
                                  stop_scope=stop_scope, conversation_update=updates, opportunity_update=opportunity_update)
    if not applied:
        return {"status": "duplicate", "event_id": e["id"], "conversation_id": conv["id"]}
    if "meeting_interest" in labels and not updates.get("is_terminal"):
        store.record_event("meeting_proposed", conv["id"], {"source_ref": e["id"], "by": e["sender"]},
                           event_id="meeting-proposed:" + e["id"])
    result = {"status": "applied", "event_id": e["id"], "conversation_id": conv["id"],
              "labels": classification.labels, "next_action": proposed}
    if catalog is not None and proposed["requires_approval"] and not holds:
        from .messaging import prepare_reply_draft
        current = store.get_conversation(conv["id"])
        payload = prepare_reply_draft(current, classification, catalog, e, duration_minutes=config.meetings.duration_minutes if config else 25)
        if payload is not None:
            if config is not None:
                from .action_policy import cost_policy_version
                from .workflow import assess_current_action
                preparation = {**payload, "requires_fresh_sync": False}
                payload["assessment"] = assess_current_action(store, preparation, config).model_dump(mode="json")
                payload["cost_policy_version"] = cost_policy_version(config)
            result["draft"] = store.queue_action(payload)
    return result


def prepare_followup(store, conversation_id: str, catalog, config=None, reason: str | None = None,
                     due_at: str | None = None, max_followups: int = 1) -> dict:
    """Prepare one new useful follow-up for review; no silence-based auto-send."""
    c = store.get_conversation(conversation_id)
    if not c:
        raise ValueError("Unknown conversation")
    if c.get("is_terminal") or c.get("holds") or store.pause_reason({"conversation_id": conversation_id, "account_id": c.get("account_id")}):
        raise ValueError("Conversation is closed, held or paused")
    actions = [a for a in store.list_actions() if a.get("conversation_id") == conversation_id]
    if any(a.get("status") in {"sending", "uncertain"} for a in actions):
        raise ValueError("Resolve uncertain delivery before preparing another message")
    if any(m for m in store.list_inbound(conversation_id) if not set(m.get("labels", [])) & {"auto_reply", "soft_bounce"}):
        raise ValueError("A substantive inbound exists; respond to that event instead of silence")
    previous = [a for a in actions if a.get("kind") in {"followup", "follow_up"}]
    if len(previous) >= max_followups:
        raise ValueError("Configured follow-up allowance exhausted")
    if not any(a.get("status") == "sent" for a in actions):
        raise ValueError("A follow-up requires a previously sent message")
    if not reason or len(reason.strip()) < 12:
        raise ValueError("A follow-up requires a concrete new fact, material or agreed return reason")
    if not store.sync_is_fresh(c.get("mailbox_id", "default"), config.inbox.freshness_minutes * 60 if config else 300):
        raise ValueError("Successful fresh inbox sync or reliable operator import is required")
    offer = catalog.service(c.get("service_id", ""))
    if not offer.enabled:
        raise ValueError("Service disabled")
    # A reason is owner input for this specific draft, not a new automatic promise.
    payload = {
        "conversation_id": conversation_id, "opportunity_id": c.get("opportunity_id"), "account_id": c.get("account_id"),
        "recipient": c.get("recipient"), "channel": c.get("channel", "email"), "kind": "followup",
        "sender_account": catalog.sender_profile.from_email,
        "mailbox_id": c.get("mailbox_id", "default"), "service_id": offer.id,
        "subject": "Re: " + c.get("subject", "Обсуждение задачи").removeprefix("Re: "),
        "body": "Здравствуйте!\n\n" + reason.strip() + "\n\nАктуально ли обсудить этот конкретный следующий шаг?"
                + ("\n\n" + catalog.sender_profile.signature if catalog.sender_profile.signature else "")
                + '\n\nЕсли дальнейшие обращения не нужны, ответьте «не писать».',
        "goal": "Предложить один новый полезный шаг", "reason": reason.strip(), "due_at": due_at,
        "context_version": c.get("context_version", 1), "stats_version": c.get("stats_version", 0),
        "catalog_version": catalog.version, "dependency_version": catalog.dependency_version(offer.id, []),
        "proof_asset_ids": [], "attachments": [], "contact_permission": c.get("contact_permission", {"status": "unknown"}),
        "requires_fresh_sync": True, "thread_id": c.get("provider_thread_id"),
        "in_reply_to": c.get("last_outbound_rfc_id", c.get("last_rfc_message_id")), "references": c.get("rfc_message_ids", []),
        "original_subject": c.get("subject", "Обсуждение задачи"),
        "source_url": c.get("source_url", ""), "scheduled_action_id": f"followup:{conversation_id}:{len(previous) + 1}",
        "offer_snapshot": offer.model_dump(mode="json"), "sender_snapshot": catalog.sender_profile.model_dump(mode="json"),
        "validation_results": [],
    }
    if store.is_suppressed(payload):
        raise ValueError("Recipient is suppressed")
    if due_at and datetime.fromisoformat(due_at).tzinfo is None:
        raise ValueError("Follow-up date requires an explicit timezone")
    # The free-text reason is owner-authored. The authenticated approval CLI
    # records its exact content review and actor; preparation cannot invent one.
    payload["content_review_required"] = True
    if config is not None:
        from .action_policy import cost_policy_version
        from .workflow import assess_current_action
        payload["assessment"] = assess_current_action(store, payload, config).model_dump(mode="json")
        payload["cost_policy_version"] = cost_policy_version(config)
    return store.queue_action(payload)
