"""Independent contracts from the owner's 2026-11-17 test plan."""
import itertools
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from b2b_agent.config import OutreachConfig
from b2b_agent.conversations import apply_inbound_event, classify_reply
from b2b_agent.outbox import DispatchBlocked, Outbox

CASES = json.loads((Path(__file__).parents[1] / "evaluation/cases/replies.json").read_text())
NOW = datetime(2026, 11, 17, 9, tzinfo=UTC)


def queue(box):
    box.workflow.put_conversation({"id": "c", "account_id": "company",
        "mailbox_id": "test", "participants": ["buyer@example.com"],
        "recipient": "buyer@example.com", "provider_thread_id": "thread"})
    return box.workflow.queue_action({"id": "a", "conversation_id": "c",
        "account_id": "company", "recipient": "buyer@example.com",
        "sender_account": "owner@example.org", "subject": "Task", "body": "Reviewed text",
        "source_url": "https://platform.example/request/1", "message_key": "m",
        "dependency_version": "catalog", "contact_permission": {"status": "allowed"},
        "assessment": {"allowed": True, "decision": "propose_message"}})


def settings():
    return OutreachConfig(enabled=True, from_email="owner@example.org")


def dispatch(box, action, calls, now=NOW):
    # Deliberately hostile upper-level decision: always attempt to send.
    permit = box.authorize_delivery(action["id"], settings(),
                                   current_dependencies="catalog", now=now)
    permit.assert_valid(action["recipient"], action["subject"], action["body"],
                        action["message_key"])
    calls.append({key: action[key] for key in ("recipient", "subject", "body")})
    box.finish(action["message_key"], "simulated_sent", "fake-provider")


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_reply_contracts_have_persistent_observable_outcomes(tmp_path, case):
    path = str(tmp_path / "state.sqlite")
    box = Outbox(path)
    action = queue(box)
    box.workflow.approve_action("a", "owner", now=NOW)
    event = {"mailbox_id": "test", "provider_message_id": "inbound-1",
             "sender": "buyer@example.com", "thread_id": "thread", "body": case["body"]}
    labels = classify_reply(event).labels
    assert set(case["required_labels"]) <= set(labels)
    assert not set(case["forbidden_labels"]) & set(labels)
    apply_inbound_event(box.workflow, event)
    assert apply_inbound_event(box.workflow, event)["status"] == "duplicate"
    assert len(box.workflow.list_inbound()) == 1
    box.close()
    box = Outbox(path)
    assert box.workflow.is_suppressed(action) == case["suppressed"]
    assert box.workflow.get_conversation("c").get("stage") != "meeting_scheduled"
    calls = []
    with pytest.raises(DispatchBlocked):
        dispatch(box, action, calls)
    assert calls == []
    assert box.db.execute("SELECT COUNT(*) FROM outbound_messages").fetchone()[0] == 0
    box.close()


@pytest.mark.parametrize("operations", list(itertools.permutations(["restart", "pause", "resume"])))
def test_suppression_survives_operation_sequences(tmp_path, operations):
    path = str(tmp_path / "state.sqlite")
    box = Outbox(path)
    action = queue(box)
    box.workflow.approve_action("a", "owner", now=NOW)
    box.workflow.suppress("contact", action["recipient"], "customer refusal")
    calls = []
    for operation in operations:
        if operation == "restart":
            box.close()
            box = Outbox(path)
        else:
            getattr(box.workflow, operation)()
        with pytest.raises(DispatchBlocked):
            dispatch(box, action, calls)
    assert calls == []
    assert box.workflow.is_suppressed(action)
    box.close()


def test_positive_control_exactly_one_simulation(tmp_path, monkeypatch):
    monkeypatch.setattr("b2b_agent.outbox.utcnow", lambda: NOW)
    box = Outbox(str(tmp_path / "state.sqlite"))
    action = queue(box)
    box.workflow.approve_action("a", "owner", now=NOW)
    calls = []
    dispatch(box, action, calls)
    assert calls == [{"recipient": action["recipient"], "subject": "Task", "body": "Reviewed text"}]
    assert box.workflow.get_action("a")["status"] == "simulated_sent"
    with pytest.raises(DispatchBlocked):
        dispatch(box, action, calls, now=NOW + timedelta(seconds=1))
    assert len(calls) == 1
    box.close()
