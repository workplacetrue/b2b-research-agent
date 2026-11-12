
import httpx
import pytest

from b2b_agent.config import UnsubscribeConfig
from b2b_agent.state import WorkflowStore
from b2b_agent.unsubscribe import (
    UnsubscribeApp,
    UnsubscribeLedger,
    UnsubscribeSyncError,
    delivery_unsubscribe,
    public_endpoint_ready,
    require_fresh_unsubscribe_sync,
    sync_unsubscribes,
    unsubscribe_material,
)


def ledger(tmp_path, **kwargs):
    return UnsubscribeLedger(str(tmp_path / "unsubscribe.sqlite"), sync_secret="a-service-secret-of-32-characters", **kwargs)


async def test_scanner_get_head_and_forgery_never_opt_out_but_post_does(tmp_path):
    service = ledger(tmp_path)
    token = service.issue("person@buyer.ru", "sender@seller.ru")
    assert "person" not in token and "buyer" not in token
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=UnsubscribeApp(service)), base_url="https://unsubscribe.seller.ru") as client:
        assert (await client.get("/unsubscribe/" + token)).status_code == 200
        assert (await client.head("/unsubscribe/" + token)).content == b""
        assert service.events(service.sync_secret)["events"] == []
        assert (await client.post("/unsubscribe/forged")).text == (await client.post("/unsubscribe/" + token)).text
        await client.post("/unsubscribe/" + token, content="List-Unsubscribe=One-Click")
        assert len(service.events(service.sync_secret)["events"]) == 1
        assert (await client.get("/events")).status_code == 401
        result = await client.get("/events", headers={"Authorization": "Bearer " + service.sync_secret})
        assert result.json()["events"][0]["recipient"] == "person@buyer.ru"
    service.close()


async def test_durable_ledger_sync_suppresses_and_failed_sync_blocks(tmp_path):
    service = ledger(tmp_path)
    service.apply(service.issue("person@buyer.ru", "sender@seller.ru"))
    store = WorkflowStore(str(tmp_path / "agent.sqlite"))
    url = "https://unsubscribe.seller.ru"
    with pytest.raises(UnsubscribeSyncError):
        require_fresh_unsubscribe_sync(store, url)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=UnsubscribeApp(service))) as client:
        assert await sync_unsubscribes(client, store, url, service.sync_secret) == 1
        assert await sync_unsubscribes(client, store, url, service.sync_secret) == 1
        require_fresh_unsubscribe_sync(store, url)
        assert store.is_suppressed({"recipient": "person@buyer.ru"})
        with pytest.raises(UnsubscribeSyncError):
            await sync_unsubscribes(client, store, url, "wrong-secret")
        with pytest.raises(UnsubscribeSyncError):
            require_fresh_unsubscribe_sync(store, url)
    assert len([e for e in store.list_events() if e["kind"] == "unsubscribe"]) == 1
    store.close()
    service.close()


