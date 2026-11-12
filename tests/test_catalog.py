import json

import pytest
from pydantic import ValidationError

from b2b_agent.catalog import (
    CatalogValidationError,
    initialize_input,
    load_legacy_catalog,
    load_service_catalog,
)
from b2b_agent.config import Config
from b2b_agent.models import Research


def write_json(path, content):
    path.write_text(json.dumps(content), encoding="utf-8")


@pytest.fixture
def owner_input(tmp_path):
    path = tmp_path / "INPUT"
    initialize_input(path, Config())
    return path


def test_edit_services_without_python_and_keep_unrelated_dependency(owner_input):
    before = load_service_catalog(owner_input)
    version = before.dependency_version("sales_intake")
    path = owner_input / "services.json"
    data = json.loads(path.read_text())
    data["services"].append({
        "id": "warehouse_labels", "name": "Печать этикеток", "workflow": "warehouse_labels",
        "supported_tasks": ["печать этикеток"],
    })
    write_json(path, data)
    after = load_service_catalog(owner_input)
    assert after.service("warehouse_labels").supported_tasks == ("печать этикеток",)
    assert before.version != after.version
    assert version == after.dependency_version("sales_intake")
    with pytest.raises(ValidationError):
        after.service("sales_intake").name = "tampered"


def test_related_price_and_sender_changes_invalidate(owner_input):
    first = load_service_catalog(owner_input)
    path = owner_input / "services.json"
    data = json.loads(path.read_text())
    data["services"][0]["price"] = {"minimum": 100, "currency": "RUB", "source": "owner"}
    write_json(path, data)
    second = load_service_catalog(owner_input)
    assert first.dependency_version("sales_intake") != second.dependency_version("sales_intake")
    sender = owner_input / "sender_profile.json"
    data = json.loads(sender.read_text())
    data["signature"] = "Реальная подпись"
    write_json(sender, data)
    third = load_service_catalog(owner_input)
    assert second.dependency_version("sales_quotes") != third.dependency_version("sales_quotes")


def test_invalid_partial_json_never_returns_old_snapshot(owner_input):
    old = load_service_catalog(owner_input)
    (owner_input / "services.json").write_text('{"services": [')
    with pytest.raises(CatalogValidationError, match="services.json"):
        load_service_catalog(owner_input)
    assert old.service("sales_intake").price.minimum is None


def test_catalog_cannot_disable_approval_or_execute(owner_input):
    path = owner_input / "services.json"
    data = json.loads(path.read_text())
    data["disable_suppression"] = True
    write_json(path, data)
    with pytest.raises(CatalogValidationError, match="disable_suppression"):
        load_service_catalog(owner_input)


def test_material_path_cannot_escape_or_follow_escaping_symlink(owner_input, tmp_path):
    outside = tmp_path / "private.txt"
    outside.write_text("private")
    (owner_input / "escape").symlink_to(outside)
    path = owner_input / "proof_assets.json"
    for unsafe in ("../private.txt", str(outside), "escape"):
        write_json(path, {"assets": [{"id": "case", "type": "demo", "path": unsafe}]})
        with pytest.raises(CatalogValidationError, match="inside INPUT"):
            load_service_catalog(owner_input)


def test_empty_cases_are_unknown_and_do_not_create_proof(owner_input):
    snapshot = load_service_catalog(owner_input)
    assert snapshot.public_case_status == "unknown"
    assert snapshot.proof_assets == ()
    assert all(offer.price.minimum is None for offer in snapshot.services)
    path = owner_input / "proof_assets.json"
    write_json(path, {"public_case_status": "none_confirmed", "assets": []})
    with pytest.raises(CatalogValidationError, match="status_source"):
        load_service_catalog(owner_input)


def test_proof_rights_change_relevant_snapshot(owner_input):
    asset = {"id": "intake_demo", "type": "demo", "availability": "available",
             "publication_allowed": True, "allowed_summary": "Тестовый механизм",
             "url": "https://example.org/demo", "service_ids": ["sales_intake"]}
    write_json(owner_input / "proof_assets.json", {"assets": [asset]})
    path = owner_input / "services.json"
    services = json.loads(path.read_text())
    services["services"][0]["proof_asset_ids"] = ["intake_demo"]
    write_json(path, services)
    first = load_service_catalog(owner_input)
    asset["publication_allowed"] = False
    write_json(owner_input / "proof_assets.json", {"assets": [asset]})
    second = load_service_catalog(owner_input)
    assert first.dependency_version("sales_intake") != second.dependency_version("sales_intake")
    assert first.dependency_version("documents") == second.dependency_version("documents")


