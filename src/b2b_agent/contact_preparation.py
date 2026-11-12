"""Prepare a research-based first-contact draft without granting send authority."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from urllib.parse import urlsplit

from .engagement import plan_contact
from .messaging import build_draft, stamp_draft_content, validate_draft
from .state import encode
from .workflow import assess_current_action, current_catalog, workflow_store

MAILBOX = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
UNKNOWN = {"", "unknown", "none", "not_set", "неизвестно"}


def _known(value) -> bool:
    return str(value).strip().casefold() not in UNKNOWN


def _source(value: str) -> bool:
    """References are inert data; no local file is opened and no URL is fetched."""
    if value.startswith("operator:"):
        return len(value) > len("operator:") and not any(c in value for c in "\r\n\x00")
    parsed = urlsplit(value)
    return bool(parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password and not any(c in value for c in "\r\n\x00"))


def _research(store, account_id):
    try:
        row = store.db.execute("SELECT payload FROM companies WHERE domain=?", (account_id,)).fetchone()
    except sqlite3.OperationalError as error:
        if "no such table" in str(error):
            return None
        raise
    return json.loads(row[0]) if row else None


def prepare_company_contact(box, opportunity_id: str, recipient: str, channel: str, config,
                            *, verified_contact: bool = False, basis: str = "", source: str = "") -> dict:
    """Queue one exact draft. Permission stays unknown until all cold-contact checks pass.

    ``verified_contact`` records explicit operator verification of the supplied
    literal identity/source; it is never inferred from an email-domain pattern.
    Calling this operation cannot approve or send a message.
    """
    store = workflow_store(box)
    opportunity = store.get_opportunity(opportunity_id)
    if not opportunity:
        raise ValueError("Unknown opportunity; run company research first")
    if opportunity.get("origin") != "company_research":
        raise ValueError("Use inspect-request or the existing conversation for a public request")
    account = opportunity.get("account_id")
    if not account or account == "unknown":
        raise ValueError("Company research requires a confirmed canonical account; do not use a marketplace domain")
    if opportunity.get("status") == "disqualified":
        raise ValueError("The opportunity is disqualified")
    recipient, channel = recipient.strip(), channel.strip().lower()
    if not recipient or any(c in recipient for c in "\r\n\x00"):
        raise ValueError("Supply one literal recipient without header characters")
    if channel == "email":
        if not MAILBOX.fullmatch(recipient):
            raise ValueError("Supply one literal published or operator-verified email; addresses are never generated")
        recipient = recipient.lower()
    elif channel not in {"platform", "linkedin", "telegram", "phone", "messenger", "other"}:
        raise ValueError("Unknown channel; register its capability or use an explicit manual channel")
    service_id = opportunity.get("service_id")
    if not service_id:
        raise ValueError("Select a supported service before preparing contact")
    catalog = current_catalog(config)
    offer = catalog.service(service_id)
    if not offer.enabled:
        raise ValueError("The selected service is disabled")
    task_fit = opportunity.get("assessment", {}).get("task_fit", {})
    if isinstance(task_fit, dict) and task_fit.get("value") == "unsupported":
        raise ValueError("The task is unsupported by the active service catalog")
    research = _research(store, account) or {}
    if research and research.get("domain") != account:
        raise ValueError("Research/account identity conflicts require manual review")
    # Research contacts must belong to this actual company website, never a
    # platform supplied as a request source. Operator verification is explicit.
    website_host = (urlsplit(research.get("website", "")).hostname or "").lower().removeprefix("www.")
    matched = None
    for contact in research.get("contacts", []):
        contact_channel = "email" if contact.get("kind") == "email" else "phone"
        host = (urlsplit(contact.get("source_url", "")).hostname or "").lower().removeprefix("www.")
        if (contact_channel == channel and str(contact.get("value", "")).strip().casefold() == recipient.casefold()
                and website_host and host == website_host and contact.get("account_id") in {None, account}):
            matched = contact
            break
    verified = matched is not None or (verified_contact and _source(source))
    contact_source = source.strip() if verified_contact and _source(source) else (matched or {}).get("source_url", "unknown")
    policy = config.policy
    allowed = (verified and _known(basis) and _source(contact_source)
        and policy.cold_contact_allowed and _known(policy.market) and _known(policy.provider_policy)
        and policy.provider_policy.casefold() not in {"blocked", "forbidden", "disallowed"})
    blocked = (matched or {}).get("permission_status") == "blocked"
    permission = {"status": "blocked" if blocked else "allowed" if allowed else "unknown",
        "scope": f"company_applicability:{opportunity_id}:{channel}:{recipient}",
        "basis": basis.strip() if _known(basis) else "unknown", "source": contact_source,
        "policy_version": policy.version,
        "conditions": ["One initial applicability message for this service, recipient and channel; separate user approval required"]}
    identifier = hashlib.sha256(f"{account}:{opportunity_id}:{channel}:{recipient}".encode()).hexdigest()
    conversation_id = "company_" + hashlib.sha256(f"{account}:{opportunity_id}".encode()).hexdigest()[:32]
    eligibility = {"account_id": account, "campaign_id": opportunity.get("campaign_id", "default"),
        "conversation_id": conversation_id, "recipient": recipient, "channel": channel}
    if store.is_suppressed(eligibility):
        raise ValueError("An active suppression blocks preparation of another sales contact")
    if store.pause_reason(eligibility):
        raise ValueError("A persistent pause blocks new contact proposals; research and inbox processing remain available")
    # Serialize company-attempt selection across independent workers before a
    # conversation is created. Identical retries share both conversation and key.
    store.db.execute("BEGIN IMMEDIATE")
    try:
        active = [c for c in store.list_conversations() if not c.get("is_terminal") and
            (c.get("account_id") == account or recipient.casefold() == str(c.get("recipient", "")).casefold()
             or recipient.casefold() in {str(v).casefold() for v in c.get("participants", [])})]
        for candidate in active:
            if candidate.get("opportunity_id") != opportunity_id or str(candidate.get("recipient", recipient)).casefold() != recipient.casefold() or candidate.get("channel", "email") != channel:
                raise ValueError("Another active company/request conversation exists; review it instead of creating a parallel first contact")
        if len(active) > 1:
            raise ValueError("Ambiguous company conversations require manual review")
        conv = active[0] if active else None
        if conv:
            conversation_id = conv["id"]
            if store.pause_reason({**eligibility, "conversation_id": conversation_id}):
                raise ValueError("A persistent pause or human takeover blocks new contact proposals")
            attempted = [a for a in store.list_actions() if a.get("conversation_id") == conversation_id and a.get("kind") == "initial" and a.get("status") in {"sending", "sent", "simulated_sent", "uncertain", "sent_confirmed_by_user", "failed"}]
            if attempted:
                store.db.commit()
                return attempted[0]
            if conv.get("stage", "new") != "new" or conv.get("last_inbound_id"):
                raise ValueError("This conversation already has a reply; use its next action instead of another first contact")
        else:
            if store.get_conversation(conversation_id):
                raise ValueError("This company attempt is closed; an explicit new opportunity or owner review is required")
            conv = {"id": conversation_id, "account_id": account, "opportunity_id": opportunity_id,
                "campaign_id": opportunity.get("campaign_id", "default"), "recipient": recipient,
                "participants": [recipient], "channel": channel, "mailbox_id": config.inbox.mailbox_id,
                "service_id": service_id, "task": opportunity.get("task", "unknown"),
                "context_version": 1, "stats_version": 0, "stage": "new", "holds": []}
        if conv.get("contact_permission") is not None and conv.get("contact_permission") != permission:
            conv["context_version"] = int(conv.get("context_version", 1)) + 1
        conv["contact_permission"] = permission
        holds = set(conv.get("holds", [])) - {"contact_policy"}
        if permission["status"] != "allowed":
            holds.add("contact_policy")
        conv.update(holds=sorted(holds), next_action="waiting_for_user_approval", interest="unconfirmed")
        store.db.execute("INSERT INTO workflow_conversations VALUES (?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload", (conversation_id, encode(conv)))
        store.db.commit()
    except BaseException:
        store.db.rollback()
        raise
    from .action_policy import cost_policy_version
    existing_action = store.get_action("contact_" + identifier[:32])
    if (existing_action and existing_action.get("contact_permission") == permission
            and existing_action.get("dependency_version") == catalog.dependency_version(service_id)
            and existing_action.get("context_version") == conv["context_version"]
            and existing_action.get("stats_version", 0) == conv["stats_version"]
            and existing_action.get("sender_account") == config.outreach.from_email
            and existing_action.get("cost_policy_version") == cost_policy_version(config)):

        return existing_action
    from .action_policy import evaluate_action
    from .company_stats import get_company_stats
    preparation = evaluate_action({"kind": "prepare_draft", "verified_free": True,
        "goal": "Prepare one bounded applicability draft from existing research",
        "context_version": conv["context_version"]},
        {"context_version": conv["context_version"]}, get_company_stats(store, account), config.budget)
    if not preparation.allowed:
        raise ValueError("Draft preparation requires review: " + "; ".join(preparation.reasons))
    plan = plan_contact(opportunity, {"value": recipient, "channel": channel, "permission": permission}, conv)
    plan["opportunity_id"] = opportunity_id
    evidence = research.get("evidence", [])
    if not evidence:
        namespace = "research:" + hashlib.sha256(encode(research).encode()).hexdigest()
        evidence = [{"id": namespace + ":" + f["id"], "research_version": namespace, "kind": "observation",
            "quote": f["quote"], "source_url": f["source_url"], "account_id": account,
            "freshness": "unknown"} for f in research.get("facts", [])]
    draft = build_draft(plan, evidence, offer, conv, catalog.sender_profile).model_dump(mode="json")
    website = research.get("website", "")
    if website and not _source(website):
        website = ""
    if website:
        draft["body"] = draft["body"].replace("Предлагаем проверить применимость одного сценария автоматизации.",
            "Обращаемся по информации на вашем сайте: " + website + ". Предлагаем проверить применимость одного сценария автоматизации.")
    action = {**draft, "id": "contact_" + identifier[:32], "message_key": identifier,
        "conversation_id": conversation_id, "opportunity_id": opportunity_id, "account_id": account,
        "mailbox_id": config.inbox.mailbox_id, "campaign_id": opportunity.get("campaign_id", "default"),
        "sender_account": config.outreach.from_email, "source_url": website or contact_source,
        "contact_permission": permission, "contact_policy_snapshot": config.policy.model_dump(mode="json"), "contact_verified": verified,
        "contact_verification_source": contact_source, "contact_role": (matched or {}).get("role", "unknown"),
        "catalog_version": catalog.version, "dependency_version": catalog.dependency_version(service_id),
        "service_id": service_id, "offer_snapshot": offer.model_dump(mode="json"),
        "sender_snapshot": catalog.sender_profile.model_dump(mode="json"), "evidence_snapshot": evidence,
        "evidence_refs": [e["id"] for e in evidence], "unknowns": opportunity.get("unknowns", []),
        "origin": "company_research", "interest": "unconfirmed", "requires_fresh_sync": False,
        "cost_policy_version": cost_policy_version(config), "is_new_opportunity": True,
        "capacity_full": config.capacity.max_active_opportunities is not None and len([c for c in store.list_conversations() if not c.get("is_terminal")]) > config.capacity.max_active_opportunities}
    if config.policy.contact_hours_start is not None:
        action["contact_window"] = {"start": f"{config.policy.contact_hours_start:02d}:00",
            "end": f"{config.policy.contact_hours_end if config.policy.contact_hours_end is not None else 23:02d}:59",
            "timezone": config.policy.timezone}
    stamp_draft_content(action)
    action["preparation_assessment"] = preparation.model_dump(mode="json")
    action["validation_results"] = validate_draft(action, evidence, offer, catalog.proof_assets, require_provenance=True)
    action["assessment"] = assess_current_action(store, action, config).model_dump(mode="json")
    existing = store.get_action(action["id"])
    if existing:
        comparable = {k: v for k, v in action.items() if existing.get(k) != v}
        if comparable:
            return store.update_action(action["id"], **comparable)
        return existing
    store.record_event("contact_prepared", conversation_id, {"opportunity_id": opportunity_id,
        "account_id": account, "recipient": recipient, "channel": channel,
        "verified": verified, "permission": permission["status"], "source": contact_source},
        event_id="contact-prepared:" + identifier)
    return store.queue_action(action)
