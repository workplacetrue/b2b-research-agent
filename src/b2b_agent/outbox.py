"""Durable reservations prevent repeat sends, including after a crash or timeout."""

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import OutreachConfig


class Outbox:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS outbound_messages (
                message_key TEXT PRIMARY KEY,
                source_url TEXT NOT NULL UNIQUE,
                recipient TEXT NOT NULL,
                created_at TEXT NOT NULL,
                local_day TEXT NOT NULL,
                status TEXT NOT NULL,
                payload TEXT NOT NULL,
                provider_id TEXT,
                error TEXT);
            CREATE TABLE IF NOT EXISTS suppressed_emails (
                recipient TEXT PRIMARY KEY,
                reason TEXT NOT NULL);
        """)

    def suppress(self, email: str, reason: str = "operator request") -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO suppressed_emails VALUES (?, ?) "
                "ON CONFLICT(recipient) DO UPDATE SET reason=excluded.reason",
                (email.strip().lower(), reason),
            )

    def reserve(self, payload: dict, config: OutreachConfig, now: datetime | None = None) -> str:
        now = now or datetime.now(UTC)
        recipient = payload["recipient"].lower()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            if self.db.execute(
                "SELECT 1 FROM suppressed_emails WHERE recipient=?",
                (recipient,),
            ).fetchone():
                return "suppressed"
            if self.db.execute(
                "SELECT 1 FROM outbound_messages WHERE message_key=? OR source_url=?",
                (payload["message_key"], payload["source_url"]),
            ).fetchone():
                return "duplicate"
            since = (now - timedelta(days=config.recipient_cooldown_days)).isoformat()
            if self.db.execute(
                "SELECT 1 FROM outbound_messages WHERE recipient=? AND created_at>=?",
                (recipient, since),
            ).fetchone():
                return "recipient_cooldown"
            day = now.astimezone(ZoneInfo(config.timezone)).date().isoformat()
            count = self.db.execute(
                "SELECT COUNT(*) FROM outbound_messages WHERE local_day=?",
                (day,),
            ).fetchone()[0]
            if count >= config.max_per_day:
                return "daily_limit"
            self.db.execute(
                "INSERT INTO outbound_messages "
                "(message_key,source_url,recipient,created_at,local_day,status,payload) "
                "VALUES (?,?,?,?,?,'sending',?)",
                (
                    payload["message_key"],
                    payload["source_url"],
                    recipient,
                    now.isoformat(),
                    day,
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
            return "reserved"
        finally:
            self.db.commit()

    def finish(
        self, key: str, status: str, provider_id: str | None = None, error: str | None = None
    ) -> None:
        if status not in {"sent", "failed", "uncertain"}:
            raise ValueError("Invalid outbox state")
        with self.db:
            self.db.execute(
                "UPDATE outbound_messages SET status=?,provider_id=?,error=? "
                "WHERE message_key=? AND status='sending'",
                (status, provider_id, error, key),
            )

    def close(self):
        self.db.close()
