from datetime import UTC, datetime, timedelta

import pytest

from b2b_agent.action_policy import evaluate_action
from b2b_agent.company_stats import get_company_stats
from b2b_agent.config import Config, OutreachConfig
from b2b_agent.conversations import apply_inbound_event
from b2b_agent.crawler import Page
from b2b_agent.outbox import DispatchBlocked, Outbox
from b2b_agent.outreach import inspect_ad
from b2b_agent.state import WorkflowStore
from b2b_agent.workflow import current_catalog, queue_public_request, refresh_action


def conversation(store):
    store.put_opportunity({"id": "o", "origin": "company_research", "account_id": "customer", "service_id": "sales_intake"})
    store.put_conversation({"id": "c", "opportunity_id": "o", "account_id": "customer", "recipient": "buyer@customer.example",
        "participants": ["buyer@customer.example"], "provider_thread_id": "thread", "mailbox_id": "box", "task": "Приём заявок", "service_id": "sales_intake"})


def event(text, key="in1"):
    return {"mailbox_id": "box", "provider_message_id": key, "rfc_message_id": f"<{key}@customer.example>",
        "sender": "buyer@customer.example", "thread_id": "thread", "body": text}


def test_low_usefulness_or_high_risk_waits_without_new_action(tmp_path):
    store = WorkflowStore(tmp_path / "db")
    conversation(store)
    before = len(store.list_actions())
    assert evaluate_action({"kind": "reply", "usefulness": "low"}).decision == "defer"
    assert evaluate_action({"kind": "initial", "reputational_risk": "high"}).decision == "human_handoff"
    assert len(store.list_actions()) == before
    store.close()


def test_deferred_is_active_terminal_preserves_new_inbound_and_suppression(tmp_path):
    store = WorkflowStore(tmp_path / "db")
    conversation(store)
    apply_inbound_event(store, event("Не сейчас, вернитесь 2026-12-01"))
    c = store.get_conversation("c")
    assert not c.get("is_terminal") and c["activity"] == "deferred"
    assert evaluate_action({}, c, now=datetime(2026, 11, 12, tzinfo=UTC)).decision == "defer"
    apply_inbound_event(store, event("Не пишите мне больше", "stop"))
    apply_inbound_event(store, event("Теперь интересно", "new"))
    assert len(store.list_inbound()) == 3
    assert store.get_conversation("c")["is_terminal"]
    assert store.is_suppressed({"recipient": "buyer@customer.example"})
    assert not store.list_actions()
    store.close()


def test_sourced_channel_complaint_holds_without_rotating_sender(tmp_path):
    store = WorkflowStore(tmp_path / "db")
    conversation(store)
    store.queue_action({"id": "a", "conversation_id": "c", "account_id": "customer",
        "sender_account": "sender@example.org", "recipient": "buyer@customer.example", "body": "Draft"})
    initial = get_company_stats(store, "customer")
    assert initial.complaints is None and initial.deliverability == "unknown"
    store.record_event("complaint", "sender@example.org", {"source": "provider-report:1", "risk": "high"}, event_id="complaint1")
    stats = get_company_stats(store, "customer")
    assert stats.complaints == 1 and stats.channel_hold
    assert stats.reputation_signals[0]["source"] == "provider-report:1"
    assert not evaluate_action({"kind": "initial", "contact_permission": "allowed"}, stats=stats).allowed
    assert store.get_action("a")["sender_account"] == "sender@example.org"
    store.close()


def test_capacity_blocks_new_attempts_but_existing_reply_and_optout_processed(tmp_path):
    assert evaluate_action({"kind": "initial", "is_new_opportunity": True, "capacity_full": True}).decision == "defer"
    assert evaluate_action({"kind": "reply", "capacity_full": True, "contact_permission": "allowed"}).allowed
    assert not evaluate_action({"kind": "reply", "promises_start_date": True, "availability": "unknown"}).allowed
    store = WorkflowStore(tmp_path / "db")
    conversation(store)
    apply_inbound_event(store, event("Сколько стоит?"))
    assert "price_question" in store.get_conversation("c")["last_labels"]
    apply_inbound_event(store, event("Не пишите", "stop"))
    assert store.is_suppressed({"recipient": "buyer@customer.example"})
    store.close()


