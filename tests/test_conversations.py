from datetime import UTC, datetime

import pytest

from b2b_agent.catalog import CatalogSnapshot
from b2b_agent.config import Config
from b2b_agent.conversations import apply_inbound_event, classify_reply, prepare_followup
from b2b_agent.engagement import recommend_next_action
from b2b_agent.meetings import assess_meeting_readiness, build_meeting_brief, mark_meeting
from b2b_agent.messaging import case_answer, price_answer, validate_draft
from b2b_agent.models import AllowedClaim, PriceModel, ProofAsset, SenderProfile, ServiceOffer
from b2b_agent.state import WorkflowStore


@pytest.fixture
def store(tmp_path):
    value = WorkflowStore(tmp_path / "state.db")
    value.put_opportunity({"id": "opp", "origin": "public_request", "task": "Приём заявок", "service_id": "sales_intake"})
    value.put_conversation({"id": "conv", "opportunity_id": "opp", "mailbox_id": "default",
        "participants": ["buyer@client.test"], "recipient": "buyer@client.test", "provider_thread_id": "thread-1",
        "rfc_message_ids": ["<first@sender.test>"], "service_id": "sales_intake", "task": "Приём заявок",
        "contact_permission": {"status": "allowed", "scope": "request:opp"}})
    yield value
    value.close()


@pytest.fixture
def catalog():
    return CatalogSnapshot(version="catalog-1", services=(ServiceOffer(id="sales_intake", name="Приём заявок",
        allowed_claims=(AllowedClaim(text="Подготовка заявки для менеджера", source="owner"),)),),
        sender_profile=SenderProfile(name="Оператор", from_email="sender@sender.test", signature="Оператор"))


def event(body, message="provider-1", **extra):
    return {"mailbox_id": "default", "provider_message_id": message, "sender": "buyer@client.test",
            "thread_id": "thread-1", "rfc_message_id": f"<{message}@client.test>", "body": body, **extra}


def test_stop_priority_full_tail_and_atomic_cancellation(store):
    action = store.queue_action({"conversation_id": "conv", "recipient": "buyer@client.test", "body": "Draft", "kind": "followup"})
    store.approve_action(action["id"], "owner")
    text = "Интересно! " + "Нейтральный контекст. " * 5000 + "Не пишите мне больше."
    result = apply_inbound_event(store, event(text))
    assert {"interest", "opt_out"} <= set(result["labels"])
    assert result["next_action"]["action"] == "stop"
    assert store.get_action(action["id"])["status"] == "cancelled"
    assert store.is_suppressed({"recipient": "buyer@client.test"})
    assert store.get_conversation("conv")["is_terminal"]


def test_quoted_old_refusal_not_reapplied_and_duplicate_no_new_action(store, catalog):
    body = "Пришлите информацию.\n> Не пишите мне\n> Старое письмо"
    first = apply_inbound_event(store, event(body), catalog, Config())
    second = apply_inbound_event(store, event(body), catalog, Config())
    assert first["next_action"]["action"] == "send_information"
    assert second["status"] == "duplicate"
    assert not store.is_suppressed({"recipient": "buyer@client.test"})
    assert len(store.list_inbound()) == 1
    assert len(store.list_actions()) == 1
    assert store.get_conversation("conv")["stage"] == "replied"


def test_similar_subject_different_sender_is_not_a_match(store):
    result = apply_inbound_event(store, event("Интересно", sender="stranger@other.test", subject="Re: Приём заявок"))
    assert result["status"] == "manual_review"
    assert store.get_conversation("conv")["context_version"] == 1


@pytest.mark.parametrize("body,label", [("Давайте созвонимся", "meeting_interest"),
    ("Вручную, но всё устраивает", "satisfied"), ("У нас всё полностью автоматизировано", "automated"),
    ("Нет бюджета", "no_budget"), ("Не моя зона ответственности", "wrong_role"),
    ("Не сейчас, вернитесь в феврале", "defer"), ("Сколько стоит?", "price_question")])