def test_mailto_is_honest_and_fake_https_is_not_ready():
    mailto = unsubscribe_material(UnsubscribeConfig(mode="mailto", mailbox="stop@seller.ru"), sender="seller@seller.ru")
    assert mailto["url"].startswith("mailto:stop@seller.ru")
    assert mailto["one_click_verified"] is False
    assert "List-Unsubscribe-Post" not in mailto["headers"]
    for url in ("https://localhost", "https://127.0.0.1", "https://demo.invalid", "http://seller.ru"):
        config = UnsubscribeConfig(mode="https", public_base_url=url, public_endpoint_verified=True)
        assert not public_endpoint_ready(config)
        with pytest.raises(ValueError, match="not ready"):
            unsubscribe_material(config, sender="sender@seller.ru", token="opaque")
    config = UnsubscribeConfig(mode="https", public_base_url="https://unsubscribe.seller.ru", public_endpoint_verified=True)
    assert not unsubscribe_material(config, sender="sender@seller.ru", token="opaque")["one_click_verified"]
    with pytest.raises(ValueError, match="durable"):
        delivery_unsubscribe(config, "buyer@buyer.ru", "sender@seller.ru")
    config.dkim_headers_verified = True
    result = unsubscribe_material(config, sender="sender@seller.ru", token="opaque")
    assert result["one_click_verified"] and result["headers"]["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"


def test_colocated_optout_immediately_cancels_prepared_actions(tmp_path):
    store = WorkflowStore(str(tmp_path / "local.sqlite"))
    action = store.queue_action({"message_key": "one-initial", "recipient": "person@buyer.ru", "channel": "email", "body": "bounded message"})
    service = ledger(tmp_path, suppression_store=store)
    token = service.issue("person@buyer.ru", "sender@seller.ru")
    assert service.apply(token)
    assert store.get_action(action["id"])["status"] == "cancelled"
    assert store.is_suppressed({"recipient": "person@buyer.ru", "channel": "telegram"})
    service.close()
    store.close()


@pytest.mark.parametrize("body_present", [True, False])
def test_generated_mailto_mime_import_cancels_approved_action_without_confirmation(tmp_path, body_present):
    from email.message import EmailMessage
    from urllib.parse import parse_qs, urlsplit

    from b2b_agent.conversations import apply_inbound_event
    from b2b_agent.inbox import import_replies
    store = WorkflowStore(str(tmp_path / "mailto.sqlite"))
    store.put_conversation({"id": "conv", "opportunity_id": "op", "mailbox_id": "sales", "recipient": "buyer@customer.ru", "participants": ["buyer@customer.ru"]})
    action = store.queue_action({"conversation_id": "conv", "message_key": "old-followup",
        "recipient": "buyer@customer.ru", "body": "Draft", "channel": "email", "kind": "followup"})
    store.approve_action(action["id"], "owner")
    material = unsubscribe_material(UnsubscribeConfig(mode="mailto", mailbox="stop@seller.ru"), sender="sender@seller.ru")
    parsed = urlsplit(material["url"])
    query = parse_qs(parsed.query)
    mime = EmailMessage()
    mime["From"], mime["To"] = "buyer@customer.ru", parsed.path
    mime["Subject"], mime["Message-ID"] = query["subject"][0], "<mailto-stop@customer.ru>"
    mime.set_content(query["body"][0] if body_present else "")
    path = tmp_path / "mailto.eml"
    path.write_bytes(mime.as_bytes())
    event = import_replies(path, mailbox_id="sales")[0]
    first = apply_inbound_event(store, event)
    second = apply_inbound_event(store, event)
    assert "opt_out" in first["labels"] and second["status"] == "duplicate"
    assert store.is_suppressed({"recipient": "buyer@customer.ru", "channel": "telegram"})
    assert store.get_action(action["id"])["status"] == "cancelled"
    assert len(store.list_actions()) == 1
    assert store.db.execute("SELECT COUNT(*) FROM workflow_inbound").fetchone()[0] == 1
    approval = store._get("workflow_approvals", store.get_action(action["id"])["approval_id"])
    assert approval["status"] == "invalidated"
    store.close()


async def test_authenticated_https_token_issuance_is_durable_scoped_and_connectable(tmp_path, monkeypatch):
    from urllib.parse import urlsplit

    from b2b_agent.unsubscribe import prepare_delivery_unsubscribe
    service = ledger(tmp_path)
    monkeypatch.setenv("B2B_UNSUBSCRIBE_SYNC_SECRET", service.sync_secret)
    config = UnsubscribeConfig(mode="https", public_base_url="https://unsubscribe.seller.ru",
        sync_url="https://unsubscribe.seller.ru", public_endpoint_verified=True, dkim_headers_verified=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=UnsubscribeApp(service)),
                                 base_url=config.public_base_url) as client:
        assert (await client.post("/tokens", json={"recipient": "buyer@customer.ru", "sender": "sender@seller.ru"})).status_code == 401
        assert (await client.get("/tokens")).status_code == 405
        headers = {"Authorization": "Bearer " + service.sync_secret}
        rejected = await client.post("/tokens", headers=headers, json={"recipient": "buyer@customer.ru", "sender": "sender@seller.ru", "scope": "account"})
        assert rejected.status_code == 400
        material = await prepare_delivery_unsubscribe(config, "buyer@customer.ru", "sender@seller.ru", client)
        assert material["issuance_verified"] and material["scope"] == "contact"
        assert material["one_click_verified"]
        assert "buyer" not in material["url"] and "customer.ru" not in material["url"]
        assert service.db.execute("SELECT COUNT(*) FROM unsubscribe_tokens").fetchone()[0] == 1
        path = urlsplit(material["url"]).path
        await client.get(path)
        assert not service.events(service.sync_secret)["events"]
        await client.post(path, content="List-Unsubscribe=One-Click")
        assert service.events(service.sync_secret)["events"][0]["recipient"] == "buyer@customer.ru"
    service.close()