def test_init_is_non_overwriting_and_audit_snapshot_is_exact(owner_input, tmp_path):
    sender = owner_input / "sender_profile.json"
    text = sender.read_text()
    assert initialize_input(owner_input, Config()) == []
    assert sender.read_text() == text
    snapshot = load_service_catalog(owner_input, snapshot_dir=tmp_path / "audit")
    target = tmp_path / "audit" / f"{snapshot.version}.json"
    assert target.is_file()
    assert json.loads(target.read_text())["version"] == snapshot.version


def test_legacy_config_and_research_stay_compatible():
    config = Config.model_validate({"outreach": {"enabled": True}, "sender": "Владелец"})
    snapshot = load_legacy_catalog(config)
    assert snapshot.sender_profile.brand == "Владелец"
    assert len(snapshot.services) == 4
    assert Research.model_validate({"domain": "example.org", "website": "https://example.org"}).evidence == []


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan"), "NaN", "Infinity"])
def test_nonfinite_prices_and_budget_limits_are_rejected(owner_input, value):
    path = owner_input / "services.json"
    data = json.loads(path.read_text())
    data["services"][0]["price"] = {"minimum": value, "currency": "USD"}
    write_json(path, data)
    with pytest.raises(CatalogValidationError, match="minimum"):
        load_service_catalog(owner_input)
    with pytest.raises(ValidationError):
        Config(budget={"max_run_cost": value})


def test_catalog_change_between_inspection_and_queue_is_not_silently_approved(owner_input, tmp_path):
    from datetime import UTC, datetime

    from b2b_agent.crawler import Page
    from b2b_agent.outbox import Outbox
    from b2b_agent.outreach import inspect_ad
    from b2b_agent.workflow import queue_public_request

    config = Config(input_path=str(owner_input), outreach={"enabled": True})
    page = Page("https://platform.example/projects/123", "", '''<article>
        <h1>Обработка заявок для производства</h1><time datetime="2026-11-11"></time>
        <p>Ищем подрядчика для автоматизации обработки заявок.</p>
        <p>Для откликов пишите на buyer@example.org</p></article>''', [])
    inspected = inspect_ad(page, config, datetime(2026, 11, 12, tzinfo=UTC))
    assert inspected.status == "ready"
    path = owner_input / "services.json"
    data = json.loads(path.read_text())
    data["services"][0]["price"] = {"minimum": 500, "currency": "USD", "source": "owner"}
    write_json(path, data)
    box = Outbox(str(tmp_path / "state.sqlite"))
    try:
        with pytest.raises(ValueError, match="Catalog changed"):
            queue_public_request(box, inspected, config)
        assert box.db.execute("SELECT COUNT(*) FROM workflow_actions").fetchone()[0] == 0
    finally:
        box.close()


def test_whole_crm_request_cannot_expand_intake_offer(owner_input):
    from datetime import UTC, datetime

    from b2b_agent.crawler import Page
    from b2b_agent.outreach import inspect_ad

    config = Config(input_path=str(owner_input))
    page = Page("https://platform.example/projects/777", "", '''<article>
        <h1>Внедрить CRM полностью</h1><time datetime="2026-11-11"></time>
        <p>Ищем подрядчика: внедрить CRM под ключ для B2B продаж.</p>
        <p>Для откликов пишите на buyer@example.org</p></article>''', [])
    result = inspect_ad(page, config, datetime(2026, 11, 12, tzinfo=UTC))
    assert result.status == "ready"
    assert result.task_fit == "partial"
    assert "полное внедрение CRM не обещаем" in result.body
    assert "под ваш бюджет" not in result.body


def test_effective_sender_fallback_is_included_in_dependency_hash(owner_input):
    from b2b_agent.workflow import current_catalog

    first_config = Config(input_path=str(owner_input), outreach={"signature": "Первая подпись"})
    first = current_catalog(first_config)
    second_config = Config(input_path=str(owner_input), outreach={"signature": "Новая подпись"})
    second = current_catalog(second_config)
    assert first.sender_profile.signature == "Первая подпись"
    assert first.dependency_version("sales_intake") != second.dependency_version("sales_intake")
    assert load_service_catalog(owner_input).sender_profile.signature == ""


