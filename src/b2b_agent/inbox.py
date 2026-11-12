"""Bounded inbox adapters and inert MIME/JSON imports; no implicit mail access.

Gmail reading needs the separately granted ``gmail.readonly`` OAuth scope.
The send-only scope is insufficient. Imported messages are untrusted data.
"""

import base64
import hashlib
import json
import re
from datetime import UTC, datetime
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path
from typing import Literal, Protocol

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field, field_validator


class InboxConfigurationError(ValueError):
    """An actionable configuration failure without credentials or message content."""


class InboxMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mailbox_id: str = "import"
    provider_message_id: str
    rfc_message_id: str | None = None
    thread_id: str | None = None
    sender: str
    recipients: list[str] = Field(default_factory=list)
    subject: str = ""
    body: str = ""
    full_author_text: str = ""
    in_reply_to: str | None = None
    references: list[str] = Field(default_factory=list)
    received_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    is_auto_reply: bool = False
    is_delivery_notification: bool = False
    delivery_recipient: str | None = None
    bounce: Literal["hard", "soft"] | None = None
    is_operator: bool = False

    @field_validator("sender")
    @classmethod
    def normalize_sender(cls, value):
        addresses = getaddresses([value])
        if len(addresses) != 1 or not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+", addresses[0][1]):
            raise ValueError("A single literal sender mailbox is required")
        return addresses[0][1].lower()

    @field_validator("received_at")
    @classmethod
    def aware_date(cls, value):
        if value.tzinfo is None:
            raise ValueError("received_at requires an explicit timezone")
        return value.astimezone(UTC)

    @field_validator("delivery_recipient")
    @classmethod
    def normalize_delivery_recipient(cls, value):
        if value is not None and not re.fullmatch(r"[^\s@<>;,]+@[^\s@<>;,]+", value):
            raise ValueError("DSN recipient must be a single literal mailbox")
        return value.lower() if value else None

    @property
    def event_id(self):
        return hashlib.sha256(
            f"{self.mailbox_id}\0{self.provider_message_id}".encode()
        ).hexdigest()


class InboxBatch(BaseModel):
    messages: list[InboxMessage] = Field(default_factory=list)
    cursor: str | None = None
    history_reset: bool = False
    has_more: bool = False


class InboxProvider(Protocol):
    async def sync(self, cursor: str | None = None) -> InboxBatch: ...


def authored_text(text: str) -> str:
    """Remove recognized quoted history/signatures without truncating new text."""
    lines = []
    source_lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for index, line in enumerate(source_lines):
        stripped = line.strip()
        if re.match(r"^(?:On .+wrote:|.+писал(?:а|\(а\))?:|[-_]{2,}\s*(?:Original Message|Исходное сообщение))$", stripped, re.I):
            break
        if re.match(r"^(?:From|От):\s*\S", stripped, re.I):
            following = source_lines[index + 1:index + 6]
            if sum(bool(re.match(r"^(?:Sent|Date|To|Subject|Отправлено|Дата|Кому|Тема):", item.strip(), re.I)) for item in following) >= 2:
                break
        if line == "-- " or stripped == "--":
            break
        if stripped.startswith(">"):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _html_text(value):
    soup = BeautifulSoup(value, "html.parser")
    for node in soup.select("script,style,blockquote,.gmail_quote,.yahoo_quoted"):
        node.decompose()
    return soup.get_text("\n", strip=True)


def _message_ids(value):
    return re.findall(r"<[^<>\s]+>", value or "")


