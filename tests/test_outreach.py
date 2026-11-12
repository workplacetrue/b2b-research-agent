import base64
import json
from datetime import UTC, datetime, timedelta
from email import message_from_bytes

import httpx
import pytest

from b2b_agent.config import Config, OutreachConfig
from b2b_agent.crawler import Page
from b2b_agent.mail_delivery import DeliveryConfigurationError, deliver, preflight
from b2b_agent.models import RequestCandidate
from b2b_agent.outbox import Outbox
from b2b_agent.outreach import auto_reply, canonical_ad_url, inspect_ad

NOW = datetime(2026, 11, 5, 10, tzinfo=UTC)


def ad_page(extra="", email="Для откликов пишите на buyer@customer.ru", published="2026-11-04"):
    html = f'''<html><main><article>
      <h1>Автоматизация продаж в CRM</h1>
      <time datetime="{published}"></time>
      <p>Ищем подрядчика для автоматизации продаж и обработки заявок.</p>
      <p>{email}</p>{extra}
      <footer>Поддержка: support@platform.ru</footer>
    </article></main></html>'''
    return Page("https://platform.ru/projects/123", "", html, [])


def mail_config(**kwargs):
    return Config(
        outreach={
            "enabled": True,
            "from_email": "sender@gmail.com",
            "signature": "Иван",
            **kwargs,
        }
    )


def test_inspect_explicit_reply_contact_and_cautious_offer():
    result = inspect_ad(ad_page(), mail_config(), NOW)
    assert result.status == "ready"
    assert result.recipient == "buyer@customer.ru"
    assert "buyer@customer.ru" in result.contact_quote
    assert "support@platform.ru" not in result.contact_quote
    assert result.source_url in result.body
    assert "под ваш бюджет" not in result.body
    assert "стоимость" in result.body.lower()
    assert "после уточнения" in result.body
    assert "за сжатые сроки" not in result.body


@pytest.mark.parametrize(
    "page,reason",
    [
        (ad_page(published="2025-01-01"), "устарело"),
        (ad_page(published="2027-01-01"), "будущей"),
        (ad_page(email="Контакты площадки: support@platform.ru"), "явно указанный email"),
        (ad_page(email="Пишите на first@customer.ru или second@customer.ru"), "единственный"),
        (ad_page(extra="<p>Проект закрыт, исполнитель найден.</p>"), "закрытого"),
        (ad_page(extra="<p>Вакансия, полный рабочий день.</p>"), "вакансия"),
        (ad_page(published=""), "Дата публикации"),
        (ad_page(extra='<time datetime="2026-10-28"></time>'), "неоднозначна"),
    ],
)
def test_ineligible_ads_are_not_sent(page, reason):
    result = inspect_ad(page, mail_config(), NOW)
    assert result.status == "needs_review"
    assert reason in result.reasons[0]
    assert result.body is None


def test_listing_is_not_a_single_ad():
    page = ad_page()
    page.html = page.html.replace("</main>", "<article><h1>Ещё задача</h1></article></main>")
    assert inspect_ad(page, mail_config(), NOW).status == "needs_review"


def test_tracking_parameters_do_not_create_new_listing():
    assert canonical_ad_url("https://www.platform.ru/projects/123?utm_source=x#reply") == (
        "https://platform.ru/projects/123"
    )
    assert canonical_ad_url("https://platform.ru/project?id=123") != canonical_ad_url(
        "https://platform.ru/project?id=124"
    )


def payload(source="https://platform.ru/1", recipient="buyer@customer.ru", key="key1"):
    return {
        "source_url": source,
        "recipient": recipient,
        "message_key": key,
        "subject": "Отклик",
        "body": "Здравствуйте!",
    }


def test_outbox_persists_dedupe_after_crash_and_applies_suppression(tmp_path):
    path = str(tmp_path / "db.sqlite")
    first, second = Outbox(path), Outbox(path)
    try:
        assert first.reserve(payload(), OutreachConfig(), NOW) == "reserved"
        assert second.reserve(payload(), OutreachConfig(), NOW) == "duplicate"
        assert (
            second.reserve(
                payload(source="https://other.ru/1", key="crosspost"), OutreachConfig(), NOW
            )
            == "recipient_cooldown"
        )
        second.suppress("other@customer.ru")
        assert (
            first.reserve(
                payload(source="https://other.ru/2", recipient="OTHER@customer.ru"),
                OutreachConfig(),
                NOW,
            )
            == "suppressed"
        )
        # A surviving 'sending' reservation is intentionally never retried.
        assert first.db.execute("SELECT status FROM outbound_messages").fetchone()[0] == "sending"
    finally:
        first.close()
        second.close()