def test_content_provenance_rejects_unreviewed_llm_text_and_edited_template():
    from b2b_agent.messaging import stamp_draft_content, validate_draft

    unreviewed = {"source": "llm", "subject": "Предложение", "body": "Гарантируем экономию 90%", "claims": []}
    assert validate_draft(unreviewed)
    template = {"subject": "Вопрос по задаче", "body": "Можно уточнить объём?", "claims": []}
    stamp_draft_content(template)
    assert not validate_draft(template, require_provenance=True)
    template["body"] = "Гарантируем экономию 90%"
    assert validate_draft(template, require_provenance=True)
    with pytest.raises(ValueError, match="individual"):
        stamp_draft_content(template, source="owner_review", actor="llm")
    stamp_draft_content(template, source="owner_review", actor="Иван")
    assert not validate_draft(template, require_provenance=True)


def test_actual_global_extracted_evidence_resolves_only_in_own_snapshot():
    from b2b_agent.analysis import extract, qualify
    from b2b_agent.crawler import Page
    from b2b_agent.messaging import validate_draft

    own = Research(domain="own.example", website="https://own.example")
    other = Research(domain="other.example", website="https://other.example")
    for report in (own, other):
        extract(Page(report.website, "Каталог упаковки оптом. Приём заказов.", "", []), report, Config())
        qualify(report)
    fact = own.evidence[0]
    draft = {"claims": [{"text": fact.quote, "evidence_ids": [fact.id]}]}
    assert fact.id.startswith("ev:")
    assert not validate_draft(draft, own.evidence)
    assert validate_draft(draft, other.evidence)


def test_platform_customers_stay_distinct_but_crosspost_is_one_initial(owner_input, tmp_path):
    from datetime import UTC, datetime

    from b2b_agent.crawler import Page
    from b2b_agent.outbox import Outbox
    from b2b_agent.outreach import inspect_ad
    from b2b_agent.workflow import queue_public_request, workflow_store

    config = Config(input_path=str(owner_input), outreach={"enabled": True})
    def inspect(address, url):
        html = f'''<article><h1>Обработка входящих заявок</h1>
            <time datetime="2026-11-11"></time>
            <p>Ищем подрядчика для автоматизации обработки заявок.</p>
            <p>Для откликов пишите на {address}</p></article>'''
        return inspect_ad(Page(url, "", html, []), config, datetime(2026, 11, 12, tzinfo=UTC))
    box = Outbox(str(tmp_path / "queue.sqlite"))
    try:
        first = queue_public_request(box, inspect("first@buyer.example", "https://platform.example/request/1"), config)
        second = queue_public_request(box, inspect("second@buyer.example", "https://platform.example/request/2"), config)
        repost = queue_public_request(box, inspect("first@buyer.example", "https://other-platform.example/request/3"), config)
        assert first["opportunity_id"] != second["opportunity_id"]
        assert repost["id"] == first["id"]
        assert len(workflow_store(box).list_opportunities()) == 2
        assert len(workflow_store(box).list_actions()) == 2
    finally:
        box.close()


@pytest.mark.parametrize("kind,label", [
    ("client_case", "Разрешённый клиентский пример"),
    ("team_experience", "Личный опыт участника команды"),
    ("demo", "Тестовая демонстрация"),
])
def test_client_team_and_demo_proof_remain_separate(owner_input, kind, label):
    from b2b_agent.messaging import case_answer

    write_json(owner_input / "proof_assets.json", {"assets": [{
        "id": "proof", "type": kind, "service_ids": ["sales_intake"],
        "availability": "available", "publication_allowed": True,
        "allowed_summary": "Обработка входящей заявки", "company_role": "Участник проекта",
        "source": "owner", "url": "https://example.org/material",
    }]})
    catalog = load_service_catalog(owner_input)
    text, refs, blockers = case_answer(catalog, catalog.service("sales_intake"))
    assert label in text and refs == ["proof"] and not blockers
    if kind == "team_experience":
        assert "не внедрение нашей компании" in text
    if kind == "demo":
        assert "не подтверждает производственные результаты" in text


