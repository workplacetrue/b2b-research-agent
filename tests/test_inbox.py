import base64
import json
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser

import httpx
import pytest

from b2b_agent.config import OutreachConfig
from b2b_agent.inbox import (
    GmailInboxProvider,
    InboxConfigurationError,
    import_replies,
    match_conversation,
    normalize_mime,
)
from b2b_agent.mail_delivery import DeliveryConfigurationError, deliver


def raw_message(body="Интересно, пришлите информацию", sender="buyer@customer.ru"):
    message = EmailMessage()
    message["From"] = sender
    message["To"] = "operator@example.org"
    message["Subject"] = "Re: Обработка заявок"
    message["Message-ID"] = "<inbound-1@customer.ru>"
    message["In-Reply-To"] = "<outbound-1@example.org>"
    message["Date"] = "Thu, 12 Nov 2026 11:00:00 +0300"
    message.set_content(body)
    return message.as_bytes()


def gmail_message(message_id="p1", thread_id="t1", body="Интересно"):
    return {
        "id": message_id, "threadId": thread_id,
        "raw": base64.urlsafe_b64encode(raw_message(body)).decode(),
        "internalDate": "1794470400000",
    }


def test_import_preserves_stop_at_end_but_removes_quoted_stop_and_signature(tmp_path):
    text = "Подробности " * 3000 + "\nНе пишите мне больше.\n\nOn Wed, buyer wrote:\n> старый текст\n"
    path = tmp_path / "message.eml"
    path.write_bytes(raw_message(text))
    message = import_replies(path)[0]
    assert "Не пишите мне больше" in message.body
    assert "старый текст" not in message.body
    assert message.rfc_message_id == "<inbound-1@customer.ru>"
    assert message.in_reply_to == "<outbound-1@example.org>"
    second = normalize_mime(raw_message("Спасибо\n> Не пишите мне\n-- \nПодпись"))
    assert second.body == "Спасибо"
    assert message.event_id == import_replies(path)[0].event_id


def test_html_quotes_and_attached_messages_are_not_new_author_text():
    message = EmailMessage()
    message["From"] = "buyer@customer.ru"
    message["To"] = "operator@example.org"
    message.set_content("<p>Пришлите сведения</p><blockquote>не пишите</blockquote>", subtype="html")
    message.add_attachment(raw_message("не писать"), maintype="message", subtype="rfc822", filename="old.eml")
    normalized = normalize_mime(message.as_bytes())
    assert normalized.body == "Пришлите сведения"


def test_auto_reply_and_delivery_notifications_are_distinct():
    message = BytesParser(policy=policy.default).parsebytes(raw_message("Я в отпуске"))
    message["Auto-Submitted"] = "auto-replied"
    assert normalize_mime(message.as_bytes()).is_auto_reply
    dsn = b"""From: mailer-daemon@example.org\nTo: operator@example.org\nMIME-Version: 1.0\nContent-Type: multipart/report; boundary=dsn; report-type=delivery-status\n\n--dsn\nContent-Type: text/plain\n\nDelivery failed\n--dsn\nContent-Type: message/delivery-status\n\nReporting-MTA: dns; example.org\n\nFinal-Recipient: rfc822; buyer@customer.ru\nAction: failed\nStatus: 5.1.1\n\n--dsn--\n"""
    report = normalize_mime(dsn)
    assert report.bounce == "hard"
    assert report.delivery_recipient == "buyer@customer.ru"
    assert report.sender == "mailer-daemon@example.org"


def test_json_import_is_inert_and_quote_aware(tmp_path):
    path = tmp_path / "message.json"
    path.write_text(json.dumps({
        "provider_message_id": "json-1", "sender": "buyer@customer.ru",
        "body": "Пришлите цену\n> не писать", "received_at": "2026-11-12T08:00:00Z",
    }), encoding="utf-8")
    assert import_replies(path)[0].body == "Пришлите цену"
    data = json.loads(path.read_text())
    data["execute"] = "open('/etc/passwd')"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        import_replies(path)