def test_multilabel_observable_interpretations(body, label):
    assert label in classify_reply(event(body)).labels


def test_call_interest_does_not_schedule_and_readiness_no_bant(store, catalog):
    result = apply_inbound_event(store, event("Интересно, давайте созвонимся"), catalog)
    c = store.get_conversation("conv")
    assert c["stage"] == "meeting_proposed"
    assert result["next_action"]["action"] == "prepare_scheduling"
    assert assess_meeting_readiness(c).ready
    with pytest.raises(ValueError, match="confirmation"):
        mark_meeting(store, "conv", "scheduled")
    c = mark_meeting(store, "conv", "scheduled", "2026-11-20T15:00", "Europe/Moscow", confirmed=True)
    assert c["meeting"]["utc"] == "2026-11-20T12:00:00+00:00"
    assert c["stage"] == "meeting_scheduled"
    assert not any(e["kind"] == "meeting_held" for e in store.list_events())
    mark_meeting(store, "conv", "held", notes="Задача подтвердилась")
    assert store.get_conversation("conv")["stage"] == "meeting_held"
    assert build_meeting_brief(store, "conv", catalog).customer_task == "Приём заявок"


def test_auto_replies_and_bounces_never_interest(store):
    for n, extra in enumerate([{"is_auto_reply": True}, {"bounce": "hard"}, {"bounce": "soft"}]):
        assessment = classify_reply(event("Интересно", str(n), **extra))
        assert "interest" not in assessment.labels
        assert recommend_next_action({}, assessment)["action"] == "no_action"


def test_manual_operator_reply_holds_and_invalidates(store):
    action = store.queue_action({"conversation_id": "conv", "recipient": "buyer@client.test", "sender_account": "sender@sender.test", "body": "Draft"})
    store.approve_action(action["id"], "owner")
    apply_inbound_event(store, event("Отвечу клиенту лично", sender="sender@sender.test", recipients=["buyer@client.test"], is_operator=True))
    assert "human_handoff" in store.get_conversation("conv")["holds"]
    assert store.get_action(action["id"])["status"] == "cancelled"


def test_known_price_and_unknown_case_are_honest(store, catalog):
    assert "неизвестна" in price_answer(catalog.services[0])
    priced = catalog.services[0].model_copy(update={"price": PriceModel(minimum=100, maximum=200, currency="RUB", dependencies=("объём",))})
    assert "100–200 RUB" in price_answer(priced)
    answer, _, blockers = case_answer(catalog, catalog.services[0])
    assert answer is None and blockers
    result = apply_inbound_event(store, event("Пришлите кейс, он обязателен"), catalog)
    assert result["next_action"]["action"] == "human_handoff"
    assert not store.list_actions()
    assert "manual_review" in store.get_conversation("conv")["holds"]


def test_proof_demo_label_rights_and_nda(catalog):
    asset = ProofAsset(id="demo", type="demo", service_ids=("sales_intake",), availability="available",
                       publication_allowed=True, allowed_summary="Тестовый приём заявки", source="owner")
    c = catalog.model_copy(update={"proof_assets": (asset,)})
    text, refs, _ = case_answer(c, c.services[0])
    assert "Тестовая демонстрация" in text and "не подтверждает" in text and refs == ["demo"]
    assert case_answer(c, c.services[0], "Клиентский кейс обязателен")[0] is None
    hidden = asset.model_copy(update={"publication_allowed": False, "nda_restrictions": ("Нельзя раскрывать клиента",)})
    assert case_answer(c.model_copy(update={"proof_assets": (hidden,)}), c.services[0])[0] is None


def test_unverified_claim_ids_and_inferences_rejected(catalog):
    draft = {"claims": [{"text": "Вы теряете деньги", "evidence_ids": ["f1"]}]}
    assert validate_draft(draft, [{"id": "f1", "quote": "Каталог", "research_version": "client1"}], catalog.services[0])
    assert validate_draft(draft, [{"id": "client2:f1", "quote": "Каталог"}], catalog.services[0])