def test_daily_quota_uses_moscow_calendar(tmp_path):
    box = Outbox(str(tmp_path / "db.sqlite"))
    config = OutreachConfig(max_per_day=1)
    try:
        before_midnight = datetime(2026, 11, 5, 20, 59, tzinfo=UTC)
        assert box.reserve(payload(), config, before_midnight) == "reserved"
        other = payload(source="https://platform.ru/2", recipient="other@customer.ru", key="key2")
        assert box.reserve(other, config, before_midnight) == "daily_limit"
        assert box.reserve(other, config, before_midnight + timedelta(minutes=2)) == "reserved"
    finally:
        box.close()


class FakeCrawler:
    def __init__(self, *args):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def fetch(self, url):
        page = ad_page(published=datetime.now(UTC).date().isoformat())
        page.url = url
        return page


def gmail_credentials(monkeypatch):
    for name in ["GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN"]:
        monkeypatch.setenv(name, "fake-test-value")


async def test_gmail_pipeline_queues_once_then_individually_approved_delivery(tmp_path, monkeypatch):
    gmail_credentials(monkeypatch)
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "fake-access-token"})
        assert request.url.path.endswith("/users/me/messages/send")
        assert request.headers["Authorization"] == "Bearer fake-access-token"
        message = message_from_bytes(base64.urlsafe_b64decode(json.loads(request.content)["raw"]))
        assert message["To"] == "buyer@customer.ru"
        assert message["From"] == "sender@gmail.com"
        assert not message["Cc"] and not message["Bcc"]
        assert "https://platform.ru/projects/123" in message.get_payload(decode=True).decode()
        return httpx.Response(200, json={"id": "gmail-message-1"})

    box = Outbox(str(tmp_path / "db.sqlite"))
    candidates = [RequestCandidate(source_url="https://platform.ru/projects/123", query="test")]
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            first = await auto_reply(
                mail_config(), box, candidates, crawler_factory=FakeCrawler, client=client
            )
            second = await auto_reply(
                mail_config(), box, candidates, crawler_factory=FakeCrawler, client=client
            )
        assert first[0].status == second[0].status == "pending_approval"
        assert not calls  # The old enabled flag does not authorize OAuth or send.
        assert first[0].action_id == second[0].action_id
        assert len(box.workflow.list_actions()) == 1
        from b2b_agent.workflow import dispatch_action
        box.workflow.approve_action(first[0].action_id, "owner")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            sent = await dispatch_action(box, first[0].action_id, mail_config(), client)
        assert sent["status"] == "sent"
        from b2b_agent.outbox import DispatchBlocked
        with pytest.raises(DispatchBlocked):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                await dispatch_action(box, first[0].action_id, mail_config(), client)
        assert sum(r.url.host == "gmail.googleapis.com" for r in calls) == 1
        status, stored = box.db.execute("SELECT status,payload FROM outbound_messages").fetchone()
        assert status == "sent" and json.loads(stored)["body"] == first[0].body
    finally:
        box.close()


async def test_timeout_is_uncertain_and_is_not_retried(tmp_path, monkeypatch):
    gmail_credentials(monkeypatch)

    def handler(request):
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "token"})
        raise httpx.ReadTimeout("timeout after server may have accepted", request=request)

    box = Outbox(str(tmp_path / "db.sqlite"))
    candidates = [RequestCandidate(source_url="https://platform.ru/projects/123", query="test")]
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            first = await auto_reply(
                mail_config(), box, candidates, crawler_factory=FakeCrawler, client=client
            )
            second = await auto_reply(
                mail_config(), box, candidates, crawler_factory=FakeCrawler, client=client
            )
        assert first[0].status == second[0].status == "pending_approval"
        from b2b_agent.outbox import DispatchBlocked
        from b2b_agent.workflow import dispatch_action
        box.workflow.approve_action(first[0].action_id, "owner")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(httpx.ReadTimeout):
                await dispatch_action(box, first[0].action_id, mail_config(), client)
            with pytest.raises(DispatchBlocked):
                await dispatch_action(box, first[0].action_id, mail_config(), client)
        assert box.db.execute("SELECT status FROM outbound_messages").fetchone()[0] == "uncertain"
    finally:
        box.close()


