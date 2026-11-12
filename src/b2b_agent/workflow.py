"""Orchestration shared by the legacy CLI and the operator action queue."""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import httpx

from .catalog import load_legacy_catalog, load_service_catalog, with_legacy_sender_defaults
from .config import Config
from .state import WorkflowStore


def current_catalog(config: Config):
    # Absence supports pre-v2 research configs. A present but broken INPUT must
    # fail closed, rather than silently restore old prices or disclosure rights.
    if Path(config.input_path).exists():
        return with_legacy_sender_defaults(load_service_catalog(config.input_path), config)
    return load_legacy_catalog(config)


def workflow_store(box):
    return box if isinstance(box, WorkflowStore) else WorkflowStore(":memory:", db=box.db)


def assess_current_action(store, action, config):
    from .action_policy import evaluate_action
    from .company_stats import get_company_stats
    from .memory import build_company_capsule

    account = action.get("account_id") or action.get("opportunity_id")
    stats = get_company_stats(store, account)
    capsule = build_company_capsule(store, account, config.memory.capsule_tokens)
    stats_data = stats.model_dump(mode="json")
    conv = store.get_conversation(action.get("conversation_id", "")) or {}
    payload = {**action, "stats_version": stats_data.get("stats_version"),
               "contact_permission": (action.get("contact_permission", {}).get("status", "unknown")
                   if isinstance(action.get("contact_permission"), dict) else action.get("contact_permission", "unknown")),
               "paused": bool(store.pause_reason(action)), "suppressed": store.is_suppressed(action)}
    if payload.get("requires_fresh_sync"):
        payload["sync_fresh"] = store.sync_is_fresh(action.get("mailbox_id", "default"), config.inbox.freshness_minutes * 60)
    capsule_data = capsule.model_dump(mode="json")
    capsule_data["context_version"] = conv.get("context_version", action.get("context_version", 1))
    # Bounded templates use complete structured local state, without sending a
    # prompt or dropping critical fields. The conservative prompt bound is a
    # barrier for model use, not evidence that local data was lost.
    if not action.get("uses_model_context", False):
        capsule_data["hold_reason"] = None
        capsule_data["holds"] = [h for h in capsule_data.get("holds", []) if h != "context_budget"]
    return evaluate_action(payload, capsule_data, stats_data, config.budget)