def test_owner_confirmed_no_case_uses_real_first_step_without_demo_promise(owner_input):
    from b2b_agent.messaging import case_answer

    write_json(owner_input / "proof_assets.json", {
        "public_case_status": "none_confirmed", "status_source": "owner confirmed 2026-11-12", "assets": [],
    })
    catalog = load_service_catalog(owner_input)
    text, refs, blockers = case_answer(catalog, catalog.service("sales_intake"))
    assert "Подходящего публичного кейса пока нет" in text and not refs and not blockers
    assert "показать готовое демо" not in text and "бесплатн" not in text
    assert "Можем обсудить применимость" in text
    pilot_offer = catalog.service("sales_intake").model_copy(update={"first_steps": ("ограниченный пилот",)})
    pilot, _, _ = case_answer(catalog, pilot_offer)
    assert "согласовать небольшой проверяемый пилот" in pilot
    assert "стоимость и условия остановки" in pilot


@pytest.mark.parametrize("event", [
    "Открыта вакансия менеджера по продажам; ведём заказы в CRM.",
    "Компания привлекла инвестиции в раунде финансирования; развиваем CRM.",
])
async def test_hiring_or_funding_is_an_observation_without_demand_or_budget(event):
    import httpx

    from b2b_agent.analysis import analyze, extract
    from b2b_agent.crawler import Page

    report = Research(domain="business.example", website="https://business.example")
    extract(Page(report.website, event, "", []), report, Config())
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: pytest.fail("No external call expected"))) as client:
        await analyze(report, Config(), client)
    observations = [fact for fact in report.facts if fact.kind == "business_event"]
    assert observations and observations[0].quote in event
    assert not [fact for fact in report.facts if fact.kind == "buyer_request"]
    assert report.qualification.interest == "unconfirmed"
    assert any("бюджет" in unknown.lower() for unknown in report.score.unknowns)
    assert report.assessment["timing"]["value"] == "unknown"
    if report.draft:
        assert not any(word in report.draft.lower() for word in ("есть бюджет", "получили бюджет", "теряете", "всё вручную"))


@pytest.mark.parametrize("attribute,content", [
    ("", '<p>Архивное объявление</p><p>Ищем подрядчика для обработки заявок.</p>'),
    (' data-status="archived"', '<p>Ищем подрядчика для обработки заявок.</p>'),
    (' class="archived"', '<p>Ищем подрядчика для обработки заявок.</p>'),
    ("", '<blockquote>Ищем подрядчика для обработки заявок.</blockquote>'),
    ("", '<p>Раньше клиент говорил: «Ищем подрядчика для обработки заявок».</p>'),
    ("", '<p>Ищем подрядчика для автоматизации документов для личных домашних дел, не для бизнеса.</p>'),
])
def test_archived_quoted_and_explicit_personal_requests_are_not_buyer_responses(owner_input, attribute, content):
    from datetime import UTC, datetime

    from b2b_agent.crawler import Page
    from b2b_agent.outreach import inspect_ad

    page = Page("https://platform.example/project/123", "", f'''<article{attribute}>
        <h1>Обработка обращений</h1><time datetime="2026-11-11"></time>
        {content}<p>Для откликов пишите на buyer@example.org</p></article>''', [])
    result = inspect_ad(page, Config(input_path=str(owner_input)), datetime(2026, 11, 12, tzinfo=UTC))
    assert result.status == "needs_review"
    assert result.body is None and result.message_key is None