async def test_dry_run_needs_no_mail_credentials_and_reserves_nothing(tmp_path):
    box = Outbox(str(tmp_path / "db.sqlite"))
    candidates = [RequestCandidate(source_url="https://platform.ru/projects/123", query="test")]
    try:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: pytest.fail("Dry run must not contact the mail service"),
            )
        ) as client:
            result = await auto_reply(
                mail_config(),
                box,
                candidates,
                dry_run=True,
                crawler_factory=FakeCrawler,
                client=client,
            )
        assert result[0].status == "dry_run" and result[0].body
        assert box.db.execute("SELECT COUNT(*) FROM outbound_messages").fetchone()[0] == 0
    finally:
        box.close()


def test_missing_gmail_configuration_is_actionable(monkeypatch):
    monkeypatch.delenv("GMAIL_REFRESH_TOKEN", raising=False)
    with pytest.raises(DeliveryConfigurationError, match="GMAIL_REFRESH_TOKEN"):
        preflight(mail_config().outreach)


async def test_smtp_uses_tls_and_single_explicit_recipient(monkeypatch, tmp_path):
    calls = []

    class SMTP:
        def __init__(self, host, port, **kwargs):
            assert host == "smtp.gmail.com" and port == 465 and kwargs["context"]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def login(self, user, password):
            assert user == "sender@gmail.com" and password == "fake-app-password"

        def send_message(self, message):
            calls.append(message)
            return {}

    monkeypatch.setattr("b2b_agent.mail_delivery.smtplib.SMTP_SSL", SMTP)
    monkeypatch.setenv("OUTREACH_SMTP_USER", "sender@gmail.com")
    monkeypatch.setenv("OUTREACH_SMTP_PASSWORD", "fake-app-password")
    config = mail_config(provider="smtp", smtp_host="smtp.gmail.com").outreach
    preflight(config)
    box = Outbox(str(tmp_path / "smtp.sqlite"))
    try:
        action = box.workflow.queue_action({**payload(), "id": "smtp-test", "sender_account": config.from_email,
            "dependency_version": "test", "contact_permission": {"status": "allowed"}, "assessment": {"allowed": True, "decision": "propose_message"}})
        box.workflow.approve_action(action["id"], "owner")
        authorization = box.authorize_delivery(action["id"], config, current_dependencies="test")
        async with httpx.AsyncClient() as client:
            await deliver(config, client, action["recipient"], action["subject"], action["body"], action["message_key"], authorization=authorization)
        assert len(calls) == 1 and calls[0]["To"] == "buyer@customer.ru"
    finally:
        box.close()


@pytest.mark.parametrize("requirement,channel", [
    ("Отклики принимаются только через сайт.", "platform"),
    ("Отклики принимаются только через Telegram.", "telegram"),
    ("Отклики принимаются только через LinkedIn.", "linkedin"),
])
def test_required_platform_prepares_manual_draft_without_email_bypass(requirement, channel):
    page = ad_page(extra=f"<p>{requirement}</p>")
    result = inspect_ad(page, mail_config(), NOW)
    assert result.status == "ready_manual" and result.channel == channel
    assert result.recipient == result.source_url
    assert result.recipient != "buyer@customer.ru"
    assert result.body and requirement.rstrip(".") in result.contact_quote
    assert "обязательный" in result.reasons[0]


async def test_mandatory_manual_channel_queues_when_email_transport_disabled_and_never_sends(tmp_path):
    from b2b_agent.channels import confirm_manual_sent
    from b2b_agent.workflow import current_catalog, dispatch_action
    class ManualCrawler(FakeCrawler):
        async def fetch(self, url):
            return ad_page(extra="<p>Отклики принимаются только через сайт.</p>",
                           published=datetime.now(UTC).date().isoformat())
    calls = []
    def unexpected_transport(request):
        calls.append(str(request.url))
        raise AssertionError("A manual platform task must not contact an email service")
    box = Outbox(str(tmp_path / "manual.sqlite"))
    config = mail_config(enabled=False)
    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected_transport)) as client:
        results = await auto_reply(config, box, [RequestCandidate(source_url="https://platform.ru/projects/123", query="test")],
                                   crawler_factory=ManualCrawler, client=client)
        assert results[0].status == "pending_approval" and results[0].channel == "platform"
        action_id = results[0].action_id
        with pytest.raises(ValueError, match="approval"):
            await dispatch_action(box, action_id, config, client)
        box.workflow.approve_action(action_id, "owner")
        result = await dispatch_action(box, action_id, config, client)
        assert result["status"] == "awaiting_manual_send"
        assert box.db.execute("SELECT COUNT(*) FROM outbound_messages").fetchone()[0] == 0
        dependency = current_catalog(config).dependency_version(result["service_id"])
        final = confirm_manual_sent(box.workflow, action_id, actor="owner", current_dependencies=dependency)
        assert final["status"] == "sent_confirmed_by_user"
        assert not calls
    box.close()