def test_matching_requires_identifiers_mailbox_and_participants():
    message = normalize_mime(raw_message(), mailbox_id="work", thread_id="t1")
    conversation = {
        "id": "c1", "mailbox_id": "work", "provider_thread_id": "t1",
        "participants": ["buyer@customer.ru", "operator@example.org"],
        "rfc_message_ids": ["<outbound-1@example.org>"],
    }
    assert match_conversation(message, [conversation]) == ("c1", "matched")
    stranger = message.model_copy(update={"sender": "other@customer.ru"})
    assert match_conversation(stranger, [conversation]) == (None, "manual_review")
    subject_only = message.model_copy(update={"thread_id": None, "in_reply_to": None})
    assert match_conversation(subject_only, [conversation]) == (None, "manual_review")
    assert match_conversation(message, [conversation, {**conversation, "id": "c2"}]) == (None, "manual_review")
    operator = message.model_copy(update={
        "sender": "operator@example.org", "recipients": ["other@example.net"], "is_operator": True,
    })
    assert match_conversation(operator, [conversation]) == (None, "manual_review")
    operator.recipients = ["buyer@customer.ru"]
    assert match_conversation(operator, [conversation]) == ("c1", "matched")
    notification = message.model_copy(update={
        "sender": "mailer-daemon@example.org", "is_delivery_notification": True,
        "bounce": "hard", "delivery_recipient": "buyer@customer.ru",
    })
    assert match_conversation(notification, [conversation]) == ("c1", "matched")