def queue_public_request(box, result, config):
    store = workflow_store(box)
    logical = result.message_key
    opportunity_id, conversation_id = "opp_" + logical[:32], "conv_" + logical[:32]
    existing = next((a for a in store.list_actions() if a.get("message_key") == logical), None)
    if existing:
        return existing
    catalog = current_catalog(config)
    offer = catalog.service(result.workflow)
    dependencies = catalog.dependency_version(result.workflow)
    if not offer.enabled or (result.dependency_version and result.dependency_version != dependencies):
        raise ValueError("Catalog changed after inspection; inspect the request again before approval")
    research_version = "request_" + logical
    evidence = [{"id": research_version + ":need", "research_version": research_version,
                 "kind": "public_need", "quote": result.title,
                 "source_url": result.source_url, "author_id": result.recipient,
                 "published_at": str(result.published_on), "freshness": "fresh"},
                {"id": research_version + ":contact", "research_version": research_version,
                 "kind": "observation", "quote": result.contact_quote,
                 "source_url": result.source_url, "author_id": result.recipient}]
    store.put_opportunity({"id": opportunity_id, "account_id": None, "origin": "public_request",
        "request_id": logical, "task": result.title, "service_id": result.workflow,
        "status": "ready_for_contact", "need": {"status": "publicly_stated", "description": result.title},
        "task_fit": result.task_fit, "interest": "unconfirmed", "evidence": evidence,
        "unknowns": result.unknowns, "source_url": result.source_url})
    if not store.get_conversation(conversation_id):
        store.put_conversation({"id": conversation_id, "opportunity_id": opportunity_id,
            "account_id": None, "mailbox_id": config.inbox.mailbox_id,
            "recipient": result.recipient, "participants": [result.recipient],
            "service_id": result.workflow, "task": result.title, "subject": result.subject,
            "channel": result.channel, "source_url": result.source_url,
            "stage": "new", "context_version": 1, "stats_version": 0,
            "next_action": "waiting_for_user_approval", "unknowns": result.unknowns,
            "contact_permission": {"status": "allowed", "scope": "request:" + logical,
                "basis": "explicit reply destination for this request", "source": result.source_url,
                "policy_version": config.policy.version}})
    conv = store.get_conversation(conversation_id)
    action = {"opportunity_id": opportunity_id, "conversation_id": conversation_id,
        "account_id": None, "mailbox_id": config.inbox.mailbox_id, "channel": result.channel,
        "required_channel": result.channel,
        "kind": "initial", "service_id": result.workflow, "recipient": result.recipient,
        "sender_account": config.outreach.from_email, "subject": result.subject, "body": result.body,
        "source_url": result.source_url, "message_key": logical, "logical_request_id": logical,
        "attachments": [], "context_version": conv["context_version"], "stats_version": conv["stats_version"],
        "dependency_version": catalog.dependency_version(result.workflow), "catalog_version": catalog.version,
        "contact_permission": conv["contact_permission"], "goal": "Check relevance of one supported task",
        "evidence_refs": [e["id"] for e in evidence], "unknowns": result.unknowns,
        "offer_snapshot": offer.model_dump(mode="json"), "evidence_snapshot": evidence,
        "sender_snapshot": catalog.sender_profile.model_dump(mode="json"),
        "claims": [{"text": claim.text, "offer_id": offer.id, "kind": claim.kind}
                   for claim in offer.allowed_claims if claim.text in result.body],
        "requires_fresh_sync": False, "validation_results": [],
        "is_new_opportunity": True, "capacity_full": config.capacity.max_active_opportunities is not None
             and len([c for c in store.list_conversations() if not c.get("is_terminal")]) > config.capacity.max_active_opportunities}
    if config.policy.contact_hours_start is not None:
        action["contact_window"] = {"start": f"{config.policy.contact_hours_start:02d}:00",
            "end": f"{config.policy.contact_hours_end if config.policy.contact_hours_end is not None else 23:02d}:59",
            "timezone": config.policy.timezone}
    from .messaging import stamp_draft_content, validate_draft
    stamp_draft_content(action)
    action["validation_results"] = validate_draft(action, evidence, offer, catalog.proof_assets, require_provenance=True)
    assessment = assess_current_action(store, action, config)
    action["assessment"] = assessment.model_dump(mode="json")
    from .action_policy import cost_policy_version
    action["cost_policy_version"] = cost_policy_version(config)
    # Snapshots are durable and only written to the private SQLite working store.
    store.record_event("catalog_snapshot", opportunity_id, {"version": catalog.version,
        "snapshot": catalog.model_dump(mode="json")}, event_id="catalog:" + catalog.version)
    return store.queue_action(action)


def review_card(store, action, config=None):
    conv = store.get_conversation(action.get("conversation_id", "")) or {}
    return {"id": action["id"], "status": action["status"], "customer": action.get("account_id") or action.get("opportunity_id"),
        "recipient": action.get("recipient"), "channel": action.get("channel"),
        "event": action.get("inbound_event_id") or action.get("source_url"),
        "goal": action.get("goal"), "evidence": action.get("evidence_refs", []),
        "unknowns": action.get("unknowns", conv.get("unknowns", [])),
        "assessment": (assess_current_action(store, action, config).model_dump(mode="json")
                       if config else action.get("assessment", {})),
        "subject": action.get("subject", ""), "body": action.get("body", ""),
        "attachments": action.get("attachments", [])}


