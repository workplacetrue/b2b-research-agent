import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from b2b_agent.action_policy import (
    BudgetExceeded,
    BudgetLedger,
    assessment_signature,
    cost_policy_version,
    evaluate_action,
)
from b2b_agent.company_stats import build_report, export_stats, get_company_stats
from b2b_agent.memory import (
    ContextBudgetExceeded,
    build_action_context,
    build_company_capsule,
    check_context_budget,
    estimate_tokens,
)
from b2b_agent.state import WorkflowStore


def test_unknown_paid_cost_is_not_free_and_optout_needs_no_paid_action():
    assessment = evaluate_action({"kind": "llm", "paid": True})
    assert not assessment.allowed
    assert assessment.incremental_cost.upper is None
    assert "unknown" in assessment.reasons[0]
    stop = evaluate_action({"kind": "reply", "paid": True, "process_optout": True}, {"is_terminal": True})
    assert stop.allowed and not stop.approval_required
    assert stop.incremental_cost.upper == 0


def test_known_research_uses_cache_and_fresh_dialogue_requires_new_approval():
    cached = evaluate_action({"kind": "search", "known_answer": "CRM X", "verified_free": True})
    assert cached.decision == "cheaper_alternative" and not cached.allowed
    message = evaluate_action({"kind": "reply", "contact_permission": "allowed", "verified_free": True})
    assert message.allowed and message.approval_required


def test_terminal_defer_pause_and_commercial_boundaries():
    now = datetime(2026, 11, 12, tzinfo=UTC)
    assert evaluate_action({}, {"is_terminal": True}).decision == "stop"
    assert evaluate_action({}, {"human_takeover": True}).decision == "human_handoff"
    future = now + timedelta(days=10)
    assert evaluate_action({}, {"next_review_at": future.isoformat()}, now=now).decision == "defer"
    assert evaluate_action({"promises_start_date": True}).decision == "human_handoff"
    assert evaluate_action({"new_commercial_commitment": True}).decision == "human_handoff"
    assert evaluate_action({"kind": "reply", "contact_permission": "unknown"}).decision == "human_handoff"


def test_budget_reserves_upper_reconciles_actual_and_keeps_unknown(tmp_path):
    ledger = BudgetLedger(sqlite3.connect(tmp_path / "budget.sqlite"))
    caps = {"company:c": {"amount": 10, "max_calls": 3, "max_tokens": 1000}}
    ledger.reserve("one", 6, "USD", ["company:c"], caps, tokens=400)
    assert ledger.reserve("one", 6, "USD", ["company:c"], caps, tokens=400)["reused"]
    with pytest.raises(BudgetExceeded):
        ledger.reserve("two", 5, "USD", ["company:c"], caps)
    ledger.reconcile("one", 2, billing_known=True)
    ledger.reserve("two", 5, "USD", ["company:c"], caps)
    ledger.reconcile("two", actual_amount=0, billing_known=False)
    assert ledger.totals("company:c") == {"USD": 7}
    with pytest.raises(BudgetExceeded, match="Currencies"):
        ledger.reserve("euro", 1, "EUR", ["company:c"], caps)


def test_budget_atomic_across_connections(tmp_path):
    path = tmp_path / "shared.sqlite"
    BudgetLedger(sqlite3.connect(path))

    def reserve(identifier):
        connection = sqlite3.connect(path, timeout=10)
        ledger = BudgetLedger(connection)
        try:
            ledger.reserve(identifier, 7, "USD", ["campaign:x"], {"campaign:x": {"amount": 10}})
            return True
        except BudgetExceeded:
            return False
        finally:
            connection.close()

    with ThreadPoolExecutor(2) as workers:
        assert sum(workers.map(reserve, ["a", "b"])) == 1


def test_call_limits_even_when_cost_is_zero(tmp_path):
    ledger = BudgetLedger(sqlite3.connect(tmp_path / "calls.sqlite"))
    caps = {"run:r": {"max_calls": 1, "max_tokens": 100}}
    ledger.reserve("first", 0, "USD", ["run:r"], caps, tokens=50)
    with pytest.raises(BudgetExceeded, match="Call"):
        ledger.reserve("second", 0, "USD", ["run:r"], caps, tokens=50)


def test_stats_derive_unique_events_separate_auto_human_and_meeting(tmp_path):
    store = WorkflowStore(tmp_path / "stats.sqlite")
    store.put_conversation({"id": "c", "account_id": "a", "stage": "exploring"})
    event = {"provider_message_id": "m1", "sender": "x@example.com", "mailbox_id": "inbox", "received_at": "2026-11-12T08:00:00+00:00"}
    assert store.apply_inbound(event, "c", labels=["interest", "price_question", "case_request"])
    assert not store.apply_inbound(event, "c", labels=["interest", "price_question", "case_request"])
    store.apply_inbound({**event, "provider_message_id": "m2", "is_auto_reply": True}, "c", labels=["ooo"])
    store.record_event("meeting_proposed", "c", {}, event_id="meet1")
    store.record_event("meeting_proposed", "c", {}, event_id="meet1")
    store.record_event("meeting_scheduled", "c", {}, event_id="meet2")
    stats = get_company_stats(store, "a")
    assert stats.human_replies == 1 and stats.auto_replies == 1
    assert stats.price_requests == 1 and stats.case_requests == 1
    assert stats.meetings_proposed == stats.meetings_scheduled == 1
    assert stats.meetings_held == 0 and stats.complaints is None
    assert stats.response_rate["denominator"] == 0
    assert stats.response_rate["value"] is None
    version = stats.stats_version
    evaluate_action({"id": "review", "conversation_id": "c", "verified_free": True}, {}, stats, store=store)
    assert get_company_stats(store, "a").stats_version == version
    paths = export_stats(store, tmp_path / "STATS")
    assert len(paths) == 2 and paths[0].name == "companies.csv"