async def test_https_issuance_rejects_forged_receipt_redirect_and_placeholder_without_sending(monkeypatch):
    from b2b_agent.unsubscribe import prepare_delivery_unsubscribe
    monkeypatch.setenv("B2B_UNSUBSCRIBE_SYNC_SECRET", "dedicated-service-secret-32-characters")
    config = UnsubscribeConfig(mode="https", public_base_url="https://unsubscribe.seller.ru", public_endpoint_verified=True)
    calls = []
    def forged(request):
        calls.append(str(request.url))
        return httpx.Response(200, json={"token": "x" * 43, "purpose": "unsubscribe", "scope": "contact", "receipt": "0" * 64})
    async with httpx.AsyncClient(transport=httpx.MockTransport(forged)) as client:
        with pytest.raises(ValueError, match="receipt"):
            await prepare_delivery_unsubscribe(config, "buyer@customer.ru", "sender@seller.ru", client)
        invalid = config.model_copy(update={"public_base_url": "https://localhost"})
        with pytest.raises(ValueError, match="not configured"):
            await prepare_delivery_unsubscribe(invalid, "buyer@customer.ru", "sender@seller.ru", client)
        assert len(calls) == 1
    redirect_calls = []
    def redirect(request):
        redirect_calls.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://other.ru/steal"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(redirect), follow_redirects=True) as client:
        with pytest.raises(ValueError, match="issuance failed"):
            await prepare_delivery_unsubscribe(config, "buyer@customer.ru", "sender@seller.ru", client)
        assert redirect_calls == ["https://unsubscribe.seller.ru/tokens"]


def request_action(box, config):
    from datetime import UTC, datetime

    from b2b_agent.crawler import Page
    from b2b_agent.outreach import inspect_ad
    from b2b_agent.workflow import queue_public_request
    html = f'<main><article><h1>Автоматизация обработки заявок</h1><time datetime="{datetime.now(UTC).date()}"></time><p>Ищем подрядчика для автоматизации обработки заявок.</p><p>Для откликов пишите buyer@customer.ru</p></article></main>'
    inspection = inspect_ad(Page("https://platform.ru/request/1", "", html, []), config)
    assert inspection.status == "ready"
    return queue_public_request(box, inspection, config)


async def test_https_footer_is_exact_reviewed_before_approval_and_reused_idempotently(tmp_path, monkeypatch):
    from b2b_agent.config import Config
    from b2b_agent.outbox import Outbox
    from b2b_agent.unsubscribe import prepare_action_unsubscribe, validate_prepared_unsubscribe
    service = ledger(tmp_path)
    monkeypatch.setenv("B2B_UNSUBSCRIBE_SYNC_SECRET", service.sync_secret)
    config = Config(outreach={"enabled": True, "from_email": "sender@seller.ru", "signature": "Owner"},
        unsubscribe={"mode": "https", "public_base_url": "https://unsubscribe.seller.ru", "public_endpoint_verified": True,
                     "dkim_headers_verified": True})
    box = Outbox(str(tmp_path / "exact.sqlite"))
    action = request_action(box, config)
    previous_approval = box.workflow.approve_action(action["id"], "owner")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=UnsubscribeApp(service))) as client:
        prepared = await prepare_action_unsubscribe(box.workflow, action["id"], config, client)
        assert prepared["status"] == "pending_approval"
        assert box.workflow._get("workflow_approvals", previous_approval["id"])["status"] == "invalidated"
        material = validate_prepared_unsubscribe(prepared, config)
        assert material["url"] in prepared["body"] and prepared["body"].count(material["url"]) == 1
        assert "buyer@customer.ru" not in material["url"]
        assert prepared["unsubscribe_headers"]["List-Unsubscribe"] == "<" + material["url"] + ">"
        approval = box.workflow.approve_action(action["id"], "owner")
        repeated = await prepare_action_unsubscribe(box.workflow, action["id"], config, client)
        assert repeated["status"] == "approved" and repeated["approval_id"] == approval["id"]
        assert service.db.execute("SELECT COUNT(*) FROM unsubscribe_tokens").fetchone()[0] == 1
        forged = {**prepared, "unsubscribe_material": {**material, "url": config.unsubscribe.public_base_url + "/unsubscribe/" + "x" * 43}}
        with pytest.raises(ValueError, match="receipt"):
            validate_prepared_unsubscribe(forged, config)
    box.close()
    service.close()