async def dispatch_action(box, action_id, config, client=None, *, simulated=False):
    from .mail_delivery import DeliveryRejected, deliver, gmail_access_token, preflight
    from .outbox import DispatchBlocked

    if client is None:
        async with httpx.AsyncClient(timeout=config.timeout_seconds) as owned:
            return await dispatch_action(box, action_id, config, owned, simulated=simulated)
    store = workflow_store(box)
    action = store.get_action(action_id)
    if not action:
        raise ValueError("Unknown action")
    catalog = current_catalog(config)
    deps = catalog.dependency_version(action["service_id"], action.get("proof_asset_ids"))
    if action.get("sender_account", "") != config.outreach.from_email:
        raise DispatchBlocked("sender_changed")
    if catalog.sender_profile.from_email and catalog.sender_profile.from_email != config.outreach.from_email:
        raise DispatchBlocked("sender_profile_mismatch")
    assessment = assess_current_action(store, action, config)
    if not assessment.allowed:
        raise DispatchBlocked("action_policy: " + "; ".join(assessment.reasons))
    from .action_policy import cost_policy_version
    if action.get("cost_policy_version", cost_policy_version(config)) != cost_policy_version(config):
        raise DispatchBlocked("policy_or_cost_changed: review the current card and approve again")
    approved_assessment = action.get("assessment", {})
    fresh_assessment = assessment.model_dump(mode="json")
    for key in ("incremental_cost", "contact_load", "reputational_risk"):
        if approved_assessment.get(key, fresh_assessment.get(key)) != fresh_assessment.get(key):
            raise DispatchBlocked("assessment_changed: review the current card and approve again")
    if action.get("attachments"):
        raise DispatchBlocked("Attachments require a supported validated delivery adapter")
    if config.unsubscribe.sync_required or (config.unsubscribe.mode == "https" and action.get("channel", "email") == "email"):
        import os

        from .unsubscribe import require_fresh_unsubscribe_sync, sync_unsubscribes
        sync_secret = os.environ.get(config.unsubscribe.sync_secret_env)
        sync_url = config.unsubscribe.sync_url or config.unsubscribe.public_base_url
        if not sync_secret or not sync_url:
            raise DispatchBlocked("Mandatory unsubscribe sync credentials or URL are missing")
        await sync_unsubscribes(client, store, sync_url, sync_secret)
        require_fresh_unsubscribe_sync(store, sync_url)
    from .messaging import validate_draft
    errors = validate_draft(action, action.get("evidence_snapshot", []),
                            catalog.service(action["service_id"]), catalog.proof_assets, require_provenance=True)
    if errors or action.get("validation_results"):
        raise DispatchBlocked("draft_validation: " + "; ".join(errors or action["validation_results"]))
    if action.get("channel", "email") != "email":
        from .channels import prepare_manual_send
        return prepare_manual_send(store, action_id, current_dependencies=deps)
    blocker = store.approval_blocker(action, current_dependencies=deps)
    if blocker:
        raise DispatchBlocked(blocker)
    token = None
    if not simulated:
        if not config.outreach.enabled:
            raise DispatchBlocked("Transport is disabled")
        preflight(config.outreach)
        if config.outreach.provider == "gmail":
            token = await gmail_access_token(config.outreach, client)
    # Build headers before reserving/consuming approval so missing HTTPS runtime
    # configuration leaves a reviewable held action, not an uncertain delivery.
    from .unsubscribe import delivery_unsubscribe, validate_prepared_unsubscribe
    if config.unsubscribe.mode == "https" or action.get("unsubscribe_material"):
        material = validate_prepared_unsubscribe(action, config)
    else:
        material = delivery_unsubscribe(config.unsubscribe, action["recipient"], config.outreach.from_email)
    unsubscribe_headers = material.get("headers", {})
    cost_reservation = None
    estimate = assessment.incremental_cost
    if not simulated and estimate.known and estimate.upper is not None and estimate.upper > 0:
        from .action_policy import BudgetLedger
        ledger = BudgetLedger(store.db)
        account_scope = "account:" + str(action.get("account_id") or action.get("opportunity_id"))
        campaign_scope = "campaign:" + str(action.get("campaign_id", "default"))
        run_scope = "dispatch:" + action_id
        limits = {account_scope: {"amount": config.budget.max_company_cost},
                  campaign_scope: {"amount": config.budget.max_campaign_cost},
                  run_scope: {"amount": config.budget.max_run_cost}}
        reservation = ledger.reserve(run_scope, estimate.upper, estimate.currency,
            [account_scope, campaign_scope, run_scope], limits)
        if reservation["reused"]:
            raise DispatchBlocked("The cost reservation was already used; review its outcome")
        cost_reservation = ledger, run_scope
    try:
        authorization = box.authorize_delivery(action_id, config.outreach.model_copy(update={"enabled": True}) if simulated else config.outreach, current_dependencies=deps,
            dependency_loader=lambda: current_catalog(config).dependency_version(action["service_id"], action.get("proof_asset_ids")))
    except Exception:
        if cost_reservation:
            cost_reservation[0].reconcile(cost_reservation[1], actual_amount=0, billing_known=True)
        raise
    try:
        authorization.bind_unsubscribe_headers(unsubscribe_headers)
        if simulated:
            authorization.assert_valid(action["recipient"], action["subject"], action["body"], action["message_key"])
            receipt = "simulated:" + hashlib.sha256(action_id.encode()).hexdigest()[:16]
            rfc = f"<{action['message_key']}@{(config.outreach.from_email or 'operator@example.invalid').split('@')[-1]}>"
            thread = None
        else:
            conv = store.get_conversation(action.get("conversation_id", "")) or {}
            receipt = await deliver(config.outreach, client, action["recipient"], action["subject"], action["body"], action["message_key"], gmail_token=token,
                authorization=authorization, thread_id=conv.get("provider_thread_id"),
                in_reply_to=action.get("in_reply_to"), references=action.get("references", []),
                original_subject=action.get("original_subject"), unsubscribe_headers=unsubscribe_headers)
            rfc, thread = receipt.rfc_message_id, receipt.thread_id
        status = "simulated_sent" if simulated else "sent"
        if cost_reservation:
            cost_reservation[0].reconcile(cost_reservation[1], billing_known=False)
        box.finish(action["message_key"], status, str(receipt))
        store.update_action(action_id, status=status, provider_id=str(receipt), rfc_message_id=rfc,
            provider_thread_id=thread, simulation=simulated)
        conv = store.get_conversation(action.get("conversation_id", "")) or {}
        if conv:
            conv.update(last_outbound_id=action["message_key"], last_outbound_rfc_id=rfc,
                rfc_message_ids=list(dict.fromkeys(conv.get("rfc_message_ids", []) + [rfc])),
                last_outbound_at=datetime.now(UTC).isoformat(),
                stage="outreach_sent" if action.get("kind") == "initial" else conv.get("stage", "exploring"),
                next_action="waiting_for_customer")
            if action.get("business_action") == "propose_meeting":
                conv["stage"] = "meeting_proposed"
                store.record_event("meeting_proposed", conv["id"], {"action_id": action_id,
                    "message_ref": rfc, "simulation": simulated, "goal": action.get("goal")},
                    event_id="meeting-proposed:" + action_id)
            questions = [line.strip() for line in action["body"].splitlines() if "?" in line]
            if questions:
                conv["questions_asked"] = list(dict.fromkeys(conv.get("questions_asked", []) + questions))
                conv["question_sources"] = conv.get("question_sources", []) + [{"question": q, "message_ref": rfc} for q in questions]
            if thread:
                conv["provider_thread_id"] = thread
            store.put_conversation(conv)
        store.record_event(status, action.get("conversation_id", action_id), {"action_id": action_id,
            "kind": action.get("kind"), "recipient": action["recipient"], "rfc_message_id": rfc})
        return store.get_action(action_id)
    except Exception as error:
        if cost_reservation:
            cost_reservation[0].reconcile(cost_reservation[1], billing_known=False)
        status = "failed" if isinstance(error, (DeliveryRejected, DispatchBlocked)) else "uncertain"
        box.finish(action["message_key"], status, error=type(error).__name__)
        store.update_action(action_id, status=status, error=type(error).__name__)
        raise