def test_no_call_scope_not_universal_permission(store):
    result = apply_inbound_event(store, event("Интересно, но не звоните"))
    assert not store.is_suppressed({"recipient": "buyer@client.test", "channel": "email"})
    assert store.is_suppressed({"recipient": "buyer@client.test", "channel": "phone"})
    assert "no_calls" in result["labels"]


def test_followup_requires_reason_fresh_sync_and_one_allowance(store, catalog):
    action = store.queue_action({"conversation_id": "conv", "recipient": "buyer@client.test", "body": "Initial"})
    store.update_action(action["id"], status="sent")
    with pytest.raises(ValueError, match="concrete"):
        prepare_followup(store, "conv", catalog)
    with pytest.raises(ValueError, match="fresh"):
        prepare_followup(store, "conv", catalog, reason="Есть существенное уточнение по совместимости")
    store.mark_sync("default")
    followup = prepare_followup(store, "conv", catalog, reason="Есть существенное уточнение по совместимости")
    assert followup["status"] == "pending_approval"
    with pytest.raises(ValueError, match="allowance"):
        prepare_followup(store, "conv", catalog, reason="Другое полезное уточнение по задаче")


def test_numeric_constraints_sources_preserved(store):
    apply_inbound_event(store, event("Бюджет 150000 RUB. Срок 2026-12-01. Звонить в 15:30 Europe/Moscow."))
    constraints = store.get_conversation("conv")["constraints"]
    assert any("150000 RUB" in x["value"] for x in constraints)
    assert all(x["source_ref"] and x["author"] == "buyer@client.test" for x in constraints)


def test_forged_operator_flag_cannot_attach_other_sender(store):
    result = apply_inbound_event(store, event("Интересно", sender="attacker@other.test", is_operator=True))
    assert result["status"] == "manual_review"
    assert not store.get_conversation("conv")["holds"]


def test_participant_budget_conflict_is_preserved_with_sources(store):
    c = store.get_conversation("conv")
    c["participants"].append("other@client.test")
    store.put_conversation(c)
    apply_inbound_event(store, event("Бюджет 100000 RUB.", "budget1"))
    apply_inbound_event(store, event("Бюджет 200000 RUB.", "budget2", sender="other@client.test"))
    c = store.get_conversation("conv")
    assert "manual_review" in c["holds"]
    assert c["conflicts"][0]["kind"] == "budget"
    assert {source["author"] for source in c["conflicts"][0]["sources"]} == {"buyer@client.test", "other@client.test"}


def test_mandatory_case_needs_comparability_not_just_service(catalog):
    asset = ProofAsset(id="case1", type="client_case", service_ids=("sales_intake",), availability="available",
        publication_allowed=True, allowed_summary="Заявки в торговле", processes=("Приём заявок",), industry="Торговля", scale="100 в день", source="owner")
    c = catalog.model_copy(update={"proof_assets": (asset,)})
    assert case_answer(c, c.services[0], "Сопоставимый кейс обязателен")[0] is None
    assert case_answer(c, c.services[0], "Сопоставимый кейс обязателен", context={"task": "Приём заявок", "industry": "Торговля", "scale": "100 в день"})[0]
    assert case_answer(c, c.services[0], "Сопоставимый кейс обязателен", context={"task": "Приём заявок", "industry": "Производство", "scale": "100 в день"})[0] is None


def test_satisfied_manual_process_creates_no_pain_or_meeting_draft(store, catalog):
    result = apply_inbound_event(store, event("Вручную, но всё устраивает"), catalog, Config())
    assert result["next_action"]["action"] == "no_action"
    assert "draft" not in result
    assert not store.list_actions()
    assert store.get_conversation("conv").get("interest") is None
    assert not assess_meeting_readiness(store.get_conversation("conv")).ready


def test_automated_process_disproves_opportunity_need_with_inbound_reference(store, catalog):
    apply_inbound_event(store, event("У нас это полностью автоматизировано"), catalog, Config())
    opportunity = store.get_opportunity("opp")
    assert opportunity["need"]["status"] == "disproved"
    assert opportunity["evidence"][-1]["kind"] == "contradiction"
    assert opportunity["evidence"][-1]["inbound_message_id"] == store.list_inbound()[0]["id"]
    assert not store.list_actions()
    # An existing system alone is weaker than a denial of the need.
    assert "automated" not in classify_reply(event("Уже используем CRM")).labels