async def test_unsubscribe_preparation_cannot_bless_unreviewed_text_and_preserves_owner_actor(tmp_path, monkeypatch):
    from b2b_agent.config import Config
    from b2b_agent.messaging import stamp_draft_content
    from b2b_agent.outbox import Outbox
    from b2b_agent.unsubscribe import prepare_action_unsubscribe
    config = Config(outreach={"from_email": "sender@seller.ru", "signature": "Owner"},
                    unsubscribe={"mode": "mailto", "mailbox": "stop@seller.ru"})
    box = Outbox(str(tmp_path / "content.sqlite"))
    action = request_action(box, config)
    box.workflow.update_action(action["id"], content_review_required=True)
    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError, match="Review the message content"):
            await prepare_action_unsubscribe(box.workflow, action["id"], config, client)
        reviewed = box.workflow.get_action(action["id"])
        stamp_draft_content(reviewed, source="owner_review", actor="owner-person")
        box.workflow.update_action(action["id"], content_validation=reviewed["content_validation"], content_review_required=False)
        prepared = await prepare_action_unsubscribe(box.workflow, action["id"], config, client)
        assert prepared["content_validation"]["source"] == "owner_review"
        assert prepared["content_validation"]["actor"] == "owner-person"
        assert "mailto:stop@seller.ru" in prepared["body"]
    box.close()


@pytest.mark.parametrize("opted_out", [False, True])
async def test_prepared_https_message_dispatch_syncs_ledger_before_mock_gmail(tmp_path, monkeypatch, opted_out):
    import base64
    import json
    from email import policy
    from email.parser import BytesParser

    from b2b_agent.config import Config
    from b2b_agent.outbox import DispatchBlocked, Outbox
    from b2b_agent.unsubscribe import prepare_action_unsubscribe
    from b2b_agent.workflow import dispatch_action

    service = ledger(tmp_path)
    monkeypatch.setenv("B2B_UNSUBSCRIBE_SYNC_SECRET", service.sync_secret)
    for variable in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN"):
        monkeypatch.setenv(variable, "offline-fixture")
    config = Config(outreach={"enabled": True, "from_email": "sender@seller.ru", "signature": "Owner"},
        unsubscribe={"mode": "https", "public_base_url": "https://unsubscribe.seller.ru",
                     "public_endpoint_verified": True, "dkim_headers_verified": True})
    box = Outbox(str(tmp_path / "agent.sqlite"))
    action = request_action(box, config)
    service_transport = httpx.ASGITransport(app=UnsubscribeApp(service))
    sent = []
    async def route(request):
        if request.url.host == "unsubscribe.seller.ru":
            return await service_transport.handle_async_request(request)
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "offline-access"})
        if request.url.host == "gmail.googleapis.com":
            raw = json.loads(request.content)["raw"]
            mime = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(raw))
            sent.append(mime)
            assert mime["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
            assert box.workflow.get_action(action["id"])["unsubscribe_material"]["url"] in mime.get_content()
            return httpx.Response(200, json={"id": "mock-gmail-message", "threadId": "mock-thread"})
        pytest.fail("Unexpected network host in offline workflow")
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(route)) as client:
            prepared = await prepare_action_unsubscribe(box.workflow, action["id"], config, client)
            box.workflow.approve_action(action["id"], "owner")
            if opted_out:
                await client.post(prepared["unsubscribe_material"]["url"], content="List-Unsubscribe=One-Click")
                with pytest.raises(DispatchBlocked, match="suppressed"):
                    await dispatch_action(box, action["id"], config, client)
                assert not sent
            else:
                result = await dispatch_action(box, action["id"], config, client)
                assert result["status"] == "sent" and len(sent) == 1
    finally:
        box.close()
        service.close()