def normalize_mime(
    raw: bytes, *, mailbox_id="import", provider_message_id=None, thread_id=None,
    operator_addresses=(), received_at=None,
) -> InboxMessage:
    mail = BytesParser(policy=policy.default).parsebytes(raw)
    plain, html, statuses, delivery_recipients, original_ids = [], [], [], [], []
    is_delivery_notification = mail.get_content_type() == "multipart/report" and str(mail.get_param("report-type", "")).lower() == "delivery-status"
    def safe_parts(part):
        # A DSN may return the original RFC headers. Inspect identifiers only,
        # never the attached original body or any other attachment content.
        if part.get_content_type() == "message/rfc822":
            if is_delivery_notification:
                for original in part.get_payload() or []:
                    if hasattr(original, "get"):
                        original_ids.extend(_message_ids(str(original.get("Message-ID", ""))))
            return
        if part.get_content_disposition() == "attachment":
            return
        yield part
        if part.is_multipart() and part.get_content_type() != "message/delivery-status":
            for child in part.iter_parts():
                yield from safe_parts(child)

    for part in safe_parts(mail):
        if part.get_content_disposition() == "attachment":
            continue
        content_type = part.get_content_type()
        if content_type == "text/rfc822-headers" and is_delivery_notification:
            content = part.get_payload(decode=True) or b""
            original = BytesParser(policy=policy.default).parsebytes(content, headersonly=True)
            original_ids.extend(_message_ids(str(original.get("Message-ID", ""))))
            continue
        if content_type == "message/delivery-status":
            is_delivery_notification = True
            for block in part.get_payload() or []:
                statuses.append(str(block.get("Status", "")))
                recipient = str(block.get("Final-Recipient") or block.get("Original-Recipient") or "")
                if ";" in recipient:
                    recipient = recipient.split(";", 1)[1].strip()
                    if re.fullmatch(r"[^\s@<>;,]+@[^\s@<>;,]+", recipient):
                        delivery_recipients.append(recipient.lower())
            continue
        if content_type not in {"text/plain", "text/html"}:
            continue
        try:
            content = part.get_content()
        except (LookupError, UnicodeError):
            content = (part.get_payload(decode=True) or b"").decode("utf-8", errors="replace")
        if isinstance(content, str):
            (plain if content_type == "text/plain" else html).append(content)
    text = "\n".join(plain) if plain else "\n".join(_html_text(item) for item in html)
    author = authored_text(text)
    rfc_id = str(mail.get("Message-ID", "")).strip() or None
    if received_at is None:
        try:
            received_at = parsedate_to_datetime(str(mail.get("Date", "")))
            if received_at.tzinfo is None:
                received_at = received_at.replace(tzinfo=UTC)
        except (TypeError, ValueError):
            received_at = datetime.now(UTC)
    addresses = getaddresses([str(mail.get("From", ""))])
    if len(addresses) != 1 or not addresses[0][1]:
        raise InboxConfigurationError("Imported message requires one sender mailbox")
    sender = addresses[0][1].lower()
    recipients = [address.lower() for _, address in getaddresses(
        [str(mail.get("To", "")), str(mail.get("Cc", ""))]
    ) if address]
    auto = str(mail.get("Auto-Submitted", "no")).lower() not in {"no", ""}
    auto = auto or bool(mail.get("X-Autoreply") or mail.get("X-Autorespond"))
    auto = auto or bool(re.search(
        r"(?:out of office|automatic reply|автоматический ответ|автоответ)",
        str(mail.get("Subject", "")), re.I,
    ))
    bounce = "hard" if any(item.startswith("5.") for item in statuses) else (
        "soft" if any(item.startswith("4.") for item in statuses) else None
    )
    return InboxMessage(
        mailbox_id=mailbox_id,
        provider_message_id=provider_message_id or rfc_id or hashlib.sha256(raw).hexdigest(),
        rfc_message_id=rfc_id, thread_id=thread_id, sender=sender, recipients=recipients,
        subject=str(mail.get("Subject", "")), body=author, full_author_text=author,
        in_reply_to=next(iter(_message_ids(str(mail.get("In-Reply-To", "")))), None),
        references=list(dict.fromkeys(_message_ids(str(mail.get("References", ""))) + original_ids)), received_at=received_at,
        is_auto_reply=auto or is_delivery_notification,
        is_delivery_notification=is_delivery_notification, bounce=bounce,
        delivery_recipient=delivery_recipients[0] if len(set(delivery_recipients)) == 1 else None,
        is_operator=sender in {address.lower() for address in operator_addresses},
    )


def import_replies(path, *, mailbox_id="import", operator_addresses=()) -> list[InboxMessage]:
    """Import data only. Attachments and local paths inside JSON are never opened."""
    source = Path(path)
    if source.suffix.lower() == ".eml":
        return [normalize_mime(
            source.read_bytes(), mailbox_id=mailbox_id, operator_addresses=operator_addresses
        )]
    if source.suffix.lower() != ".json":
        raise InboxConfigurationError("Import requires a .eml or .json file")
    values = json.loads(source.read_text(encoding="utf-8"))
    if isinstance(values, dict) and "messages" in values:
        values = values["messages"]
    if isinstance(values, dict):
        values = [values]
    if not isinstance(values, list):
        raise InboxConfigurationError("JSON import must contain a message or list of messages")
    messages = []
    for value in values:
        value = dict(value)
        value.setdefault("mailbox_id", mailbox_id)
        full = value.get("full_author_text") or value.get("body", "")
        value["full_author_text"] = value["body"] = authored_text(full)
        message = InboxMessage.model_validate(value)
        if message.sender in {address.lower() for address in operator_addresses}:
            message.is_operator = True
        messages.append(message)
    return messages


def _encode_cursor(state):
    return "g1:" + base64.urlsafe_b64encode(json.dumps(state).encode()).decode()