def test_stats_approval_does_not_invalidate_meaningful_source_version(tmp_path):
    store = WorkflowStore(tmp_path / "stats.sqlite")
    store.put_conversation({"id": "c", "account_id": "a"})
    action = store.queue_action({"id": "a1", "conversation_id": "c", "recipient": "x@example.com", "subject": "Task", "body": "Question"})
    before = get_company_stats(store, "a").stats_version
    store.approve_action(action["id"], "owner")
    assert get_company_stats(store, "a").stats_version == before


def test_context_counts_system_tools_and_preserves_long_tail_stop():
    assert estimate_tokens("Я") == len("Я".encode("utf-8"))
    with pytest.raises(ContextBudgetExceeded):
        check_context_budget({"system": "s" * 500, "tools": [{"schema": "x" * 500}]}, budget=800)
    long_text = "Уточнение " * 2000 + "Не пишите мне больше"
    with pytest.raises(ContextBudgetExceeded):
        build_action_context({}, {}, {}, long_text, total_budget=4000)
    assert long_text.endswith("Не пишите мне больше")


def test_capsule_critical_constraints_hold_instead_of_silent_truncation(tmp_path):
    store = WorkflowStore(tmp_path / "capsule.sqlite")
    constraints = [{"text": "Лимит 250000 RUB; не звонить; Europe/Moscow; вернуться 2027-02-01", "source_ref": "m1"}] * 20
    store.put_conversation({"id": "c", "account_id": "a", "constraints": constraints, "promises": [{"text": "Не обещали дату старта", "ref": "m2"}]})
    capsule = build_company_capsule(store, "a", context_budget=100)
    assert capsule.hold_reason and "context_budget" in capsule.holds
    assert "250000 RUB" in capsule.constraints[0]["text"]
    with pytest.raises(ContextBudgetExceeded):
        build_action_context(capsule, {}, {}, "ok")


def test_initial_public_request_queue_assessment_and_capsule(tmp_path):
    from b2b_agent.config import Config
    from b2b_agent.outbox import Outbox
    from b2b_agent.outreach import OutboundAssessment
    from b2b_agent.workflow import assess_current_action, queue_public_request, workflow_store

    config = Config(input_path=str(tmp_path / "absent"), outreach={"from_email": "owner@example.com", "signature": "Owner"})
    box = Outbox(str(tmp_path / "queue.sqlite"))
    result = OutboundAssessment(source_url="https://marketplace.example/task/123", title="Нужна обработка заявок", recipient="buyer@example.com", workflow="sales_intake", subject="По вашей задаче", body="Здравствуйте! Обращаемся по вашему запросу.", message_key="public-request-key", task_fit="full", status="ready")
    action = queue_public_request(box, result, config)
    store = workflow_store(box)
    assert action["status"] == "pending_approval"
    assert assess_current_action(store, action, config).allowed
    assert assess_current_action(store, action, config).approval_required
    stats = get_company_stats(store, action["opportunity_id"])
    capsule = build_company_capsule(store, action["opportunity_id"])
    assert stats.messages_sent == 0 and not stats.is_terminal
    assert capsule.conversation_ids == [action["conversation_id"]]
    assert action["assessment"]["stats_version"] == stats.stats_version
    store.reject_action(action["id"], "owner", "Needs wording edits")
    assert get_company_stats(store, action["opportunity_id"]).delivery_errors == 0
    assert get_company_stats(store, action["opportunity_id"]).send_attempts == 0
    assert build_report(store)["operator_rejections"] == 1


def test_optout_interest_priority_and_persistent_pause_in_capsule(tmp_path):
    store = WorkflowStore(tmp_path / "priority.sqlite")
    store.put_conversation({"id": "c", "account_id": "a", "recipient": "x@example.com"})
    store.apply_inbound({"provider_message_id": "m", "mailbox_id": "i", "sender": "x@example.com"}, "c", labels=["interest", "opt_out"], stop_scope="contact")
    stats = get_company_stats(store, "a")
    assert stats.optouts == 1 and stats.substantive_interest == 0
    assert stats.suppression_scopes[0]["scope"] == "contact"
    assert build_company_capsule(store, "a").suppressed
    store.pause("human_takeover", "c")
    assert build_company_capsule(store, "a").human_takeover
    assert build_report(store)["substantive_interest"] == 0


def test_material_policy_and_context_changes_require_review():
    from b2b_agent.config import Config

    config = Config()
    original = cost_policy_version(config)
    changed = config.model_copy(update={"budget": config.budget.model_copy(update={"max_run_cost": 10})})
    assert cost_policy_version(changed) != original
    prepared = evaluate_action({"kind": "reply", "contact_permission": "allowed", "verified_free": True})
    changed_assessment = prepared.model_copy(update={"reputational_risk": "high"})
    assert assessment_signature(prepared) != assessment_signature(changed_assessment)
    held = evaluate_action({"kind": "reply", "contact_permission": "allowed"}, {"holds": ["context_budget"]})
    assert not held.allowed and held.decision == "human_handoff"
    optout = evaluate_action({"process_optout": True}, {"holds": ["context_budget"]})
    assert optout.allowed
