"""Durable reservations and exact, single-message transport authorization.

A recorded suppression is checked both on reservation and at transport handoff.
Acceptance already handed to a mail service cannot be recalled atomically with inbox sync.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import OutreachConfig
from .state import WorkflowStore, action_fingerprint, encode, time_blocker, utcnow


class DispatchBlocked(ValueError):
    pass


class DeliveryAuthorization:
    """One process-local permit, issued only after a durable individual approval."""
    def __init__(self, outbox, action: dict, approval: dict, dependencies: str, config, dependency_loader=None):
        self._outbox = outbox
        self._action = dict(action)
        self._approval = dict(approval)
        self._dependencies = dependencies
        self._config = config
        self._sender = config.from_email
        self._provider = config.provider
        self._reply_to = config.reply_to
        conv = outbox.workflow.get_conversation(action.get("conversation_id", "")) or {}
        self._thread_id = action.get("provider_thread_id", action.get("thread_id", conv.get("provider_thread_id", conv.get("thread_id"))))
        self._dependency_loader = dependency_loader
        self._bound_unsubscribe_headers = None
        self._used = False

    def bind_unsubscribe_headers(self, headers: dict) -> None:
        """Pin headers derived by the trusted opt-out issuer after authorization.

        The random refusal capability grants no send permission and may be minted
        only by the configured issuer. This method is not an LLM-facing action.
        """
        if self._used or self._bound_unsubscribe_headers is not None:
            raise DispatchBlocked("unsubscribe_headers_already_bound")
        if not isinstance(headers, dict) or any(key not in {"List-Unsubscribe", "List-Unsubscribe-Post"} or "\r" in str(value) or "\n" in str(value) for key, value in headers.items()):
            raise DispatchBlocked("invalid_unsubscribe_headers")
        self._bound_unsubscribe_headers = dict(headers)

    def assert_valid(self, recipient, subject, body, message_key, *, metadata=None, consume=True):
        if self._used:
            raise DispatchBlocked("approval_already_consumed")
        expected = self._action
        if not self._config.enabled:
            raise DispatchBlocked("transport_disabled")
        if self._config.from_email != self._sender or self._config.provider != self._provider or self._config.reply_to != self._reply_to:
            raise DispatchBlocked("sender_changed")
        if expected.get("sender_account", expected.get("sender", self._sender)) != self._sender:
            raise DispatchBlocked("sender_changed")
        if expected.get("channel", "email") != "email":
            raise DispatchBlocked("channel_changed")
        if self._dependency_loader is not None:
            try:
                fresh_dependencies = self._dependency_loader()
            except Exception:
                raise DispatchBlocked("catalog_invalid") from None
            if fresh_dependencies != self._dependencies:
                raise DispatchBlocked("catalog_stale")
        if metadata is not None:
            expected_metadata = {
                "sender": self._sender, "reply_to": self._reply_to, "provider": self._provider,
                "thread_id": self._thread_id, "in_reply_to": expected.get("in_reply_to"),
                "references": expected.get("references", []),
                "original_subject": expected.get("original_subject"),
            }
            for key, value in expected_metadata.items():
                actual = metadata.get(key)
                if key == "references":
                    actual, value = list(actual or []), list(value or [])
                if actual != value:
                    raise DispatchBlocked("approved_metadata_changed")
            headers = metadata.get("unsubscribe_headers", {}) or {}
            if not isinstance(headers, dict) or any(k not in {"List-Unsubscribe", "List-Unsubscribe-Post"} or "\r" in str(v) or "\n" in str(v) for k,v in headers.items()):
                raise DispatchBlocked("invalid_unsubscribe_headers")
            if "unsubscribe_headers" in expected and headers != expected["unsubscribe_headers"]:
                raise DispatchBlocked("approved_metadata_changed")
            if self._bound_unsubscribe_headers is not None and headers != self._bound_unsubscribe_headers:
                raise DispatchBlocked("issued_unsubscribe_headers_changed")
        if any(str(expected.get(k, "")) != str(v) for k, v in {
            "recipient": recipient, "subject": subject, "body": body, "message_key": message_key,
        }.items()):
            raise DispatchBlocked("approved_content_changed")
        # SMTP runs in a worker thread. Each transport check owns its connection.
        store = WorkflowStore(self._outbox.path)
        store.db.execute("BEGIN IMMEDIATE")
        try:
            current = store.get_action(expected["id"])
            if not current or current.get("status") != "sending":
                raise DispatchBlocked("action_not_reserved")
            if action_fingerprint(current) != self._approval["fingerprint"]:
                raise DispatchBlocked("approval_stale")
            if store.is_suppressed(current):
                raise DispatchBlocked("suppressed")
            if store.pause_reason(current):
                raise DispatchBlocked("paused")
            clock_blocker = time_blocker(current, utcnow())
            if clock_blocker:
                raise DispatchBlocked(clock_blocker)
            conv = store.get_conversation(current.get("conversation_id", "")) or {}
            if conv.get("is_terminal"):
                raise DispatchBlocked("terminal")
            if conv and (conv.get("context_version", 1) != current.get("context_version") or conv.get("stats_version", 0) != current.get("stats_version")):
                raise DispatchBlocked("context_stale")
            if set(conv.get("holds", [])) - {"pending_approval"}:
                raise DispatchBlocked("conversation_hold")
            if datetime.fromisoformat(self._approval["expires_at"]) <= utcnow():
                raise DispatchBlocked("approval_expired")
            if current.get("kind", "initial") != "initial" and not store.sync_is_fresh(current.get("mailbox_id", conv.get("mailbox_id", "")), int(current.get("sync_max_age_seconds", 300))):
                raise DispatchBlocked("stale_sync")
            if consume:
                self._used = True
            store.db.commit()
        except BaseException:
            store.db.rollback()
            raise
        finally:
            store.close()


class Outbox:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        self.db = sqlite3.connect(path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self._migrate()
        self.workflow = WorkflowStore(path, db=self.db)

    def _migrate(self):
        """Rebuild v1 UNIQUE(source_url) transactionally; preserve rows and crash states."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute("CREATE TABLE IF NOT EXISTS suppressed_emails(recipient TEXT PRIMARY KEY,reason TEXT NOT NULL)")
            exists = self.db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='outbound_messages'").fetchone()
            if exists:
                columns = {r[1] for r in self.db.execute("PRAGMA table_info(outbound_messages)")}
                if "kind" not in columns:
                    self.db.execute("ALTER TABLE outbound_messages RENAME TO outbound_messages_v1")
                    self._create_messages()
                    self.db.execute("INSERT INTO outbound_messages(message_key,source_url,recipient,created_at,local_day,status,payload,provider_id,error,kind,logical_key) SELECT message_key,source_url,recipient,created_at,local_day,status,payload,provider_id,error,'initial',message_key FROM outbound_messages_v1")
                    self.db.execute("DROP TABLE outbound_messages_v1")
            else:
                self._create_messages()
            self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS outbound_initial_request ON outbound_messages(source_url,recipient) WHERE kind='initial'")
            self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS outbound_logical_action ON outbound_messages(logical_key)")
            if self.db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Outbox integrity check failed; source database was preserved")
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def _create_messages(self):
        self.db.execute("""CREATE TABLE outbound_messages(
            message_key TEXT PRIMARY KEY,source_url TEXT NOT NULL,recipient TEXT NOT NULL,
            created_at TEXT NOT NULL,local_day TEXT NOT NULL,status TEXT NOT NULL,
            payload TEXT NOT NULL,provider_id TEXT,error TEXT,kind TEXT NOT NULL DEFAULT 'initial',
            logical_key TEXT NOT NULL,conversation_id TEXT,account_id TEXT)""")

    def suppress(self, email: str | None = None, reason: str = "operator request", *, scope: str = "contact", target: str | None = None, channel: str = "") -> None:
        target = target or email
        if not target:
            raise ValueError("Suppression target is required")
        with self.db:
            if scope == "contact":
                self.db.execute("INSERT INTO suppressed_emails VALUES (?,?) ON CONFLICT(recipient) DO UPDATE SET reason=excluded.reason", (target.strip().lower(), reason))
            self.workflow._suppress(scope, target, reason, channel)

    def _reserve(self, payload: dict, config: OutreachConfig, now: datetime) -> str:
        recipient = payload["recipient"].lower()
        kind = payload.get("kind", "initial")
        logical_key = payload.get("action_id") or payload.get("logical_key") or payload["message_key"]
        if self.workflow.is_suppressed(payload):
            return "suppressed"
        if self.workflow.pause_reason(payload):
            return "paused"
        if self.db.execute("SELECT 1 FROM outbound_messages WHERE message_key=? OR logical_key=?", (payload["message_key"], logical_key)).fetchone():
            return "duplicate"
        if kind == "initial":
            if self.db.execute("SELECT 1 FROM outbound_messages WHERE kind='initial' AND source_url=? AND recipient=?", (payload.get("source_url", ""), recipient)).fetchone():
                return "duplicate"
            since = (now - timedelta(days=config.recipient_cooldown_days)).isoformat()
            if self.db.execute("SELECT 1 FROM outbound_messages WHERE kind='initial' AND recipient=? AND created_at>=?", (recipient, since)).fetchone():
                return "recipient_cooldown"
            account = payload.get("account_id")
            if account and self.db.execute("SELECT 1 FROM outbound_messages WHERE account_id=? AND kind='initial' AND created_at>=?", (account, since)).fetchone():
                return "account_cooldown"
        if kind != "initial" and payload.get("conversation_id"):
            since_hour = (now - timedelta(hours=1)).isoformat()
            count = self.db.execute("SELECT COUNT(*) FROM outbound_messages WHERE conversation_id=? AND created_at>=?", (payload["conversation_id"], since_hour)).fetchone()[0]
            if count >= getattr(config, "dialogue_max_per_hour", 5):
                return "dialogue_rate_limit"
            seconds = getattr(config, "dialogue_min_interval_seconds", 0)
            if seconds and self.db.execute("SELECT 1 FROM outbound_messages WHERE conversation_id=? AND created_at>=?", (payload["conversation_id"], (now - timedelta(seconds=seconds)).isoformat())).fetchone():
                return "dialogue_rate_limit"
        day = now.astimezone(ZoneInfo(config.timezone)).date().isoformat()
        count = self.db.execute("SELECT COUNT(*) FROM outbound_messages WHERE local_day=?", (day,)).fetchone()[0]
        if count >= config.max_per_day:
            return "daily_limit"
        self.db.execute("INSERT INTO outbound_messages(message_key,source_url,recipient,created_at,local_day,status,payload,kind,logical_key,conversation_id,account_id) VALUES (?,?,?,?,?,'sending',?,?,?,?,?)", (payload["message_key"], payload.get("source_url", ""), recipient, now.isoformat(), day, encode(payload), kind, logical_key, payload.get("conversation_id"), payload.get("account_id")))
        return "reserved"

    def reserve(self, payload: dict, config: OutreachConfig, now: datetime | None = None) -> str:
        """Low-level ledger reservation; it grants no transport authority."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            result = self._reserve(payload, config, now or utcnow())
            self.db.commit()
            return result
        except BaseException:
            self.db.rollback()
            raise

    def authorize_delivery(self, action_id: str, config: OutreachConfig, *, current_dependencies: str | None = None, now: datetime | None = None, dependency_loader=None) -> DeliveryAuthorization:
        now = now or utcnow()
        if not config.enabled:
            raise DispatchBlocked("transport_disabled")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            action = self.workflow.get_action(action_id)
            if not action:
                raise DispatchBlocked("unknown_action")
            if action.get("channel", "email") != "email":
                raise DispatchBlocked("manual_channel_required")
            if action.get("sender_account", action.get("sender", config.from_email)) != config.from_email:
                raise DispatchBlocked("sender_changed")
            if dependency_loader is not None:
                try:
                    loaded_dependencies = dependency_loader()
                except Exception:
                    raise DispatchBlocked("catalog_invalid") from None
                if loaded_dependencies != current_dependencies:
                    raise DispatchBlocked("catalog_stale")
            blocker = self.workflow.approval_blocker(action, current_dependencies=current_dependencies, now=now)
            if blocker:
                raise DispatchBlocked(blocker)
            reservation = self._reserve(action, config, now)
            if reservation != "reserved":
                raise DispatchBlocked(reservation)
            approval = self.workflow._get("workflow_approvals", action["approval_id"])
            approval["status"] = "consumed"
            self.db.execute("UPDATE workflow_approvals SET payload=? WHERE id=?", (encode(approval), approval["id"]))
            action["status"] = "sending"
            self.workflow._write_action(action)
            self.db.commit()
            return DeliveryAuthorization(self, action, approval, current_dependencies, config, dependency_loader)
        except BaseException:
            self.db.rollback()
            raise

    def finish(self, key: str, status: str, provider_id: str | None = None, error: str | None = None) -> None:
        if status not in {"sent", "simulated_sent", "failed", "uncertain", "rejected", "awaiting_manual_send"}:
            raise ValueError("Invalid outbox state")
        with self.db:
            row = self.db.execute("SELECT payload FROM outbound_messages WHERE message_key=? AND status='sending'", (key,)).fetchone()
            self.db.execute("UPDATE outbound_messages SET status=?,provider_id=?,error=? WHERE message_key=? AND status='sending'", (status, str(provider_id) if provider_id else None, error, key))
            if row:
                payload = json.loads(row[0])
                action = self.workflow.get_action(payload.get("action_id", ""))
                if action:
                    action.update(status=status, provider_id=str(provider_id) if provider_id else None, error=error)
                    for field in ("rfc_message_id", "thread_id", "provider_message_id"):
                        if getattr(provider_id, field, None):
                            action[field] = getattr(provider_id, field)
                    self.workflow._write_action(action)
                    conv = self.workflow.get_conversation(action.get("conversation_id", ""))
                    if conv:
                        conv["last_outbound_id"] = action["id"]
                        conv["stats_version"] = int(conv.get("stats_version", 0)) + 1
                        if status == "sent":
                            conv["last_sent_at"] = utcnow().isoformat()
                            if conv.get("stage", "new") == "new":
                                conv["stage"] = "outreach_sent"
                        self.db.execute("UPDATE workflow_conversations SET payload=? WHERE id=?", (encode(conv), conv["id"]))
        if row:
            self.workflow.record_event("outbound_" + status, payload.get("conversation_id") or key, {"message_key": key, "action_id": payload.get("action_id"), "account_id": payload.get("account_id"), "kind": payload.get("kind", "initial")}, event_id="out_" + key + "_" + status)

    def close(self):
        self.db.close()
