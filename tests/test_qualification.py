import csv
import json

import httpx
import pytest

from b2b_agent.analysis import analyze, extract, extract_request
from b2b_agent.config import Config
from b2b_agent.crawler import Page
from b2b_agent.discovery import discover_requests, read_requests_csv
from b2b_agent.models import Research
from b2b_agent.pipeline import run
from b2b_agent.storage import Store, export


def report_for(text):
    report = Research(domain="supplier.ru", website="https://supplier.ru")
    extract(Page(report.website, text, "", []), report, Config())
    return report


async def analyze_rules(report):
    async with httpx.AsyncClient() as client:
        await analyze(report, Config(), client)


async def test_sales_and_documents_are_distinct_from_interest(tmp_path):
    report = report_for(
        "Упаковка оптом. Каталог. Заявки. CRM. Повторные заказы. Счета и накладные."
    )
    await analyze_rules(report)
    assert {h.workflow for h in report.hypotheses} == {
        "sales_intake",
        "sales_quotes",
        "sales_followup",
        "documents",
    }
    assert report.score.sales == 100 and report.score.documents == 50
    assert report.qualification.interest == "unconfirmed"
    assert report.qualification.next_step == "check_relevance"
    export([report], str(tmp_path))
    with (tmp_path / "results.csv").open(encoding="utf-8-sig") as f:
        row = next(csv.DictReader(f))
    assert json.loads(row["qualification"])["interest"] == "unconfirmed"
    assert row["sales"] == "100"


@pytest.mark.parametrize(
    "text",
    [
        "Предлагаем услуги автоматизации продаж и внедрения CRM.",
        "Не ищем подрядчика для автоматизации продаж.",
        "Нам не нужна автоматизация продаж.",
        "Ищем подрядчика по автоматизации CRM, но проект закрыт.",
        "Нужна автоматизация продаж? Оставьте заявку.",
        "Нужна автоматизация продаж. Предлагаем внедрение CRM.",
        "Ищем подрядчика по доставке. Автоматизация продаж — другая тема.",
    ],
)
async def test_seller_negation_closed_and_unrelated_are_not_demand(text):
    report = report_for(text)
    await analyze_rules(report)
    assert report.qualification.interest == "unconfirmed"
    assert not report.qualification.request_evidence_ids


async def test_public_request_is_only_a_review_signal():
    report = report_for("Упаковка оптом. Ищем подрядчика для автоматизации продаж и внедрения CRM.")
    await analyze_rules(report)
    assert report.qualification.interest == "public_request_to_verify"
    assert report.qualification.next_step == "verify_request"
    assert report.qualification.request_evidence_ids
    assert any("дату" in reason for reason in report.qualification.reasons)


async def test_third_person_request_is_supported():
    report = report_for(
        "Онлайн-школа ищет подрядчика для внедрения ИИ-менеджера с целью автоматизации продаж."
    )
    await analyze_rules(report)
    assert report.qualification.interest == "public_request_to_verify"
    assert report.draft is None


async def test_non_target_has_no_outreach_even_with_catalog():
    report = report_for("Каталог. Оставьте заявку. Договор.")
    await analyze_rules(report)
    assert report.hypotheses and report.draft is None
    assert report.qualification.next_step == "skip"


async def test_llm_hostile_question_never_enters_outreach(monkeypatch):
    monkeypatch.setenv("B2B_LLM_API_KEY", "test-key")
    report = report_for("Упаковка оптом. Каталог. Заявки.")
    content = {
        "hypotheses": [
            {
                "statement": "У вас плохие продажи и вы теряете деньги",
                "workflow": "sales_quotes",
                "evidence_ids": [f.id for f in report.facts if f.kind in {"order", "catalog"}],
                "validation_question": "Почему у вас всё плохо? Обещаем рост на 50%.",
            }
        ]
    }
    transport = httpx.MockTransport(
        lambda req: httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(content)}}]},
        )
    )
    async with httpx.AsyncClient(transport=transport) as client:
        await analyze(report, Config(llm={"enabled": True}, budget={"tool_prices": {"llm": {"per_call": 0.01, "currency": "USD", "source": "offline mock fixture"}}}, memory={"input_tokens": 12000}), client)
    assert report.draft
    assert not any(word in report.draft for word in ["плохо", "теряете", "50%", "Почему"])
    assert "Актуальна ли" in report.draft


async def test_llm_cannot_propose_workflow_without_its_evidence(monkeypatch):
    monkeypatch.setenv("B2B_LLM_API_KEY", "test-key")
    report = report_for("Упаковка оптом. Каталог. Заявки.")
    content = {
        "hypotheses": [
            {
                "statement": "Автоматизация документов",
                "workflow": "documents",
                "evidence_ids": [report.facts[0].id],
                "validation_question": "Актуально?",
            }
        ]
    }
    transport = httpx.MockTransport(
        lambda req: httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(content)}}]},
        )
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ValueError, match="not supported"):
            await analyze(report, Config(llm={"enabled": True}, budget={"tool_prices": {"llm": {"per_call": 0.01, "currency": "USD", "source": "offline mock fixture"}}}, memory={"input_tokens": 12000}), client)
    assert report.draft is None


async def test_request_search_preserves_post_url_and_is_not_company_discovery(monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "test-key")
    transport = httpx.MockTransport(
        lambda req: httpx.Response(
            200,
            json={
                "web": {
                    "results": [
                        {
                            "url": "https://platform.ru/posts/123",
                            "title": "Ищем подрядчика",
                            "description": "CRM",
                        },
                        {"url": "https://platform.ru/posts/123#reply"},
                        {"url": "http://127.0.0.1"},
                    ]
                }
            },
        )
    )
    async with httpx.AsyncClient(transport=transport) as client:
        candidates = await discover_requests(Config(search={"provider": "brave"}), client)
    assert len(candidates) == 1
    assert candidates[0].source_url == "https://platform.ru/posts/123"
    assert candidates[0].status == "unverified_search_hit"


async def test_external_request_is_cited_without_importing_platform_contacts(tmp_path):
    class RequestCrawler:
        def __init__(self, *args):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def fetch(self, url):
            text = (
                "Ищем подрядчика для автоматизации продаж. admin@platform.ru"
                if "platform.ru" in url
                else "Упаковка оптом. Каталог. Заявки."
            )
            return Page(url, text, "", ["https://other.ru"])

    path = tmp_path / "requests.csv"
    path.write_text("website,request_url\nsupplier.ru,https://platform.ru/posts/123\n")
    store = Store(str(tmp_path / "db.sqlite"))
    try:
        reports = await run(
            Config(), [], store, RequestCrawler, request_sources=read_requests_csv(str(path))
        )
        report = reports[0]
        assert report.qualification.interest == "public_request_to_verify"
        request_id = report.qualification.request_evidence_ids[0]
        assert next(f for f in report.facts if f.id == request_id).source_url.endswith("posts/123")
        assert not report.contacts
        restored = store.reports()[0]
        assert restored.qualification == report.qualification
    finally:
        store.close()


def test_request_quote_is_literal():
    report = Research(domain="supplier.ru", website="https://supplier.ru")
    page = Page("https://platform.ru/request", "Ищем подрядчика для автоматизации продаж!", "", [])
    extract_request(page, report)
    assert report.facts[0].quote in page.text