@pytest.mark.parametrize("evidence_state", [
    {"freshness": "stale"},
    {"freshness": "fresh", "contradictions": ["inbound:already_automated"]},
])
async def test_stale_or_contradictory_evidence_blocks_even_approved_dispatch(tmp_path, evidence_state):
    import httpx

    from b2b_agent.messaging import stamp_draft_content
    from b2b_agent.outbox import DispatchBlocked, Outbox
    from b2b_agent.workflow import current_catalog, dispatch_action

    config = Config(input_path=str(tmp_path / "absent"),
                    outreach={"from_email": "owner@example.org", "signature": "Owner"},
                    memory={"capsule_tokens": 10000})
    box = Outbox(str(tmp_path / "state.sqlite"))
    try:
        catalog = current_catalog(config)
        box.workflow.put_conversation({"id": "c", "opportunity_id": "o", "recipient": "buyer@example.org", "participants": ["buyer@example.org"]})
        quote = "Наш процесс требует проверки."
        draft = {"id": "draft", "opportunity_id": "o", "conversation_id": "c", "service_id": "sales_intake",
                 "recipient": "buyer@example.org", "sender_account": config.outreach.from_email,
                 "subject": "Уточнение задачи", "body": quote, "kind": "initial",
                 "source_url": "https://buyer.example/request/1", "message_key": "message",
                 "dependency_version": catalog.dependency_version("sales_intake"),
                 "contact_permission": {"status": "allowed"},
                 "claims": [{"text": quote, "evidence_ids": ["research_current:f1"]}],
                 "evidence_snapshot": [{"id": "research_current:f1", "research_version": "research_current", "quote": quote, **evidence_state}]}
        stamp_draft_content(draft)
        action = box.workflow.queue_action(draft)
        box.workflow.approve_action(action["id"], "owner")
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: pytest.fail("No transport call expected"))) as client:
            with pytest.raises(DispatchBlocked, match="draft_validation"):
                await dispatch_action(box, action["id"], config, client, simulated=True)
        assert box.db.execute("SELECT COUNT(*) FROM outbound_messages").fetchone()[0] == 0
    finally:
        box.close()


async def test_runtime_prompt_capture_excludes_unrelated_catalog_archives_and_spec(owner_input, tmp_path, monkeypatch):
    import httpx

    from b2b_agent.analysis import analyze, extract
    from b2b_agent.crawler import Page
    from b2b_agent.memory import build_action_context, check_context_budget
    from b2b_agent.messaging import case_answer
    from b2b_agent.storage import Store

    marker_offer = "UNRELATED_SERVICE_731"
    marker_case = "UNRELATED_CASE_492"
    marker_archive = "ARCHIVED_PERSONAL_MESSAGE_386"
    marker_spec = "FULL_DEVELOPER_SPEC_156"
    path = owner_input / "services.json"
    data = json.loads(path.read_text())
    data["services"].append({"id": "unrelated", "name": marker_offer, "supported_tasks": [marker_offer]})
    write_json(path, data)
    write_json(owner_input / "proof_assets.json", {"assets": [
        {"id": "intake_demo", "type": "demo", "service_ids": ["sales_intake"], "availability": "available", "publication_allowed": True,
         "allowed_summary": "Разрешённый пример обработки заявки", "url": "https://example.org/demo"},
        {"id": "unrelated_case", "type": "client_case", "service_ids": ["unrelated"], "availability": "available", "publication_allowed": True,
         "allowed_summary": marker_case, "url": "https://example.org/unrelated"},
    ]})
    catalog = load_service_catalog(owner_input)
    store = Store(str(tmp_path / "store.sqlite"))
    store.save(Research(domain="old.example", website="https://old.example", draft=marker_archive, errors=[marker_spec]))
    report = Research(domain="current.example", website="https://current.example")
    extract(Page(report.website, "Каталог. Оптовые продажи. Приём заявок для бизнеса.", "", []), report, Config())
    monkeypatch.setenv("B2B_LLM_API_KEY", "offline-key")
    config = Config(input_path=str(owner_input), llm={"enabled": True}, memory={"input_tokens": 12000},
                    budget={"tool_prices": {"llm": {"per_call": .01, "currency": "USD", "source": "mock tariff"}}})
    captured = []
    def handle(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"hypotheses":[]}'}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        await analyze(report, config, client, store)
    assert len(captured) == 1
    actual_prompt = json.dumps(captured[0], ensure_ascii=False)
    for forbidden in (marker_offer, marker_case, marker_archive, marker_spec):
        assert forbidden not in actual_prompt
    assert report.facts[0].id in actual_prompt
    check_context_budget(captured[0], budget=config.memory.input_tokens)
    selected = catalog.service("sales_intake")
    answer, refs, blockers = case_answer(catalog, selected, "Пришлите кейс по обработке заявок")
    assert refs == ["intake_demo"] and not blockers
    action_context = build_action_context({}, {}, selected, answer, system="Use only source-linked facts", tools=[{"name": "classify_reply"}])
    serialized = json.dumps(action_context, ensure_ascii=False)
    assert "Разрешённый пример обработки заявки" in serialized
    for forbidden in (marker_offer, marker_case, marker_archive, marker_spec):
        assert forbidden not in serialized
    store.close()