def test_referral_preserves_source_without_contacting_new_employee(store, catalog):
    result = apply_inbound_event(store, event("Напишите коллеге new.person@client.test, он отвечает за заявки"), catalog, Config())
    assert result["next_action"]["action"] == "human_handoff"
    assert "new.person@client.test" in store.list_inbound()[0]["author_text"]
    assert not store.list_actions()
    assert "new.person@client.test" not in store.get_conversation("conv")["participants"]


def test_takeover_still_processes_optout_without_owner_approval(store, catalog):
    store.pause("human_takeover", "conv", "Owner handles conversation")
    first = apply_inbound_event(store, event("Сколько стоит?", "takeover1"), catalog, Config())
    assert "draft" not in first
    result = apply_inbound_event(store, event("Не пишите мне", "takeover2"), catalog, Config())
    assert "draft" not in result
    assert store.is_suppressed({"recipient": "buyer@client.test"})
    assert len(store.list_inbound()) == 2


def test_company_refusal_from_unverified_role_stops_pending_and_keeps_source(store, catalog):
    c = store.get_conversation("conv")
    c["account_id"] = "client-account"
    c["participants"].append("employee@client.test")
    store.put_conversation(c)
    action = store.queue_action({"conversation_id": "conv", "account_id": "client-account", "recipient": "buyer@client.test", "body": "Draft"})
    apply_inbound_event(store, event("Не обращайтесь в нашу компанию", "company-refusal", sender="employee@client.test", author_role="unknown"), catalog, Config())
    assert store.is_suppressed({"account_id": "client-account", "recipient": "another@client.test"})
    assert store.get_action(action["id"])["status"] == "cancelled"
    inbound = store.list_inbound()[0]
    assert inbound["sender"] == "employee@client.test" and inbound["author_role"] == "unknown"
    apply_inbound_event(store, event("Пишите нам, интересно", "contradictory-welcome"), catalog, Config())
    assert store.is_suppressed({"account_id": "client-account", "recipient": "another@client.test"})
    assert len(store.list_actions()) == 1


@pytest.mark.parametrize("text", ["Сделайте скидку 20%", "Гарантируете результат?", "Подпишите NDA", "Примите условия договора", "Сделайте за 3 дня", "Бесплатный пилот обязателен"])
def test_new_commercial_terms_hand_off_without_new_commitments(store, catalog, text):
    result = apply_inbound_event(store, event(text), catalog, Config())
    assert "commercial_exception" in result["labels"]
    assert result["next_action"]["action"] == "human_handoff"
    assert "manual_review" in store.get_conversation("conv")["holds"]
    assert not store.list_actions()