def _decode_cursor(cursor):
    if not cursor:
        return {}
    if not cursor.startswith("g1:"):
        return {"history_id": cursor}
    try:
        value = json.loads(base64.urlsafe_b64decode(cursor[3:]))
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, TypeError):
        raise InboxConfigurationError("Invalid inbox cursor; use an explicit bounded resync") from None


class GmailInboxProvider:
    BASE = "https://gmail.googleapis.com/gmail/v1/users/me"

    def __init__(
        self, client: httpx.AsyncClient, token: str, *, mailbox_id="gmail", query=None,
        known_thread_ids=(), label_id=None, max_messages=100, max_pages=10, operator_addresses=(),
    ):
        if not query and not known_thread_ids and not label_id:
            raise InboxConfigurationError("Choose a working Gmail query, label or known threads")
        if not 1 <= max_messages <= 500:
            raise InboxConfigurationError("max_messages must be between 1 and 500")
        if not 1 <= max_pages <= 100 or len(known_thread_ids) > 100:
            raise InboxConfigurationError("Use 1..100 pages and at most 100 known threads per sync")
        self.client, self.token, self.mailbox_id = client, token, mailbox_id
        self.query, self.threads, self.label_id = query, set(known_thread_ids), label_id
        self.max_messages, self.operator_addresses = max_messages, operator_addresses
        self.max_pages = max_pages
        self._scope_version = hashlib.sha256(json.dumps(
            [self.mailbox_id, self.query, sorted(self.threads), self.label_id],
            ensure_ascii=False,
        ).encode()).hexdigest()

    async def _get(self, path, **params):
        response = await self.client.get(
            f"{self.BASE}/{path}", params=params or None,
            headers={"Authorization": f"Bearer {self.token}"},
        )
        error_reason = ""
        if response.status_code == 403:
            try:
                error_reason = json.dumps(response.json()).lower()
            except ValueError:
                pass
        if response.status_code == 401 or (response.status_code == 403 and not any(
            reason in error_reason for reason in ("ratelimit", "quota", "dailylimit")
        )):
            raise InboxConfigurationError(
                "Gmail inbox needs separately authorized gmail.readonly scope; "
                "send-only access cannot read replies. Reauthorize in environment settings."
            )
        response.raise_for_status()
        return response.json()

    def _relevant(self, item):
        return not self.threads or item.get("threadId") in self.threads

    async def _fetch_messages(self, ids):
        messages = []
        for message_id in dict.fromkeys(ids):
            value = await self._get(f"messages/{message_id}", format="raw")
            if not self._relevant(value):
                continue
            received = None
            if value.get("internalDate"):
                received = datetime.fromtimestamp(int(value["internalDate"]) / 1000, UTC)
            try:
                raw = base64.urlsafe_b64decode(value["raw"] + "===")
            except (KeyError, ValueError):
                raise InboxConfigurationError("Gmail returned malformed MIME data") from None
            message = normalize_mime(
                raw, mailbox_id=self.mailbox_id, provider_message_id=value["id"],
                thread_id=value.get("threadId"), operator_addresses=self.operator_addresses,
                received_at=received,
            )
            if "SENT" in {str(label).upper() for label in value.get("labelIds", [])}:
                message.is_operator = True
            messages.append(message)
        return messages

    async def sync(self, cursor=None) -> InboxBatch:
        state = _decode_cursor(cursor)
        scope_changed = state.get("scope") is not None and state["scope"] != self._scope_version
        if scope_changed:
            state = {}
        # Query-scoped mailboxes use filtered bounded listing. A history read may
        # otherwise disclose messages outside that query. Thread/label scopes can
        # safely use the provider's history API.
        use_history = bool(state.get("history_id")) and bool(self.threads or self.label_id)
        if state.get("mode") == "list":
            use_history = False
        try:
            batch = await (self._history(state) if use_history else self._list(state))
        except httpx.HTTPStatusError as error:
            if not use_history or error.response.status_code != 404:
                raise
            batch = await self._list({})
            batch.history_reset = True
        cursor_state = _decode_cursor(batch.cursor)
        cursor_state["scope"] = self._scope_version
        batch.cursor = _encode_cursor(cursor_state)
        batch.history_reset = batch.history_reset or scope_changed
        return batch

    async def _list(self, state):
        baseline = state.get("baseline")
        if baseline is None:
            baseline = (await self._get("profile")).get("historyId")
        if not self.query and not self.label_id and self.threads:
            if state.get("mode") == "threads":
                pending = list(state.get("pending_ids", []))
                remaining_threads = list(state.get("remaining_thread_ids", []))
            else:
                pending = []
                remaining_threads = sorted(self.threads)
            pages = 0
            while len(pending) < self.max_messages and remaining_threads and pages < self.max_pages:
                thread_id = remaining_threads.pop(0)
                thread = await self._get(f"threads/{thread_id}", format="minimal")
                pending.extend(item["id"] for item in thread.get("messages", []))
                pending = list(dict.fromkeys(pending))
                pages += 1
            selected, pending = pending[:self.max_messages], pending[self.max_messages:]
            messages = await self._fetch_messages(selected)
            has_more = bool(pending or remaining_threads)
            next_state = {"history_id": baseline} if not has_more else {
                "mode": "threads", "baseline": baseline, "pending_ids": pending,
                "remaining_thread_ids": remaining_threads,
            }
            return InboxBatch(messages=messages, cursor=_encode_cursor(next_state), has_more=has_more)
        page_token = state.get("page_token") if state.get("mode") == "list" else None
        messages, pages = [], 0
        while len(messages) < self.max_messages and pages < self.max_pages:
            params = {"maxResults": min(100, self.max_messages - len(messages))}
            if self.query:
                params["q"] = self.query
            if self.label_id:
                params["labelIds"] = self.label_id
            if page_token:
                params["pageToken"] = page_token
            listing = await self._get("messages", **params)
            pages += 1
            ids = [item["id"] for item in listing.get("messages", []) if self._relevant(item)]
            messages.extend(await self._fetch_messages(ids))
            page_token = listing.get("nextPageToken")
            if not page_token:
                break
        if page_token:
            next_state = {"mode": "list", "baseline": baseline, "page_token": page_token}
        else:
            next_state = {"history_id": baseline}
        return InboxBatch(messages=messages, cursor=_encode_cursor(next_state), has_more=bool(page_token))

    async def _history(self, state):
        pending = list(state.get("pending_ids", []))
        messages = []
        if pending:
            selected, pending = pending[:self.max_messages], pending[self.max_messages:]
            messages.extend(await self._fetch_messages(selected))
        page_token = state.get("page_token")
        latest = state.get("latest_history_id", state["history_id"])
        done = state.get("page_done", False)
        pages = 0
        while not pending and not done and len(messages) < self.max_messages and pages < self.max_pages:
            params = {"startHistoryId": state["history_id"], "historyTypes": "messageAdded", "maxResults": 100}
            if page_token:
                params["pageToken"] = page_token
            if self.label_id:
                params["labelId"] = self.label_id
            listing = await self._get("history", **params)
            pages += 1
            latest = listing.get("historyId", latest)
            ids = []
            for event in listing.get("history", []):
                for added in event.get("messagesAdded", []):
                    item = added.get("message", {})
                    if self._relevant(item) and item.get("id"):
                        ids.append(item["id"])
            ids = list(dict.fromkeys(ids))
            available = self.max_messages - len(messages)
            selected, pending = ids[:available], ids[available:]
            messages.extend(await self._fetch_messages(selected))
            page_token = listing.get("nextPageToken")
            done = not page_token
        has_more = bool(pending or not done)
        if has_more:
            next_state = {
                "history_id": state["history_id"], "pending_ids": pending,
                "page_token": page_token, "page_done": done, "latest_history_id": latest,
            }
        else:
            next_state = {"history_id": latest}
        return InboxBatch(messages=messages, cursor=_encode_cursor(next_state), has_more=has_more)


