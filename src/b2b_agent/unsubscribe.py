"""Offline-testable unsubscribe endpoint and authenticated, durable ledger sync.

No server is launched here. GET/HEAD never subscribe/unsubscribe or track opens.
A deployment must explicitly configure a real public endpoint and DKIM coverage.
"""
import hashlib
import hmac
import html
import ipaddress
import json
import os
import re
import secrets
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

import httpx


class UnsubscribeSyncError(ValueError):
    pass


class UnsubscribeLedger:
    def __init__(self, path: str, *, sync_secret: str, suppression_store=None):
        if len(sync_secret) < 24:
            raise ValueError("Use a dedicated high-entropy service sync secret")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30)
        self.sync_secret = sync_secret
        self.suppression_store = suppression_store
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS unsubscribe_tokens (
                token_hash TEXT PRIMARY KEY, recipient TEXT NOT NULL,
                sender TEXT NOT NULL, scope TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS unsubscribe_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, token_hash TEXT UNIQUE NOT NULL,
                recipient TEXT NOT NULL, sender TEXT NOT NULL, scope TEXT NOT NULL,
                created_at TEXT NOT NULL);
        """)

    def issue(self, recipient: str, sender: str, *, scope: str = "contact") -> str:
        from .config import OutreachConfig
        OutreachConfig(from_email=recipient, reply_to=sender)
        if not recipient or not sender or scope != "contact":
            raise ValueError("Recipient/sender and contact scope are required")
        token = secrets.token_urlsafe(32)
        with self.db:
            self.db.execute("INSERT INTO unsubscribe_tokens VALUES (?,?,?,?,?)",
                (hashlib.sha256(token.encode()).hexdigest(), recipient.lower(), sender.lower(), scope,
                 datetime.now(UTC).isoformat()))
        return token

    def apply(self, token: str) -> bool:
        if not isinstance(token, str) or len(token) > 200:
            return False
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.db:
            row = self.db.execute("SELECT recipient,sender,scope FROM unsubscribe_tokens WHERE token_hash=?", (digest,)).fetchone()
            if row is None:
                return False
            self.db.execute("INSERT OR IGNORE INTO unsubscribe_events(token_hash,recipient,sender,scope,created_at) VALUES(?,?,?,?,?)",
                (digest, *row, datetime.now(UTC).isoformat()))
        if self.suppression_store is not None:
            self.suppression_store.suppress(row[2], row[0], "recipient unsubscribe")
        return True

    def events(self, bearer: str, cursor: int = 0, limit: int = 100) -> dict:
        if not hmac.compare_digest(bearer.encode(), self.sync_secret.encode()):
            raise PermissionError("Unauthorized service sync")
        if cursor < 0 or not 1 <= limit <= 100:
            raise ValueError("Invalid cursor or limit")
        rows = self.db.execute("SELECT id,recipient,sender,scope,created_at FROM unsubscribe_events WHERE id>? ORDER BY id LIMIT ?", (cursor, limit)).fetchall()
        values = [dict(zip(("id", "recipient", "sender", "scope", "created_at"), row)) for row in rows]
        return {"events": values, "cursor": rows[-1][0] if rows else cursor,
                "has_more": bool(rows and self.db.execute("SELECT 1 FROM unsubscribe_events WHERE id>?", (rows[-1][0],)).fetchone())}

    def issue_receipt(self, token: str, recipient: str, sender: str) -> str:
        """Bind an authenticated issuance response to its exact contact/purpose."""
        return _issuance_receipt(self.sync_secret, token, recipient, sender)

    def close(self):
        self.db.close()


class UnsubscribeApp:
    """Minimal ASGI application; invalid tokens receive the same generic page."""
    def __init__(self, ledger: UnsubscribeLedger):
        self.ledger = ledger

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return
        method, path = scope["method"], scope.get("path", "")
        status, body, content_type = 200, b"", b"text/html; charset=utf-8"
        if path == "/tokens":
            content_type = b"application/json"
            headers = dict(scope.get("headers", []))
            bearer = headers.get(b"authorization", b"").decode(errors="replace")
            if method != "POST":
                status, body = 405, b'{"error":"method not allowed"}'
            elif not bearer.startswith("Bearer ") or not hmac.compare_digest(bearer[7:].encode(), self.ledger.sync_secret.encode()):
                status, body = 401, b'{"error":"unauthorized"}'
            else:
                try:
                    raw = bytearray()
                    while True:
                        message = await receive()
                        if message.get("type") != "http.request":
                            raise ValueError("Incomplete request")
                        raw.extend(message.get("body", b""))
                        if len(raw) > 4096:
                            raise ValueError("Request too large")
                        if not message.get("more_body"):
                            break
                    data = json.loads(raw)
                    if not isinstance(data, dict) or set(data) != {"recipient", "sender"}:
                        raise ValueError("Only a contact-scoped recipient and sender are accepted")
                    if not all(isinstance(value, str) for value in data.values()):
                        raise ValueError("Literal mailboxes are required")
                    token = self.ledger.issue(data["recipient"], data["sender"])
                    body = json.dumps({"token": token, "purpose": "unsubscribe", "scope": "contact",
                        "receipt": self.ledger.issue_receipt(token, data["recipient"], data["sender"])}).encode()
                except (ValueError, TypeError, UnicodeError):
                    status, body = 400, b'{"error":"invalid issuance request"}'
        elif path == "/events":
            headers = dict(scope.get("headers", []))
            bearer = headers.get(b"authorization", b"").decode()
            try:
                if not bearer.startswith("Bearer "):
                    raise PermissionError()
                query = parse_qs(scope.get("query_string", b"").decode())
                data = self.ledger.events(bearer[7:], int(query.get("cursor", ["0"])[0]))
                body = json.dumps(data).encode()
                content_type = b"application/json"
            except PermissionError:
                status, body = 401, b'{"error":"unauthorized"}'
            except (ValueError, UnicodeError):
                status, body = 400, b'{"error":"invalid request"}'
        elif path.startswith("/unsubscribe/") and method in {"GET", "HEAD", "POST"}:
            token = path.removeprefix("/unsubscribe/")
            if method == "POST":
                # Purpose-scoped opaque token authorizes only contact opt-out.
                self.ledger.apply(token)
                body = "Запрос прекращения сообщений принят.".encode()
            else:
                safe_path = html.escape(path, quote=True)
                body = (f'<!doctype html><meta charset="utf-8"><title>Прекратить сообщения</title>'
                        f'<p>Подтвердите прекращение сообщений.</p><form action="{safe_path}" method="post">'
                        '<button type="submit">Не писать</button></form>').encode()
        else:
            status, body = 404, b"Not found"
        if method == "HEAD":
            body = b""
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", content_type), (b"cache-control", b"no-store"),
                                (b"referrer-policy", b"no-referrer")]})
        await send({"type": "http.response.body", "body": body})


def public_endpoint_ready(config) -> bool:
    parsed = urlsplit(config.public_base_url)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or not host or parsed.username or parsed.password or parsed.query or parsed.fragment:
        return False
    try:
        from .discovery import normalize_site
        normalize_site(config.public_base_url)
    except ValueError:
        return False
    if any(character.isspace() or ord(character) < 32 for character in config.public_base_url):
        return False
    if (host == "localhost" or "." not in host
        or host in {"example.com", "example.org", "example.net"}
        or any(host.endswith("." + name) for name in ("example.com", "example.org", "example.net"))
        or host.endswith((".local", ".localhost", ".test", ".invalid", ".example"))):
        return False
    try:
        if not ipaddress.ip_address(host).is_global:
            return False
    except ValueError:
        pass
    return bool(config.public_endpoint_verified)


def unsubscribe_material(config, *, sender: str, token: str | None = None) -> dict:
    """Readiness is explicit; mailto never claims RFC8058 one-click support."""
    footer = 'Если не хотите получать сообщения, ответьте «не писать».'
    result = {"mode": config.mode, "footer": footer, "headers": {}, "one_click_verified": False}
    if config.mode == "mailto":
        mailbox = config.mailbox or sender
        from .config import OutreachConfig
        OutreachConfig(from_email=mailbox)
        if not mailbox:
            raise ValueError("A monitored unsubscribe mailbox is required")
        result["url"] = f"mailto:{quote(mailbox, safe='@')}?subject={quote('Не писать')}&body={quote('Не писать')}"
        result["headers"] = {"List-Unsubscribe": f"<{result['url']}>"}
    elif config.mode == "https":
        if not public_endpoint_ready(config) or not token:
            raise ValueError("HTTPS unsubscribe is not ready: configure and verify a public endpoint")
        url = config.public_base_url.rstrip("/") + "/unsubscribe/" + quote(token, safe="")
        result["url"] = url
        result["footer"] += " " + url
        # RFC8058 is enabled only after real DKIM header verification.
        if config.dkim_headers_verified:
            result["headers"] = {"List-Unsubscribe": f"<{url}>",
                "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}
            result["one_click_verified"] = True
    return result


def _sync_table(store):
    store.db.execute("CREATE TABLE IF NOT EXISTS unsubscribe_sync (service TEXT PRIMARY KEY,cursor INTEGER NOT NULL,last_success TEXT,error TEXT)")
    store.db.commit()


def require_fresh_unsubscribe_sync(store, service: str, max_age_seconds: int = 300) -> None:
    _sync_table(store)
    row = store.db.execute("SELECT last_success,error FROM unsubscribe_sync WHERE service=?", (service,)).fetchone()
    if not row or not row[0] or row[1] or (datetime.now(UTC) - datetime.fromisoformat(row[0])).total_seconds() > max_age_seconds:
        raise UnsubscribeSyncError("Mandatory unsubscribe sync is missing, stale or failed; send is on hold")


async def sync_unsubscribes(client: httpx.AsyncClient, store, service_url: str, secret: str, *, max_pages: int = 10) -> int:
    """Cursor survives restarts. Failed mandatory sync cannot reuse old success."""
    parsed = urlsplit(service_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise UnsubscribeSyncError("Service sync URL must use authenticated HTTPS")
    _sync_table(store)
    row = store.db.execute("SELECT cursor FROM unsubscribe_sync WHERE service=?", (service_url,)).fetchone()
    cursor = row[0] if row else 0
    try:
        for _ in range(max_pages):
            response = await client.get(service_url.rstrip("/") + "/events", params={"cursor": cursor},
                headers={"Authorization": f"Bearer {secret}"})
            response.raise_for_status()
            batch = response.json()
            for event in batch["events"]:
                if event["scope"] != "contact":
                    raise UnsubscribeSyncError("Unexpected unsubscribe scope")
                # Suppression is monotonic and idempotent even if interrupted before cursor commit.
                store.suppress("contact", event["recipient"], "recipient unsubscribe")
                store.record_event("unsubscribe", event["recipient"], event,
                    event_id=f"unsubscribe:{hashlib.sha256(service_url.encode()).hexdigest()[:16]}:{event['id']}")
            next_cursor = batch["cursor"]
            if not isinstance(next_cursor, int) or next_cursor < cursor:
                raise UnsubscribeSyncError("Invalid service cursor")
            cursor = next_cursor
            with store.db:
                store.db.execute("INSERT INTO unsubscribe_sync VALUES(?,?,?,NULL) ON CONFLICT(service) DO UPDATE SET cursor=excluded.cursor,last_success=excluded.last_success,error=NULL",
                    (service_url, cursor, datetime.now(UTC).isoformat()))
            if not batch.get("has_more"):
                return cursor
        raise UnsubscribeSyncError("Unsubscribe pagination incomplete; send remains on hold")
    except Exception as error:
        with store.db:
            store.db.execute("INSERT INTO unsubscribe_sync VALUES(?,?,NULL,?) ON CONFLICT(service) DO UPDATE SET error=excluded.error",
                (service_url, cursor, type(error).__name__))
        raise UnsubscribeSyncError("Mandatory unsubscribe sync failed; send is on hold") from None


def delivery_unsubscribe(config, recipient: str, sender: str, *, ledger=None) -> dict:
    if config.mode == "https":
        if ledger is None:
            raise ValueError("HTTPS delivery requires the connected durable unsubscribe token ledger")
        token = ledger.issue(recipient, sender)
        return unsubscribe_material(config, sender=sender, token=token)
    return unsubscribe_material(config, sender=sender)


def _issuance_receipt(secret: str, token: str, recipient: str, sender: str) -> str:
    data = "unsubscribe-v1|contact|" + token + "|" + recipient.lower() + "|" + sender.lower()
    return hmac.new(secret.encode(), data.encode(), hashlib.sha256).hexdigest()


async def prepare_delivery_unsubscribe(config, recipient: str, sender: str,
                                       client: httpx.AsyncClient) -> dict:
    """Obtain a real ledger token over authenticated HTTPS, without sending mail.

    The request/receipt is bound to one contact and sender. An unreachable ledger,
    forged response, placeholder endpoint, or missing service credential is a
    hold; a configured HTTPS mode never silently falls back to a different mode.
    """
    if config.mode != "https":
        return delivery_unsubscribe(config, recipient, sender)
    if not public_endpoint_ready(config):
        raise ValueError("HTTPS unsubscribe endpoint is not configured and verified")
    service_url = config.sync_url or config.public_base_url
    from types import SimpleNamespace
    if not public_endpoint_ready(SimpleNamespace(public_base_url=service_url, public_endpoint_verified=True)):
        raise ValueError("Unsubscribe ledger requires a canonical public HTTPS service URL")
    secret = os.environ.get(config.sync_secret_env)
    if not secret or len(secret) < 24:
        raise ValueError("Configure the dedicated unsubscribe service credential in " + config.sync_secret_env)
    # Validate request data locally; never let service replies substitute another
    # recipient/sender or change the suppression scope.
    from .config import OutreachConfig
    OutreachConfig(from_email=recipient, reply_to=sender)
    if not recipient or not sender:
        raise ValueError("Recipient and sender are required for unsubscribe issuance")
    try:
        response = await client.post(service_url.rstrip("/") + "/tokens", json={
            "recipient": recipient, "sender": sender}, headers={"Authorization": "Bearer " + secret},
            follow_redirects=False)
        if response.status_code != 200 or len(response.content) > 4096:
            raise ValueError("Unsubscribe ledger token issuance failed; send remains on hold")
        result = response.json()
        if not isinstance(result, dict) or result.get("purpose") != "unsubscribe" or result.get("scope") != "contact":
            raise ValueError("Invalid unsubscribe issuance purpose or scope")
        token, receipt = result.get("token"), result.get("receipt")
        if (not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{40,128}", token)
            or not isinstance(receipt, str) or not re.fullmatch(r"[a-f0-9]{64}", receipt)):
            raise ValueError("Invalid unsubscribe ledger token")
        expected = _issuance_receipt(secret, token, recipient, sender)
        if not hmac.compare_digest(receipt, expected):
            raise ValueError("Unsubscribe issuance receipt does not match this contact and sender")
    except httpx.HTTPError:
        raise ValueError("Unsubscribe ledger token issuance is unavailable; send remains on hold") from None
    material = unsubscribe_material(config, sender=sender, token=token)
    material["issuance_verified"] = True
    material["issuance_receipt"] = receipt
    material["issuer"] = service_url
    material["scope"] = "contact"
    return material


def _mechanism_version(settings, recipient: str, sender: str) -> str:
    material = {"settings": settings.model_dump(mode="json"), "recipient": recipient, "sender": sender}
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


def validate_prepared_unsubscribe(action: dict, config) -> dict:
    """Verify the exact reviewed material without trusting arbitrary token fields."""
    settings = config.unsubscribe
    material = action.get("unsubscribe_material")
    if not isinstance(material, dict):
        raise ValueError("Prepare the unsubscribe mechanism and review the exact message before approval")
    sender = config.outreach.from_email
    if action.get("sender_account", sender) != sender:
        raise ValueError("Unsubscribe sender changed")
    if material.get("mechanism_version") != _mechanism_version(settings, action["recipient"], sender):
        raise ValueError("Unsubscribe mechanism changed; prepare and approve the exact message again")
    if settings.mode == "https":
        if not public_endpoint_ready(settings) or material.get("scope") != "contact" or material.get("issuance_verified") is not True:
            raise ValueError("HTTPS unsubscribe issuance has not been verified")
        issuer = settings.sync_url or settings.public_base_url
        if material.get("issuer") != issuer:
            raise ValueError("Unsubscribe issuer changed")
        prefix = settings.public_base_url.rstrip("/") + "/unsubscribe/"
        url = material.get("url", "")
        if not isinstance(url, str) or not url.startswith(prefix):
            raise ValueError("Unsubscribe URL does not match the configured endpoint")
        token = url[len(prefix):]
        if not re.fullmatch(r"[A-Za-z0-9_-]{40,128}", token):
            raise ValueError("Invalid reviewed unsubscribe token")
        secret = os.environ.get(settings.sync_secret_env, "")
        receipt = material.get("issuance_receipt", "")
        if not secret or not isinstance(receipt, str) or not re.fullmatch(r"[a-f0-9]{64}", receipt):
            raise ValueError("Unsubscribe issuance credential or receipt is unavailable")
        if not hmac.compare_digest(receipt, _issuance_receipt(secret, token, action["recipient"], sender)):
            raise ValueError("Unsubscribe receipt does not match the reviewed contact")
        expected = unsubscribe_material(settings, sender=sender, token=token)
    else:
        expected = delivery_unsubscribe(settings, action["recipient"], sender)
    if material.get("headers", {}) != expected["headers"] or action.get("unsubscribe_headers", {}) != expected["headers"]:
        raise ValueError("Reviewed unsubscribe headers changed")
    if expected.get("url") and expected["url"] not in action.get("body", ""):
        raise ValueError("The exact unsubscribe URL is missing from the reviewed message")
    if material.get("mode") != settings.mode:
        raise ValueError("Unsubscribe mode changed")
    return material


async def prepare_action_unsubscribe(store, action_id: str, config, client: httpx.AsyncClient) -> dict:
    """Prepare the visible URL and headers before the operator reviews/approves.

    This operation can invalidate an old approval, never create one. Only an
    already validated bounded render or explicitly reviewed owner text can have
    its deterministic refusal footer updated.
    """
    from .messaging import stamp_draft_content, validate_content_provenance, validate_draft
    from .state import action_fingerprint
    from .workflow import current_catalog
    action = store.get_action(action_id)
    if not action:
        raise ValueError("Unknown action")
    if action.get("channel", "email") != "email":
        raise ValueError("Email unsubscribe preparation applies only to email drafts")
    allowed_states = {"pending_approval", "approved", "deferred"}
    if action.get("status") not in allowed_states:
        raise ValueError("Unsubscribe material cannot modify a completed or reserved send")
    if action.get("content_review_required") or validate_content_provenance(action):
        raise ValueError("Review the message content explicitly before preparing its unsubscribe footer")
    catalog = current_catalog(config)
    dependency = catalog.dependency_version(action["service_id"], action.get("proof_asset_ids"))
    if action.get("dependency_version") != dependency:
        raise ValueError("Refresh the draft after the relevant catalog change")
    errors = validate_draft(action, action.get("evidence_snapshot", []), catalog.service(action["service_id"]),
                            catalog.proof_assets, require_provenance=True)
    if errors:
        raise ValueError("Draft validation failed: " + "; ".join(errors))
    if action.get("unsubscribe_material"):
        try:
            validate_prepared_unsubscribe(action, config)
            return action
        except ValueError:
            pass
    fingerprint = action_fingerprint(action)
    material = await prepare_delivery_unsubscribe(config.unsubscribe, action["recipient"],
        config.outreach.from_email, client)
    material["mechanism_version"] = _mechanism_version(config.unsubscribe, action["recipient"], config.outreach.from_email)
    old = action.get("unsubscribe_material", {})
    body = action.get("body", "").rstrip()
    previous_footer = old.get("applied_footer")
    if previous_footer and body.endswith(previous_footer):
        body = body[:-len(previous_footer)].rstrip()
    elif old.get("url") and old["url"] in body:
        raise ValueError("Remove the obsolete unsubscribe footer explicitly before preparing a new one")
    plain_footer = 'Если не хотите получать сообщения, ответьте «не писать».'
    if body.endswith(plain_footer):
        body = body[:-len(plain_footer)].rstrip()
    footer = material["footer"]
    if material.get("url") and material["url"] not in footer:
        footer += " " + material["url"]
    material["applied_footer"] = footer
    updated = {**action, "body": body + "\n\n" + footer,
               "unsubscribe_material": material, "unsubscribe_headers": material["headers"]}
    stamp = action["content_validation"]
    stamp_draft_content(updated, source=stamp["source"], actor=stamp.get("actor"))
    updated["validation_results"] = validate_draft(updated, action.get("evidence_snapshot", []),
        catalog.service(action["service_id"]), catalog.proof_assets, require_provenance=True)
    if updated["validation_results"]:
        raise ValueError("Prepared unsubscribe draft did not pass content validation")
    current = store.get_action(action_id)
    if not current or current.get("status") not in allowed_states or action_fingerprint(current) != fingerprint:
        raise ValueError("Action changed during unsubscribe preparation; review its current content")
    result = store.update_action(action_id, body=updated["body"], content_validation=updated["content_validation"],
        unsubscribe_material=material, unsubscribe_headers=material["headers"],
        validation_results=updated["validation_results"])
    store.record_event("unsubscribe_prepared", action.get("conversation_id", action_id),
        {"action_id": action_id, "mode": config.unsubscribe.mode, "scope": "contact",
         "mechanism_version": material["mechanism_version"]},
        event_id="unsubscribe-prepared:" + action_id + ":" + material["mechanism_version"])
    return result