def refresh_action(box, action_id, config):
    """Regenerate the same unattempted logical action from current owner data."""
    from .action_policy import cost_policy_version
    from .conversations import classify_reply
    from .messaging import (
        build_draft,
        prepare_reply_draft,
        price_answer,
        stamp_draft_content,
        validate_draft,
    )

    store = workflow_store(box)
    old = store.get_action(action_id)
    if not old or old["status"] in {"sending", "sent", "simulated_sent", "uncertain", "sent_confirmed_by_user", "failed", "bounced"}:
        raise ValueError("Only unattempted actions can be regenerated")
    conv = store.get_conversation(old.get("conversation_id", "")) or {}
    if conv.get("is_terminal") or store.is_suppressed(old):
        raise ValueError("Suppression or terminal context prevents another contact attempt")
    catalog = current_catalog(config)
    offer = catalog.service(old["service_id"])
    if not offer.enabled:
        raise ValueError("The selected service is disabled")
    if old.get("kind", "initial") == "initial":
        if conv.get("last_inbound_id"):
            raise ValueError("A new inbound requires a reply plan, not another first pitch")
        plan = {"opportunity_id": old.get("opportunity_id"), "service_id": offer.id,
            "origin": (store.get_opportunity(old.get("opportunity_id", "")) or {}).get("origin", "public_request"),
            "recipient": old["recipient"], "channel": old.get("channel", "email"),
            "task_fit": (store.get_opportunity(old.get("opportunity_id", "")) or {}).get("task_fit", "unknown"),
            "goal": old.get("goal")}
        payload = build_draft(plan, old.get("evidence_snapshot", []), offer, conv, catalog.sender_profile).model_dump(mode="json")
        payload["body"] += "\n\n" + price_answer(offer)
        payload["subject"] = old["subject"]
        stamp_draft_content(payload)
    elif old.get("kind") == "reply":
        inbound = next((e for e in store.list_inbound(conv["id"]) if e.get("id") == conv.get("last_inbound_id")), None)
        if not inbound:
            raise ValueError("The current inbound source is unavailable")
        payload = prepare_reply_draft(conv, classify_reply(inbound), catalog, inbound)
        if payload is None:
            raise ValueError("Current context requires human review rather than a message")
    else:
        raise ValueError("Edit the proposed follow-up explicitly and review its content")
    new = {**old, **payload, "id": old["id"], "action_id": old["id"], "message_key": old["message_key"],
        "created_at": old["created_at"], "status": "pending_approval", "context_version": conv.get("context_version", 1),
        "stats_version": conv.get("stats_version", 0), "dependency_version": catalog.dependency_version(offer.id, payload.get("proof_asset_ids")),
        "catalog_version": catalog.version, "sender_account": catalog.sender_profile.from_email,
        "offer_snapshot": offer.model_dump(mode="json"), "sender_snapshot": catalog.sender_profile.model_dump(mode="json"),
        "cost_policy_version": cost_policy_version(config)}
    new.pop("approval_id", None)
    new["validation_results"] = validate_draft(new, new.get("evidence_snapshot", []), offer, catalog.proof_assets, require_provenance=True)
    new["assessment"] = assess_current_action(store, new, config).model_dump(mode="json")
    return store.update_action(action_id, **{k: v for k, v in new.items() if k != "id"})
