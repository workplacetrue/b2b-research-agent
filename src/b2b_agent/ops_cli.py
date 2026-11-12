"""Operator controls; none of these commands implicitly approve an outgoing message."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

from .config import Config, load_config
from .outbox import Outbox
from .state import WorkflowStore

COMMANDS = {"validate-input", "reload-catalog", "import-replies", "sync-replies", "review-actions",
    "approve-action", "reject-action", "edit-action", "defer-action", "send-action", "stats",
    "company-stats", "report", "meeting-brief", "mark-meeting", "suppress", "pause", "resume",
    "takeover", "tick", "review-due", "inspect-request", "refresh-action", "prepare-contact", "prepare-unsubscribe", "prepare-followup", "confirm-manual-send"}


def register(sub):
    for name in sorted(COMMANDS):
        p = sub.add_parser(name)
        p.add_argument("--db", default="output/research.sqlite")
        p.add_argument("--config", default="config.json")
        p.add_argument("--output", default="output")
        if name in {"validate-input", "reload-catalog"}:
            p.add_argument("--input")
        if name == "import-replies":
            p.add_argument("path")
            p.add_argument("--mailbox-id")
        if name in {"approve-action", "reject-action", "edit-action", "defer-action", "send-action", "confirm-manual-send", "refresh-action", "prepare-unsubscribe"}:
            p.add_argument("action_id")
        if name in {"approve-action", "reject-action", "mark-meeting", "confirm-manual-send"}:
            p.add_argument("--actor", required=True)
        if name == "reject-action":
            p.add_argument("--reason", default="operator rejected")
        if name == "edit-action":
            p.add_argument("--body-file")
            p.add_argument("--subject")
        if name == "defer-action":
            p.add_argument("--until", required=True)
        if name == "send-action":
            p.add_argument("--simulate", action="store_true", help="Offline simulation only; no email or OAuth calls")
        if name == "company-stats":
            p.add_argument("account_id")
        if name in {"meeting-brief", "mark-meeting", "prepare-followup"}:
            p.add_argument("conversation_id")
        if name == "mark-meeting":
            p.add_argument("--event", required=True, choices=["proposed", "scheduled", "held", "no_show", "cancelled"])
            p.add_argument("--local-time")
            p.add_argument("--timezone")
            p.add_argument("--confirmed", action="store_true")
        if name == "prepare-followup":
            p.add_argument("--reason", required=True)
            p.add_argument("--due-at")
        if name == "suppress":
            p.add_argument("--scope", choices=["contact", "channel", "account", "domain"], required=True)
            p.add_argument("--target", required=True)
            p.add_argument("--channel", default="")
            p.add_argument("--reason", required=True)
        if name in {"pause", "resume", "takeover"}:
            p.add_argument("--scope", choices=["global", "campaign", "account", "conversation", "human_takeover"], default="global")
            p.add_argument("--target", default="*")
            p.add_argument("--reason", default="operator control")
        if name == "prepare-contact":
            p.add_argument("opportunity_id")
            p.add_argument("--recipient", required=True)
            p.add_argument("--channel", default="email")
            p.add_argument("--verified-contact", action="store_true")
            p.add_argument("--basis", default="")
            p.add_argument("--source", default="")
        if name == "inspect-request":
            p.add_argument("--html", required=True)
            p.add_argument("--url", required=True)
            p.add_argument("--now", help="Explicit ISO clock for reproducible offline fixture inspection")


def printable(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def emit(value):
    print(json.dumps(printable(value), ensure_ascii=False, indent=2, default=str))


def handle(args):
    config = load_config(args.config) if Path(args.config).exists() else Config()
    from .workflow import current_catalog, review_card
    if args.command in {"validate-input", "reload-catalog"}:
        from .catalog import load_service_catalog
        catalog = load_service_catalog(args.input or config.input_path)
        emit({"valid": True, "version": catalog.version, "services": [o.id for o in catalog.services],
              "public_case_status": catalog.public_case_status})
        return 0
    box = Outbox(args.db)
    store = WorkflowStore(":memory:", db=box.db)
    try:
        cmd = args.command
        if cmd == "inspect-request":
            from datetime import datetime

            from .crawler import Page
            from .outreach import inspect_ad
            from .workflow import queue_public_request
            page = Page(args.url, "", Path(args.html).read_text(encoding="utf-8"), [])
            result = inspect_ad(page, config, datetime.fromisoformat(args.now) if args.now else None)
            if result.status == "ready":
                emit(queue_public_request(box, result, config))
            else:
                emit(result)
                return 2
        elif cmd == "review-actions":
            emit([review_card(store, a, config) for a in store.list_actions() if a["status"] in {"pending_approval", "approved", "deferred", "awaiting_manual_send"}])
        elif cmd == "approve-action":
            action = store.get_action(args.action_id)
            if not action:
                raise ValueError("Unknown action")
            from .workflow import assess_current_action
            assessment = assess_current_action(store, action, config)
            if not assessment.allowed:
                raise ValueError("Action requires review: " + "; ".join(assessment.reasons))
            from .action_policy import cost_policy_version
            from .messaging import stamp_draft_content, validate_draft
            if action.get("content_review_required"):
                stamp_draft_content(action, source="owner_review", actor=args.actor)
                action["content_review_required"] = False
            catalog = current_catalog(config)
            action["validation_results"] = validate_draft(action, action.get("evidence_snapshot", []),
                catalog.service(action["service_id"]), catalog.proof_assets, require_provenance=True)
            if action["validation_results"]:
                raise ValueError("Draft validation failed: " + "; ".join(action["validation_results"]))
            updates = {"assessment": printable(assessment), "cost_policy_version": cost_policy_version(config),
                "content_review_required": action.get("content_review_required", False),
                "content_validation": action.get("content_validation", {}), "validation_results": action["validation_results"]}
            store.update_action(args.action_id, **updates)
            # The exact text is available through review-actions. This command is
            # intentionally one ID; there is no approve-all operation.
            emit(store.approve_action(args.action_id, args.actor, config.approval_ttl_hours * 3600))
        elif cmd == "prepare-unsubscribe":
            from .unsubscribe import prepare_action_unsubscribe
            async def prepare_unsubscribe():
                async with httpx.AsyncClient(timeout=config.timeout_seconds) as client:
                    return await prepare_action_unsubscribe(store, args.action_id, config, client)
            emit(asyncio.run(prepare_unsubscribe()))
        elif cmd == "prepare-contact":
            from .contact_preparation import prepare_company_contact
            emit(prepare_company_contact(box, args.opportunity_id, args.recipient, args.channel, config,
                verified_contact=args.verified_contact, basis=args.basis, source=args.source))
        elif cmd == "refresh-action":
            from .workflow import refresh_action
            emit(refresh_action(box, args.action_id, config))
        elif cmd == "reject-action":
            store.reject_action(args.action_id, args.actor, args.reason)
            emit({"status": "rejected", "action_id": args.action_id})
        elif cmd == "edit-action":
            changes = {}
            if args.body_file:
                changes["body"] = Path(args.body_file).read_text(encoding="utf-8")
            if args.subject is not None:
                changes["subject"] = args.subject
            if not changes:
                raise ValueError("Supply --body-file or --subject")
            emit(store.update_action(args.action_id, **changes, content_validation={}, content_review_required=True))
        elif cmd == "defer-action":
            from datetime import datetime
            due = datetime.fromisoformat(args.until)
            if due.tzinfo is None:
                raise ValueError("--until needs an explicit timezone")
            emit(store.update_action(args.action_id, status="deferred", due_at=due.isoformat()))
        elif cmd == "send-action":
            from .workflow import dispatch_action
            emit(asyncio.run(dispatch_action(box, args.action_id, config, simulated=args.simulate)))
        elif cmd == "confirm-manual-send":
            from .channels import confirm_manual_sent
            action = store.get_action(args.action_id)
            if not action:
                raise ValueError("Unknown action")
            catalog = current_catalog(config)
            emit(confirm_manual_sent(store, args.action_id, actor=args.actor,
                current_dependencies=catalog.dependency_version(action["service_id"], action.get("proof_asset_ids"))))
        elif cmd == "import-replies":
            from .conversations import apply_inbound_event
            from .inbox import import_replies
            mailbox = args.mailbox_id or config.inbox.mailbox_id
            events = import_replies(args.path, mailbox_id=mailbox,
                operator_addresses=[config.outreach.from_email] if config.outreach.from_email else [])
            catalog = current_catalog(config)
            store.mark_sync(mailbox, success=False)
            try:
                updates = [apply_inbound_event(store, event, catalog, config) for event in events]
                store.mark_sync(mailbox, success=True)
            except Exception:
                store.mark_sync(mailbox, success=False)
                raise
            emit(updates)
        elif cmd == "sync-replies":
            if not config.inbox.enabled or config.inbox.provider != "gmail":
                raise ValueError("Enable an explicitly bounded Gmail inbox adapter; SMTP reads replies through import-replies")
            from .conversations import apply_inbound_event
            from .inbox import GmailInboxProvider
            from .mail_delivery import gmail_access_token
            async def sync():
                async with httpx.AsyncClient(timeout=config.timeout_seconds) as client:
                    token = await gmail_access_token(config.outreach, client)
                    row = store.db.execute("SELECT cursor FROM workflow_sync WHERE mailbox_id=?", (config.inbox.mailbox_id,)).fetchone()
                    provider = GmailInboxProvider(client, token, mailbox_id=config.inbox.mailbox_id,
                        query=config.inbox.query,
                        known_thread_ids=[c["provider_thread_id"] for c in store.list_conversations() if c.get("provider_thread_id")],
                        max_messages=config.inbox.max_messages,
                        max_pages=config.inbox.max_pages,
                        operator_addresses=[config.outreach.from_email])
                    try:
                        store.mark_sync(config.inbox.mailbox_id, success=False)
                        batch = await provider.sync(row[0] if row else None)
                        catalog = current_catalog(config)
                        # Partial bounded batches remain stale for sending until
                        # all pages (including potential opt-outs) are applied.
                        store.mark_sync(config.inbox.mailbox_id, success=False)
                        updates = [apply_inbound_event(store, event, catalog, config) for event in batch.messages]
                        store.mark_sync(config.inbox.mailbox_id, success=not batch.has_more, cursor=batch.cursor)
                        return {"updates": updates, "history_reset": batch.history_reset,
                                "has_more": batch.has_more, "sync_complete": not batch.has_more}
                    except Exception:
                        store.mark_sync(config.inbox.mailbox_id, success=False)
                        raise
            emit(asyncio.run(sync()))
        elif cmd in {"stats", "company-stats", "report"}:
            from .company_stats import export_stats, get_company_stats, list_company_stats
            if cmd == "company-stats":
                emit(get_company_stats(store, args.account_id))
            elif cmd == "stats":
                emit([printable(x) for x in list_company_stats(store)])
                export_stats(store, str(Path(args.output) / "STATS"))
            else:
                emit(build_report(store))
        elif cmd == "meeting-brief":
            from .meetings import build_meeting_brief
            brief = build_meeting_brief(store, args.conversation_id, current_catalog(config))
            Path(args.output).mkdir(parents=True, exist_ok=True)
            target = Path(args.output) / ("meeting-" + _safe_id(args.conversation_id) + ".json")
            target.write_text(json.dumps(printable(brief), ensure_ascii=False, indent=2), encoding="utf-8")
            emit({"path": str(target), "brief": printable(brief)})
        elif cmd == "mark-meeting":
            from .meetings import mark_meeting
            emit(mark_meeting(store, args.conversation_id, args.event, local_time=args.local_time,
                timezone=args.timezone, actor=args.actor, confirmed=args.confirmed))
        elif cmd == "prepare-followup":
            from .conversations import prepare_followup
            emit(prepare_followup(store, args.conversation_id, current_catalog(config), config,
                reason=args.reason, due_at=args.due_at))
        elif cmd == "suppress":
            store.suppress(args.scope, args.target, args.reason, args.channel)
            emit({"suppressed": True, "scope": args.scope, "target": args.target})
        elif cmd in {"pause", "resume", "takeover"}:
            scope = "human_takeover" if cmd == "takeover" else args.scope
            if scope == "human_takeover" and args.target == "*":
                raise ValueError("Human takeover requires --target conversation_id")
            if cmd == "resume":
                store.resume(scope, args.target)
                if scope in {"human_takeover", "conversation"}:
                    conversation = store.get_conversation(args.target)
                    if conversation and "human_handoff" in conversation.get("holds", []):
                        conversation["holds"] = [h for h in conversation["holds"] if h != "human_handoff"]
                        conversation["context_version"] = conversation.get("context_version", 1) + 1
                        store.put_conversation(conversation)
            else:
                store.pause(scope, args.target, args.reason)
            emit({"operation": cmd, "scope": scope, "target": args.target})
        elif cmd in {"tick", "review-due"}:
            from .workflow import assess_current_action
            # Bounded, local and deterministic. A tick never sends or generates
            # another LLM response for an unchanged pending approval.
            rows = []
            for action in store.list_actions()[: config.max_companies * config.budget.max_drafts_per_conversation]:
                conv = store.get_conversation(action.get("conversation_id", "")) or {}
                if conv.get("is_terminal") or action["status"] not in {"pending_approval", "approved", "deferred"}:
                    continue
                assessment = assess_current_action(store, action, config)
                rows.append({"action_id": action["id"], "assessment": printable(assessment)})
            emit(rows)
        return 0
    finally:
        box.close()


def _safe_id(value):
    import hashlib
    return hashlib.sha256(value.encode()).hexdigest()[:20]


def build_report(store):
    from .company_stats import build_report as report
    return report(store)