def test_window_reopening_does_not_restore_expired_approval(tmp_path):
    box = Outbox(str(tmp_path / "db"))
    closed = datetime(2026, 11, 12, 5, tzinfo=UTC)
    opened = closed + timedelta(hours=5)
    payload = {"id": "a", "recipient": "buyer@customer.example", "sender_account": "sender@example.org",
        "message_key": "a", "source_url": "https://source.example/request/1", "kind": "initial", "body": "Draft", "subject": "Task",
        "dependency_version": "catalog", "contact_permission": {"status": "allowed"},
        "contact_window": {"timezone": "Europe/Moscow", "start": "10:00", "end": "18:00"},
        "assessment": {"allowed": True, "decision": "propose_message"}}
    box.workflow.queue_action(payload)
    box.workflow.approve_action("a", "owner", ttl_seconds=60, now=closed)
    assert evaluate_action({**payload, "contact_permission": "allowed"}, now=closed).decision == "defer"
    assert evaluate_action({**payload, "contact_permission": "allowed"}, now=opened).allowed
    with pytest.raises(DispatchBlocked, match="approval_expired"):
        box.authorize_delivery("a", OutreachConfig(enabled=True, from_email="sender@example.org"), current_dependencies="catalog", now=opened)
    assert box.db.execute("SELECT COUNT(*) FROM outbound_messages").fetchone()[0] == 0
    box.close()


def test_refresh_unattempted_action_uses_current_catalog_and_invalidates_approval(tmp_path):
    import json

    from b2b_agent.catalog import initialize_input

    root = tmp_path / "INPUT"
    config = Config(input_path=str(root), outreach={"from_email": "sender@example.org", "signature": "Sender"})
    initialize_input(root, config)
    page = Page("https://source.example/request/1", "", '<main><h1>Обработка заявок</h1><time datetime="2026-11-11"></time><p>Ищем подрядчика для обработки заявок от организаций.</p><p>Для откликов пишите buyer@customer.example</p></main>', [])
    inspected = inspect_ad(page, config, datetime(2026, 11, 12, tzinfo=UTC))
    box = Outbox(str(tmp_path / "db"))
    action = queue_public_request(box, inspected, config)
    box.workflow.approve_action(action["id"], "owner")
    path = root / "services.json"
    document = json.loads(path.read_text())
    document["services"][0]["price"] = {"minimum": 12345, "currency": "RUB", "source": "owner"}
    path.write_text(json.dumps(document))
    refreshed = refresh_action(box, action["id"], config)
    assert refreshed["status"] == "pending_approval"
    assert refreshed["dependency_version"] == current_catalog(config).dependency_version(refreshed["service_id"])
    assert "12345 RUB" in refreshed["body"]
    assert box.workflow.approval_blocker(refreshed, current_dependencies=refreshed["dependency_version"]) == "pending_approval"
    box.close()


def test_transport_rechecks_window_and_permission_after_reservation(tmp_path, monkeypatch):
    box = Outbox(str(tmp_path / "db"))
    now = datetime(2026, 11, 12, 11, 59, tzinfo=UTC)
    action = {"id": "a", "recipient": "buyer@customer.example", "sender_account": "sender@example.org",
        "message_key": "a", "source_url": "https://source.example/1", "kind": "initial", "body": "Draft", "subject": "Task",
        "dependency_version": "catalog", "contact_permission": {"status": "allowed", "expires_at": (now + timedelta(minutes=30)).isoformat()},
        "contact_window": {"timezone": "UTC", "start": "09:00", "end": "12:00"},
        "assessment": {"allowed": True, "decision": "propose_message"}}
    box.workflow.queue_action(action)
    box.workflow.approve_action("a", "owner", ttl_seconds=3600, now=now)
    permit = box.authorize_delivery("a", OutreachConfig(enabled=True, from_email="sender@example.org"), current_dependencies="catalog", now=now)
    monkeypatch.setattr("b2b_agent.outbox.utcnow", lambda: now + timedelta(minutes=2))
    with pytest.raises(DispatchBlocked, match="outside_contact_window"):
        permit.assert_valid(action["recipient"], action["subject"], action["body"], action["message_key"])
    box.close()


def test_confirmed_interest_removes_only_obsolete_interest_unknown(tmp_path):
    store = WorkflowStore(tmp_path / "db")
    conversation(store)
    c = store.get_conversation("c")
    c["unknowns"] = ["Интерес к нашему предложению не подтверждён", "Объём и бюджет неизвестны"]
    store.put_conversation(c)
    apply_inbound_event(store, event("Интересно, давайте обсудим применимость"))
    c = store.get_conversation("c")
    assert c["interest"] == "confirmed_in_conversation"
    assert c["unknowns"] == ["Объём и бюджет неизвестны"]
    assert c["confirmed"][0]["source_ref"] == c["last_inbound_id"]
    store.close()