def match_conversation(message: InboxMessage, conversations) -> tuple[str | None, str]:
    """Match identifiers AND participants; subject text never establishes identity."""
    matches = []
    ids = set(message.references + ([message.in_reply_to] if message.in_reply_to else []))
    for conversation in conversations:
        if hasattr(conversation, "model_dump"):
            conversation = conversation.model_dump()
        if conversation.get("mailbox_id") != message.mailbox_id:
            continue
        participants = {item.lower() for item in conversation.get("participants", [])}
        if message.is_delivery_notification:
            participant_match = message.delivery_recipient in participants
        elif message.is_operator:
            operator = conversation.get("sender_account") or conversation.get("sender") or conversation.get("operator_email")
            participant_match = message.sender in participants or bool(operator and message.sender == operator.lower())
            participant_match = participant_match and bool(set(message.recipients) & participants - {message.sender})
        else:
            participant_match = message.sender in participants
        if not participant_match:
            continue
        thread = conversation.get("provider_thread_id") or conversation.get("thread_id")
        known_ids = set(conversation.get("rfc_message_ids", []))
        if (message.thread_id and thread == message.thread_id) or ids.intersection(known_ids):
            matches.append(conversation["id"])
    return (matches[0], "matched") if len(matches) == 1 else (None, "manual_review")