async def test_offline_full_lifecycle_has_separate_approvals_and_real_metrics(tmp_path):
    from email.message import EmailMessage
    from pathlib import Path

    import httpx

    from b2b_agent.company_stats import get_company_stats
    from b2b_agent.crawler import Page
    from b2b_agent.inbox import import_replies
    from b2b_agent.memory import build_company_capsule
    from b2b_agent.outbox import DispatchBlocked, Outbox
    from b2b_agent.outreach import inspect_ad
    from b2b_agent.workflow import current_catalog, dispatch_action, queue_public_request

    config = Config(input_path=str(tmp_path / "missing-legacy-input"),
        outreach={"from_email": "demo@sender.example", "signature": "Synthetic operator", "enabled": False},
        inbox={"mailbox_id": "demo", "provider": "import"})
    html = (Path(__file__).parents[1] / "examples/offline/request.html").read_text()
    inspected = inspect_ad(Page("https://platform.example/requests/demo-1", "", html, []), config,
                           now=datetime(2026, 11, 12, 12, tzinfo=UTC))
    assert inspected.status == "ready"
    box = Outbox(str(tmp_path / "full.sqlite"))
    try:
        action = queue_public_request(box, inspected, config)
        conv_id = action["conversation_id"]
        account = action["opportunity_id"]
        store = box.workflow
        assert action["status"] == "pending_approval"
        assert store.get_opportunity(account)["evidence"]
        def reject_network(request):
            pytest.fail("Offline lifecycle must never contact network or send an email")
        async with httpx.AsyncClient(transport=httpx.MockTransport(reject_network)) as client:
            with pytest.raises(DispatchBlocked, match="pending_approval"):
                await dispatch_action(box, action["id"], config, client, simulated=True)
            store.approve_action(action["id"], "fixture-owner")
            initial = await dispatch_action(box, action["id"], config, client, simulated=True)
            assert initial["status"] == "simulated_sent"
            with pytest.raises(DispatchBlocked):
                await dispatch_action(box, action["id"], config, client, simulated=True)

            def imported_reply(body, message_id, reply_to):
                mime = EmailMessage()
                mime["From"], mime["To"] = "buyer@customer.example", config.outreach.from_email
                mime["Subject"] = "Re: " + action["subject"]
                mime["Message-ID"], mime["In-Reply-To"] = message_id, reply_to
                mime.set_content(body)
                path = tmp_path / (message_id.strip("<>").replace("@", "-") + ".eml")
                path.write_bytes(mime.as_bytes())
                return import_replies(path, mailbox_id="demo")[0]

            store.mark_sync("demo", success=False)
            price_event = imported_reply("Сколько стоит?", "<price@customer.example>", initial["rfc_message_id"])
            price_update = apply_inbound_event(store, price_event, current_catalog(config), config)
            price_action = price_update["draft"]
            assert price_action["requires_fresh_sync"]
            assert price_action["assessment"]["allowed"]
            assert "неизвестна" in price_action["body"]
            store.mark_sync("demo", success=True)
            with pytest.raises(DispatchBlocked, match="pending_approval"):
                await dispatch_action(box, price_action["id"], config, client, simulated=True)
            store.approve_action(price_action["id"], "fixture-owner")
            answered = await dispatch_action(box, price_action["id"], config, client, simulated=True)

            interested = imported_reply("Интересно. Нам нужно автоматизировать обработку заявок от организаций.",
                                        "<interest@customer.example>", answered["rfc_message_id"])
            store.mark_sync("demo", success=False)
            update = apply_inbound_event(store, interested, current_catalog(config), config)
            store.mark_sync("demo", success=True)
            meeting_draft = update["draft"]
            assert "25 минут" in meeting_draft["body"]
            assert "Разберём один реальный пример" in meeting_draft["body"]
            assert store.get_opportunity(account)["need"]["status"] == "confirmed_in_conversation"
            stats = get_company_stats(store, account)
            assert stats.human_replies == 2 and stats.price_requests == 1
            assert build_company_capsule(store, account).source_refs is not None
            assert stats.messages_sent == 0  # Simulated acceptance never becomes a real send metric.
            store.approve_action(meeting_draft["id"], "fixture-owner")
            proposed_message = await dispatch_action(box, meeting_draft["id"], config, client, simulated=True)
            assert proposed_message["status"] == "simulated_sent"
            assert proposed_message["approval_id"] != initial["approval_id"]

            mark_meeting(store, conv_id, "proposed", actor="fixture-owner")
            mark_meeting(store, conv_id, "scheduled", local_time="2026-11-16T15:00", timezone="Europe/Moscow",
                         actor="fixture-owner", confirmed=True)
            brief = build_meeting_brief(store, conv_id, current_catalog(config))
            assert "Нам нужно" in brief.customer_task and brief.message_references
            assert get_company_stats(store, account).meetings_held == 0
            mark_meeting(store, conv_id, "held", actor="fixture-owner", notes="Проверенная встреча в fixture")
            stats = get_company_stats(store, account)
            assert stats.meetings_scheduled == 1 and stats.meetings_held == 1
            assert len(store.list_actions()) == 3
    finally:
        box.close()
