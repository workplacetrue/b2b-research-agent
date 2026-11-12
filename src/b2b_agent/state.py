"""Persistent operator workflow. External text can never grant an approval."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


def utcnow() -> datetime:
    return datetime.now(UTC)


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def digest(value: Any) -> str:
    return hashlib.sha256(encode(value).encode()).hexdigest()


def action_fingerprint(action: dict) -> str:
    """Bind all proposed content, recipient, sender and justification versions."""
    excluded = {"status", "approval_id", "approved_at", "updated_at", "provider_id", "error",
                "delivery_status", "attempted_at", "last_checked_at"}
    return digest({key: value for key, value in action.items() if key not in excluded})



def time_blocker(action: dict, now: datetime | None = None) -> str | None:
    now = now or utcnow()
    permission = action.get("contact_permission", {})
    expires = permission.get("expires_at") if isinstance(permission, dict) else None
    if expires and datetime.fromisoformat(expires) <= now:
        return "contact_policy_expired"
    due = action.get("due_at")
    if due and datetime.fromisoformat(due) > now:
        return "not_due"
    window = action.get("contact_window")
    if window:
        if not window.get("timezone"):
            return "contact_window_unknown"
        local = now.astimezone(ZoneInfo(window["timezone"]))
        clock = local.strftime("%H:%M")
        start, end = window.get("start", "00:00"), window.get("end", "23:59")
        within = start <= clock <= end if start <= end else clock >= start or clock <= end
        if not within or local.weekday() not in window.get("weekdays", list(range(7))):
            return "outside_contact_window"
    return None

def initialize(db: sqlite3.Connection) -> None:
    db.executescript("""
      CREATE TABLE IF NOT EXISTS workflow_opportunities(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS workflow_conversations(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS workflow_inbound(
        id TEXT PRIMARY KEY,mailbox_id TEXT NOT NULL,provider_message_id TEXT NOT NULL,
        conversation_id TEXT,payload TEXT NOT NULL,UNIQUE(mailbox_id,provider_message_id));
      CREATE TABLE IF NOT EXISTS workflow_actions(
        id TEXT PRIMARY KEY,conversation_id TEXT,status TEXT NOT NULL,payload TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS workflow_events(
        id TEXT PRIMARY KEY,kind TEXT NOT NULL,entity_id TEXT NOT NULL,payload TEXT NOT NULL,
        created_at TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS workflow_approvals(
        id TEXT PRIMARY KEY,action_id TEXT NOT NULL,payload TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS workflow_pauses(
        scope TEXT NOT NULL,target TEXT NOT NULL,reason TEXT NOT NULL,PRIMARY KEY(scope,target));
      CREATE TABLE IF NOT EXISTS workflow_sync(
        mailbox_id TEXT PRIMARY KEY,succeeded_at TEXT,success INTEGER NOT NULL,cursor TEXT);
      CREATE TABLE IF NOT EXISTS workflow_suppressions(
        scope TEXT NOT NULL,target TEXT NOT NULL,channel TEXT NOT NULL DEFAULT '',
        reason TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(scope,target,channel));
      CREATE TABLE IF NOT EXISTS workflow_schema_versions(version INTEGER PRIMARY KEY,applied_at TEXT NOT NULL);
    """)
    db.execute("INSERT OR IGNORE INTO workflow_schema_versions VALUES (2,?)", (utcnow().isoformat(),))
    db.commit()


class WorkflowStore:
    def __init__(self, path: str | Path, *, db: sqlite3.Connection | None = None):
        if db is None:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = db or sqlite3.connect(str(path), timeout=30)
        self.db.row_factory = sqlite3.Row
        self._owns_db = db is None
        initialize(self.db)

    def close(self):
        if self._owns_db:
            self.db.close()

    def _get(self, table: str, identifier: str) -> dict | None:
        row = self.db.execute(f"SELECT payload FROM {table} WHERE id=?", (identifier,)).fetchone()
        return json.loads(row[0]) if row else None

    def _list(self, table: str) -> list[dict]:
        return [json.loads(row[0]) for row in self.db.execute(f"SELECT payload FROM {table} ORDER BY rowid")]

    def put_opportunity(self, data: dict) -> str:
        data = dict(data)
        data.setdefault("id", "opp_" + uuid.uuid4().hex)
        with self.db:
            self.db.execute("INSERT INTO workflow_opportunities VALUES (?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload", (data["id"], encode(data)))
        return data["id"]

    def get_opportunity(self, identifier: str) -> dict | None:
        return self._get("workflow_opportunities", identifier)

    def list_opportunities(self) -> list[dict]:
        return self._list("workflow_opportunities")

    def put_conversation(self, data: dict) -> str:
        data = dict(data)
        data.setdefault("id", "conv_" + uuid.uuid4().hex)
        data.setdefault("context_version", 1)
        data.setdefault("stats_version", 0)
        data.setdefault("stage", "new")
        data.setdefault("holds", [])
        with self.db:
            self.db.execute("INSERT INTO workflow_conversations VALUES (?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload", (data["id"], encode(data)))
        return data["id"]

    def get_conversation(self, identifier: str) -> dict | None:
        return self._get("workflow_conversations", identifier)

    def list_conversations(self) -> list[dict]:
        return self._list("workflow_conversations")

    def list_inbound(self, conversation_id: str | None = None) -> list[dict]:
        sql, params = "SELECT payload FROM workflow_inbound", ()
        if conversation_id is not None:
            sql, params = sql + " WHERE conversation_id=?", (conversation_id,)
        return [json.loads(row[0]) for row in self.db.execute(sql + " ORDER BY rowid", params)]

    def record_event(self, kind: str, entity_id: str, payload: dict, event_id: str | None = None) -> str:
        identifier = event_id or "evt_" + uuid.uuid4().hex
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO workflow_events VALUES (?,?,?,?,?)", (identifier, kind, entity_id, encode(payload), utcnow().isoformat()))
        return identifier

    def list_events(self, entity_id: str | None = None) -> list[dict]:
        sql, params = "SELECT * FROM workflow_events", ()
        if entity_id is not None:
            sql, params = sql + " WHERE entity_id=?", (entity_id,)
        return [{"id": r["id"], "kind": r["kind"], "entity_id": r["entity_id"], "created_at": r["created_at"], **json.loads(r["payload"])} for r in self.db.execute(sql + " ORDER BY rowid", params)]

    def queue_action(self, payload: dict) -> dict:
        data = dict(payload)
        identifier = data.get("id") or data.get("action_id")
        if identifier is None:
            # Logical event/message key ensures a repeated tick does not create another approval card.
            logical = data.get("message_key") or data.get("inbound_event_id") or data.get("scheduled_action_id")
            identifier = "act_" + (digest([data.get("conversation_id"), logical, data.get("kind", "initial")])[:32] if logical else uuid.uuid4().hex)
        existing = self.get_action(identifier)
        if existing:
            return existing
        data.update(id=identifier, action_id=identifier, status="pending_approval")
        data.setdefault("message_key", digest(identifier))
        data.setdefault("channel", "email")
        data.setdefault("kind", "initial")
        data.setdefault("attachments", [])
        data.setdefault("created_at", utcnow().isoformat())
        conv = self.get_conversation(data.get("conversation_id", "")) or {}
        data.setdefault("context_version", conv.get("context_version", 1))
        data.setdefault("stats_version", conv.get("stats_version", 0))
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO workflow_actions VALUES (?,?,?,?)", (identifier, data.get("conversation_id"), data["status"], encode(data)))
        return self.get_action(identifier)

    def get_action(self, identifier: str) -> dict | None:
        return self._get("workflow_actions", identifier)

    def list_actions(self, status: str | None = None) -> list[dict]:
        if status is None:
            return self._list("workflow_actions")
        return [json.loads(row[0]) for row in self.db.execute("SELECT payload FROM workflow_actions WHERE status=? ORDER BY rowid", (status,))]

    def _write_action(self, data: dict) -> None:
        self.db.execute("UPDATE workflow_actions SET status=?,payload=? WHERE id=?", (data["status"], encode(data), data["id"]))

    def update_action(self, identifier: str, **changes) -> dict:
        with self.db:
            data = self.get_action(identifier)
            if data is None:
                raise KeyError(identifier)
            protected_statuses = {"sending", "sent", "uncertain", "simulated_sent", "sent_confirmed_by_user"}
            receipt_fields = {"status", "provider_id", "provider_message_id", "rfc_message_id", "provider_thread_id", "thread_id", "simulation", "error", "delivery_status", "updated_at", "sent_at"}
            if data.get("status") in protected_statuses and any(key not in receipt_fields and value != data.get(key) for key, value in changes.items()):
                raise ValueError("Attempted message content and evidence snapshots are immutable; create a new action")
            if data.get("status") in protected_statuses and changes.get("status", data["status"]) in {"pending_approval", "approved", "deferred"}:
                raise ValueError("An attempted delivery cannot be reset for automatic retry")
            old_fingerprint = action_fingerprint(data)
            data.update(changes)
            if action_fingerprint(data) != old_fingerprint:
                self._invalidate_approval(identifier)
                if data["status"] not in {"sent", "sending", "uncertain", "simulated_sent", "sent_confirmed_by_user", "failed", "bounced", "rejected", "cancelled"}:
                    data["status"] = "pending_approval"
            self._write_action(data)
        return data

    def _invalidate_approval(self, identifier: str) -> None:
        for row in self.db.execute("SELECT id,payload FROM workflow_approvals WHERE action_id=?", (identifier,)).fetchall():
            approval = json.loads(row["payload"])
            if approval.get("status") != "active":
                continue
            approval["status"] = "invalidated"
            self.db.execute("UPDATE workflow_approvals SET payload=? WHERE id=?", (encode(approval), row["id"]))

    def approve_action(self, identifier: str, actor: str, ttl_seconds: int = 3600, now: datetime | None = None) -> dict:
        if not actor.strip() or actor.lower() in {"llm", "model", "agent", "assistant", "system"}:
            raise ValueError("An individual operator identity is required")
        if ttl_seconds <= 0:
            raise ValueError("Approval TTL must be positive")
        now = now or utcnow()
        with self.db:
            action = self.get_action(identifier)
            if not action or action["status"] not in {"pending_approval", "approved", "deferred"}:
                raise ValueError("Action is not available for individual approval")
            self._invalidate_approval(identifier)
            approval = {"id": "apr_" + uuid.uuid4().hex, "action_id": identifier, "actor": actor,
                        "fingerprint": action_fingerprint(action), "approved_at": now.isoformat(),
                        "expires_at": (now + timedelta(seconds=ttl_seconds)).isoformat(), "status": "active"}
            self.db.execute("INSERT INTO workflow_approvals VALUES (?,?,?)", (approval["id"], identifier, encode(approval)))
            action.update(status="approved", approval_id=approval["id"])
            self._write_action(action)
        return approval

    def reject_action(self, identifier: str, actor: str, reason: str = "operator rejected") -> None:
        with self.db:
            action = self.get_action(identifier)
            if not action:
                raise KeyError(identifier)
            if action["status"] in {"sending", "sent", "uncertain"}:
                raise ValueError("Cannot reject an already attempted delivery")
            self._invalidate_approval(identifier)
            action.update(status="rejected", rejection_actor=actor, rejection_reason=reason)
            self._write_action(action)
        self.record_event("operator_rejected", action.get("conversation_id") or identifier, {"action_id": identifier, "actor": actor, "reason": reason})

    def _cancel_actions(self, conversation_id: str, reason: str) -> None:
        for row in self.db.execute("SELECT payload FROM workflow_actions WHERE conversation_id=?", (conversation_id,)).fetchall():
            action = json.loads(row[0])
            if action["status"] in {"pending_approval", "approved", "deferred", "awaiting_manual_send", "approved_for_manual_send"}:
                self._invalidate_approval(action["id"])
                action.update(status="cancelled", cancellation_reason=reason)
                self._write_action(action)

    def apply_inbound(self, event: dict, conversation_id: str | None, labels: list | None = None,
                      stop_scope: str | None = None, conversation_update: dict | None = None,
                      opportunity_update: dict | None = None) -> bool:
        data = dict(event)
        mailbox = str(data.get("mailbox_id", "import"))
        provider_id = str(data.get("provider_message_id") or data.get("id") or data.get("rfc_message_id") or digest(data))
        identifier = str(data.get("id") or "in_" + digest([mailbox, provider_id]))
        data.update(id=identifier, mailbox_id=mailbox, provider_message_id=provider_id,
                    conversation_id=conversation_id, labels=labels or data.get("labels", []))
        self.db.execute("BEGIN IMMEDIATE")
        try:
            result = self.db.execute("INSERT OR IGNORE INTO workflow_inbound VALUES (?,?,?,?,?)", (identifier, mailbox, provider_id, conversation_id, encode(data)))
            if not result.rowcount:
                self.db.commit()
                return False
            if conversation_id:
                conv = self.get_conversation(conversation_id)
                if conv:
                    conv.update(conversation_update or {})
                    conv["context_version"] = int(conv.get("context_version", 1)) + 1
                    conv["stats_version"] = int(conv.get("stats_version", 0)) + 1
                    conv["last_inbound_id"] = identifier
                    conv["last_event_at"] = data.get("received_at", utcnow().isoformat())
                    self.db.execute("UPDATE workflow_conversations SET payload=? WHERE id=?", (encode(conv), conversation_id))
                    self._cancel_actions(conversation_id, "new inbound context")
                    if opportunity_update:
                        opportunity_id = conv.get("opportunity_id")
                        opportunity = self.get_opportunity(opportunity_id or "")
                        if opportunity:
                            if opportunity_update.get("id", opportunity_id) != opportunity_id:
                                raise ValueError("Inbound cannot change the opportunity identity")
                            opportunity.update(opportunity_update)
                            self.db.execute("UPDATE workflow_opportunities SET payload=? WHERE id=?", (encode(opportunity), opportunity_id))
            if stop_scope:
                target = data.get("account_id") if stop_scope == "account" else data.get("suppression_target") or data.get("sender") or data.get("recipient")
                if not target and conversation_id:
                    conv = self.get_conversation(conversation_id) or {}
                    target = conv.get("account_id") if stop_scope == "account" else conv.get("recipient")
                if target:
                    self._suppress(stop_scope, target, "inbound opt-out", data.get("channel", "email") if stop_scope == "channel" else "")
                    if stop_scope == "contact" and "hard_bounce" not in (labels or []):
                        related = self.get_conversation(conversation_id or "") or {}
                        account_id = data.get("account_id") or related.get("account_id")
                        if account_id:
                            self.db.execute("INSERT INTO workflow_pauses VALUES ('account',?,?) ON CONFLICT(scope,target) DO UPDATE SET reason=excluded.reason", (account_id, "personal opt-out: company attempt requires operator review"))
            self.db.execute("INSERT OR IGNORE INTO workflow_events VALUES (?,?,?,?,?)", ("event_" + identifier, "inbound", conversation_id or "manual_review", encode(data), utcnow().isoformat()))
            self.db.commit()
            return True
        except BaseException:
            self.db.rollback()
            raise

    def _suppress(self, scope: str, target: str, reason: str, channel: str = "") -> None:
        if scope not in {"contact", "channel", "account", "domain"}:
            raise ValueError("Suppression scope must be contact, channel, account or domain")
        target = target.strip().lower()
        if scope == "domain" and target in {"gmail.com", "outlook.com", "yahoo.com", "mail.ru", "yandex.ru"}:
            raise ValueError("Shared mailbox domains cannot identify a customer account")
        self.db.execute("INSERT INTO workflow_suppressions VALUES (?,?,?,?,?) ON CONFLICT(scope,target,channel) DO UPDATE SET reason=excluded.reason", (scope, target, channel, reason, utcnow().isoformat()))
        for action in self.list_actions():
            if self.is_suppressed(action) and action["status"] in {"pending_approval", "approved", "deferred", "awaiting_manual_send", "approved_for_manual_send"}:
                self._invalidate_approval(action["id"])
                action.update(status="cancelled", cancellation_reason="suppression")
                self._write_action(action)

    def suppress(self, scope: str, target: str, reason: str = "operator request", channel: str = "") -> None:
        with self.db:
            self._suppress(scope, target, reason, channel)

    def is_suppressed(self, action: dict) -> bool:
        recipient = str(action.get("recipient", "")).lower()
        account = str(action.get("account_id", "")).lower()
        domain = str(action.get("account_domain", "")).lower()
        for row in self.db.execute("SELECT scope,target,channel FROM workflow_suppressions"):
            if row["scope"] == "contact" and row["target"] == recipient:
                return True
            if row["scope"] == "channel" and row["target"] == recipient and row["channel"] == action.get("channel", "email"):
                return True
            if row["scope"] == "account" and account and row["target"] == account:
                return True
            if row["scope"] == "domain" and domain and row["target"] == domain:
                return True
        legacy = self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='suppressed_emails'").fetchone()
        return bool(legacy and self.db.execute("SELECT 1 FROM suppressed_emails WHERE recipient=?", (recipient,)).fetchone())

    def pause(self, scope: str = "global", target: str = "*", reason: str = "operator pause") -> None:
        if scope not in {"global", "campaign", "account", "conversation", "human_takeover"}:
            raise ValueError("Unsupported pause scope")
        with self.db:
            self.db.execute("INSERT INTO workflow_pauses VALUES (?,?,?) ON CONFLICT(scope,target) DO UPDATE SET reason=excluded.reason", (scope, target, reason))

    def resume(self, scope: str = "global", target: str = "*") -> None:
        with self.db:
            self.db.execute("DELETE FROM workflow_pauses WHERE scope=? AND target=?", (scope, target))

    def pause_reason(self, action: dict) -> str | None:
        mapping = {"global": "*", "campaign": action.get("campaign_id"), "account": action.get("account_id"), "conversation": action.get("conversation_id"), "human_takeover": action.get("conversation_id")}
        for scope, target in mapping.items():
            if target is not None:
                row = self.db.execute("SELECT reason FROM workflow_pauses WHERE scope=? AND target=?", (scope, target)).fetchone()
                if row:
                    return row[0]
        return None

    def mark_sync(self, mailbox_id: str, success: bool = True, cursor: str | None = None, now: datetime | None = None) -> None:
        with self.db:
            self.db.execute("INSERT INTO workflow_sync VALUES (?,?,?,?) ON CONFLICT(mailbox_id) DO UPDATE SET succeeded_at=excluded.succeeded_at,success=excluded.success,cursor=COALESCE(excluded.cursor,workflow_sync.cursor)", (mailbox_id, (now or utcnow()).isoformat() if success else None, int(success), cursor))

    def sync_is_fresh(self, mailbox_id: str, max_age_seconds: int = 300, now: datetime | None = None) -> bool:
        row = self.db.execute("SELECT succeeded_at,success FROM workflow_sync WHERE mailbox_id=?", (mailbox_id,)).fetchone()
        if not row or not row["success"] or not row["succeeded_at"]:
            return False
        return (now or utcnow()) - datetime.fromisoformat(row["succeeded_at"]) <= timedelta(seconds=max_age_seconds)

    def approval_blocker(self, action: dict, *, current_dependencies: str | None = None, now: datetime | None = None) -> str | None:
        now = now or utcnow()
        if self.is_suppressed(action):
            return "suppressed"
        if self.pause_reason(action):
            return "paused"
        clock_blocker = time_blocker(action, now)
        if clock_blocker:
            return clock_blocker
        if action.get("status") != "approved":
            return "pending_approval"
        approval_id = action.get("approval_id")
        approval = self._get("workflow_approvals", approval_id or "")
        if not approval or approval.get("status") != "active":
            return "pending_approval"
        if datetime.fromisoformat(approval["expires_at"]) <= now:
            return "approval_expired"
        if approval.get("fingerprint") != action_fingerprint(action):
            return "approval_stale"
        if current_dependencies is None or current_dependencies != action.get("dependency_version", action.get("catalog_version")):
            return "catalog_stale"
        permission = action.get("contact_permission", action.get("permission", {}))
        if isinstance(permission, str):
            permission = {"status": permission}
        if not isinstance(permission, dict) or permission.get("status") != "allowed":
            return "contact_policy"
        expires = permission.get("expires_at")
        if expires and datetime.fromisoformat(expires) <= now:
            return "contact_policy"
        conv = self.get_conversation(action.get("conversation_id", ""))
        if action.get("conversation_id") and not conv:
            return "context_missing"
        if action.get("kind", "initial") != "initial" and not conv:
            return "context_missing"
        if conv:
            if conv.get("is_terminal"):
                return "terminal"
            if conv.get("context_version", 1) != action.get("context_version"):
                return "context_stale"
            if conv.get("stats_version", 0) != action.get("stats_version"):
                return "stats_stale"
            holds = set(conv.get("holds", [])) - {"pending_approval"}
            if holds:
                return sorted(holds)[0]
            if action.get("kind", "initial") != "initial" and not self.sync_is_fresh(action.get("mailbox_id", conv.get("mailbox_id", "")), int(action.get("sync_max_age_seconds", 300)), now):
                return "stale_sync"
        assessment = action.get("assessment", {})
        decision = assessment.get("decision") if isinstance(assessment, dict) else None
        if not isinstance(assessment, dict) or assessment.get("allowed") is not True or decision != "propose_message":
            return "action_policy"
        due_at = action.get("due_at")
        if due_at and datetime.fromisoformat(due_at) > now:
            return "not_due"
        return None