async def test_gmail_bounded_listing_pagination_and_resume():
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path.endswith("profile"):
            return httpx.Response(200, json={"historyId": "100007"})
        if request.url.path.endswith("messages"):
            assert request.url.params["q"] == "label:B2B newer_than:30d"
            if request.url.params.get("pageToken") == "next":
                return httpx.Response(200, json={"messages": [{"id": "p2", "threadId": "t2"}]})
            return httpx.Response(200, json={"messages": [{"id": "p1", "threadId": "t1"}], "nextPageToken": "next"})
        return httpx.Response(200, json=gmail_message(request.url.path.rsplit("/", 1)[1]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GmailInboxProvider(client, "token", query="label:B2B newer_than:30d", max_messages=1)
        first = await provider.sync()
        second = await provider.sync(first.cursor)
    assert [item.provider_message_id for item in first.messages + second.messages] == ["p1", "p2"]
    assert first.has_more and not second.has_more
    assert len([item for item in calls if item.url.path.endswith("profile")]) == 1


async def test_gmail_history_is_opaque_filters_threads_and_handles_expiry():
    mode = {"expired": False}
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path.endswith("history"):
            assert request.url.params["startHistoryId"] == "12345678901234567890"
            if mode["expired"]:
                return httpx.Response(404, json={"error": "expired"})
            return httpx.Response(200, json={"historyId": "99999999999999999999", "history": [{"messagesAdded": [
                {"message": {"id": "p1", "threadId": "t1"}},
                {"message": {"id": "personal", "threadId": "private"}},
            ]}]})
        if request.url.path.endswith("profile"):
            return httpx.Response(200, json={"historyId": "777"})
        if "/threads/" in request.url.path:
            return httpx.Response(200, json={"messages": [{"id": "p2", "threadId": "t1"}]})
        return httpx.Response(200, json=gmail_message(request.url.path.rsplit("/", 1)[1]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GmailInboxProvider(client, "token", known_thread_ids=["t1"])
        first = await provider.sync("12345678901234567890")
        mode["expired"] = True
        second = await provider.sync("12345678901234567890")
    assert [item.provider_message_id for item in first.messages] == ["p1"]
    assert second.history_reset and second.messages[0].provider_message_id == "p2"
    assert not any(item.url.path.endswith("/personal") for item in calls)


async def test_gmail_history_pending_page_is_not_lost_at_batch_limit():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("history"):
            return httpx.Response(200, json={"historyId": "300", "history": [{"messagesAdded": [
                {"message": {"id": key, "threadId": "t1"}} for key in ["a", "b", "c"]
            ]}]})
        return httpx.Response(200, json=gmail_message(request.url.path.rsplit("/", 1)[1]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GmailInboxProvider(client, "token", known_thread_ids=["t1"], max_messages=1)
        first = await provider.sync("100")
        second = await provider.sync(first.cursor)
        third = await provider.sync(second.cursor)
    assert [batch.messages[0].provider_message_id for batch in [first, second, third]] == ["a", "b", "c"]
    assert sum(item.endswith("history") for item in calls) == 1
    assert not third.has_more


async def test_gmail_thread_budget_and_changed_scope_resume_without_old_cursor():
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path.endswith("profile"):
            return httpx.Response(200, json={"historyId": "100"})
        if "/threads/" in request.url.path:
            key = request.url.path.rsplit("/", 1)[1]
            return httpx.Response(200, json={"messages": [{"id": key, "threadId": key}]})
        key = request.url.path.rsplit("/", 1)[1]
        return httpx.Response(200, json=gmail_message(key, key))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GmailInboxProvider(client, "token", known_thread_ids=["t1", "t2"], max_pages=1)
        first = await provider.sync()
        assert first.has_more and [message.thread_id for message in first.messages] == ["t1"]
        assert len([call for call in calls if "/threads/" in call.url.path]) == 1
        second = await provider.sync(first.cursor)
        assert not second.has_more and second.messages[0].thread_id == "t2"
        changed = GmailInboxProvider(client, "token", known_thread_ids=["t3"], max_pages=1)
        third = await changed.sync(second.cursor)
    assert third.history_reset and third.messages[0].thread_id == "t3"
    assert len([call for call in calls if call.url.path.endswith("profile")]) == 2


async def test_gmail_read_scope_error_and_unbounded_access_are_explicit():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(403))) as client:
        with pytest.raises(InboxConfigurationError, match="query"):
            GmailInboxProvider(client, "token")
        provider = GmailInboxProvider(client, "token", query="label:B2B")
        with pytest.raises(InboxConfigurationError, match="gmail.readonly"):
            await provider.sync()


async def test_gmail_page_budget_preserves_resume_even_without_relevant_messages():
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("profile"):
            return httpx.Response(200, json={"historyId": "100"})
        if request.url.path.endswith("messages"):
            token = request.url.params.get("pageToken")
            if token == "later":
                return httpx.Response(200, json={"messages": [{"id": "relevant", "threadId": "t1"}]})
            return httpx.Response(200, json={"messages": [{"id": "personal", "threadId": "t2"}], "nextPageToken": "later"})
        return httpx.Response(200, json=gmail_message("relevant", "t1"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GmailInboxProvider(client, "token", query="label:B2B", known_thread_ids=["t1"], max_pages=1)
        first = await provider.sync()
        second = await provider.sync(first.cursor)
    assert first.has_more and first.messages == []
    assert not second.has_more and second.messages[0].provider_message_id == "relevant"
    assert not any(item.url.path.endswith("/personal") for item in requests)


def test_dsn_original_headers_match_without_reading_attached_body():
    raw = b"""From: mailer-daemon@example.org\nTo: operator@example.org\nMIME-Version: 1.0\nContent-Type: multipart/report; boundary=dsn; report-type=delivery-status\n\n--dsn\nContent-Type: text/plain\n\nDelivery failed\n--dsn\nContent-Type: message/delivery-status\n\nReporting-MTA: dns; example.org\n\nFinal-Recipient: rfc822; buyer@customer.ru\nAction: failed\nStatus: 5.1.1\n\n--dsn\nContent-Type: message/rfc822\nContent-Disposition: attachment\n\nFrom: operator@example.org\nTo: buyer@customer.ru\nMessage-ID: <original@example.org>\nContent-Type: text/plain\n\nDo not parse this original body as a reply.\n--dsn--\n"""
    message = normalize_mime(raw, mailbox_id="work")
    assert message.references == ["<original@example.org>"]
    assert "original body" not in message.body
    conversation = {"id": "c", "mailbox_id": "work", "participants": ["buyer@customer.ru"],
                    "rfc_message_ids": ["<original@example.org>"]}
    assert match_conversation(message, [conversation]) == ("c", "matched")


class Permit:
    def __init__(self):
        self.used = False

    def assert_valid(self, recipient, subject, body, message_key, **kwargs):
        assert not self.used
        self.used = kwargs.get("consume", True)


async def test_gmail_reply_keeps_all_three_identifiers_and_headers_separate():
    captured = []

    def handler(request):
        data = json.loads(request.content)
        captured.append(data)
        return httpx.Response(200, json={"id": "provider-456", "threadId": "thread-123"})

    config = OutreachConfig(from_email="operator@example.org", signature="Operator")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        receipt = await deliver(
            config, client, "buyer@customer.ru", "Re: Обработка заявок", "Сведения", "rfc-789",
            gmail_token="token", authorization=Permit(), thread_id="thread-123",
            in_reply_to="<inbound@customer.ru>", references=["<first@example.org>"],
            original_subject="Обработка заявок",
        )
    message = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(captured[0]["raw"]))
    assert captured[0]["threadId"] == "thread-123"
    assert message["In-Reply-To"] == "<inbound@customer.ru>"
    assert message["References"] == "<first@example.org> <inbound@customer.ru>"
    assert receipt == receipt.provider_message_id == "provider-456"
    assert receipt.rfc_message_id == "<rfc-789@example.org>"
    assert receipt.thread_id == "thread-123"


async def test_delivery_without_approval_never_calls_transport():
    config = OutreachConfig(from_email="operator@example.org", signature="Operator")
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: pytest.fail("No send"))) as client:
        with pytest.raises(DeliveryConfigurationError, match="approval"):
            await deliver(config, client, "buyer@customer.ru", "Subject", "Body", "key", gmail_token="token")
        with pytest.raises(DeliveryConfigurationError, match="subject"):
            await deliver(config, client, "buyer@customer.ru", "Re: Other", "Body", "key", gmail_token="token",
                          authorization=Permit(), in_reply_to="<inbound@customer.ru>", original_subject="Actual")


def _approved_smtp(tmp_path):
    from b2b_agent.outbox import Outbox

    box = Outbox(str(tmp_path / "smtp.sqlite"))
    config = OutreachConfig(enabled=True, provider="smtp", smtp_host="smtp.example.org",
                            from_email="operator@example.org", signature="Operator")
    action = box.workflow.queue_action({
        "id": "smtp-action", "recipient": "buyer@customer.ru", "sender": config.from_email,
        "subject": "Обработка заявок", "body": "Проверенный текст", "message_key": "smtp-key",
        "source_url": "https://platform.example/project/1", "dependency_version": "catalog",
        "contact_permission": {"status": "allowed"},
        "assessment": {"allowed": True, "decision": "propose_message"},
    })
    box.workflow.approve_action(action["id"], "operator")
    permit = box.authorize_delivery(action["id"], config, current_dependencies="catalog")
    return box, config, action, permit


async def test_smtp_thread_rechecks_suppression_before_connection(tmp_path, monkeypatch):
    from b2b_agent.outbox import DispatchBlocked
    from b2b_agent.state import WorkflowStore

    box, config, action, permit = _approved_smtp(tmp_path)

    async def delayed_worker(function, *args):
        # A committed opt-out arrives while the SMTP work is waiting to start.
        other = WorkflowStore(box.path)
        other.suppress("contact", action["recipient"], "new opt-out")
        other.close()
        return function(*args)

    monkeypatch.setattr("b2b_agent.mail_delivery.asyncio.to_thread", delayed_worker)
    monkeypatch.setattr("b2b_agent.mail_delivery.smtplib.SMTP_SSL", lambda *a, **k: pytest.fail("No SMTP connection after opt-out"))
    async with httpx.AsyncClient() as client:
        with pytest.raises(DispatchBlocked, match="suppressed"):
            await deliver(config, client, action["recipient"], action["subject"], action["body"],
                          action["message_key"], authorization=permit)
    box.close()


async def test_smtp_thread_rechecks_after_login_before_message_send(tmp_path, monkeypatch):
    from b2b_agent.outbox import DispatchBlocked
    from b2b_agent.state import WorkflowStore

    box, config, action, permit = _approved_smtp(tmp_path)

    class SMTP:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def login(self, *args):
            # Runs in the real worker thread, using its own SQLite connection.
            other = WorkflowStore(box.path)
            other.suppress("contact", action["recipient"], "opt-out during TLS/login")
            other.close()

        def send_message(self, message):
            pytest.fail("Opt-out committed before message transmission")

    monkeypatch.setattr("b2b_agent.mail_delivery.smtplib.SMTP_SSL", SMTP)
    monkeypatch.setenv("OUTREACH_SMTP_USER", "fake")
    monkeypatch.setenv("OUTREACH_SMTP_PASSWORD", "fake")
    async with httpx.AsyncClient() as client:
        with pytest.raises(DispatchBlocked, match="suppressed"):
            await deliver(config, client, action["recipient"], action["subject"], action["body"],
                          action["message_key"], authorization=permit)
    box.close()


async def test_real_guard_rejects_changed_sender_metadata_before_gmail(tmp_path):
    from b2b_agent.outbox import DispatchBlocked

    box, _, action, permit = _approved_smtp(tmp_path)
    # The sending config is a separate instance; the permit's config is intact.
    changed = OutreachConfig(from_email="attacker@example.org", signature="X")
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: pytest.fail("No send"))) as client:
        with pytest.raises(DispatchBlocked, match="metadata_changed"):
            await deliver(changed, client, action["recipient"], action["subject"], action["body"],
                          action["message_key"], gmail_token="token", authorization=permit)
    box.close()
