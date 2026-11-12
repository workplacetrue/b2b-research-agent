
import httpx
import pytest

from b2b_agent.analysis import analyze, assess_research, extract
from b2b_agent.channels import advance_manual_task, channel_capabilities, prepare_manual_task
from b2b_agent.config import Config
from b2b_agent.crawler import Page
from b2b_agent.models import Research
from b2b_agent.storage import Store


def research(domain="supplier.ru", text="Каталог. Оптовые продажи. Отправьте заявку."):
    report = Research(domain=domain, website="https://" + domain)
    extract(Page(report.website, text, "", []), report, Config())
    return report


async def test_independent_fit_does_not_require_legacy_icp100():
    report = research()
    async with httpx.AsyncClient() as client:
        await analyze(report, Config(), client)
    assert report.score.icp == 40
    assert report.qualification.next_step == "check_relevance"
    assert report.draft
    assert report.assessment["contactability"]["value"] == "unknown"
    assert report.qualification.interest == "unconfirmed"
    assert all(f.id.startswith("ev:") for f in report.facts)
    assert report.evidence[0].research_version == report.version
    assert "вручную" not in report.draft and "теряете" not in report.draft


def test_evidence_ids_cannot_collide_across_company_or_research():
    first, second = research(), research("other.ru")
    assert set(f.id for f in first.facts).isdisjoint(f.id for f in second.facts)
    assert set(f.id for f in first.facts).isdisjoint(f.id for f in research().facts)


async def test_public_demand_outside_niche_stays_public_need():
    report = research("school.ru", "Онлайн-школа ищет подрядчика для автоматизации обработки заявок.")
    async with httpx.AsyncClient() as client:
        await analyze(report, Config(), client)
    result = assess_research(report)
    assert result.origin == "public_request" and result.need.status == "publicly_stated"
    assert result.assessment.account_fit.value == "unknown"
    assert result.assessment.contactability.value == "unknown"
    assert report.qualification.interest == "public_request_to_verify"


def test_legacy_current_payload_survives_multiple_snapshot_versions(tmp_path):
    import sqlite3
    path = tmp_path / "old.sqlite"
    old = research()
    old.facts[0].id = "f1"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE companies(domain TEXT PRIMARY KEY,payload TEXT NOT NULL)")
    db.execute("INSERT INTO companies VALUES(?,?)", (old.domain, old.model_dump_json()))
    db.commit()
    db.close()
    store = Store(str(path))
    newer = research()
    store.save(newer)
    store.save(newer)
    assert len(store.history(old.domain)) == 2
    assert any(r.facts[0].id == "f1" for r in store.history(old.domain))
    assert store.reports()[0] == newer
    assert store.save_snapshot("offer", {"price": 1}) != store.save_snapshot("offer", {"price": 2})
    store.close()


async def test_unknown_llm_cost_blocks_network_and_paid_budget_is_reserved(tmp_path, monkeypatch):
    monkeypatch.setenv("B2B_LLM_API_KEY", "offline-key")
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"hypotheses":[]}'}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ValueError, match="price is unknown"):
            await analyze(research(), Config(llm={"enabled": True}, memory={"input_tokens": 12000}), client)
        assert not calls
        store = Store(str(tmp_path / "budget.sqlite"))
        config = Config(llm={"enabled": True}, memory={"input_tokens": 12000}, budget={"max_llm_calls": 1,
            "tool_prices": {"llm": {"per_call": .01, "currency": "USD", "source": "offline fixture"}}})
        await analyze(research(), config, client, store)
        with pytest.raises(ValueError, match="Call budget exceeded"):
            await analyze(research(), config, client, store)
        assert len(calls) == 1
        assert store.db.execute("SELECT amount FROM action_cost_reservations").fetchone()[0] == "0.01"
        store.close()


def test_manual_copy_is_not_sent_and_mandatory_channel_cannot_be_bypassed():
    assert channel_capabilities("email").can_send
    assert not channel_capabilities("telegram").can_send
    with pytest.raises(ValueError, match="обходить"):
        prepare_manual_task({"channel": "linkedin"}, required_channel="platform")
    task = prepare_manual_task({"channel": "platform", "body": "A bounded response"})
    with pytest.raises(ValueError, match="approval"):
        advance_manual_task(task, "approved", actor="owner")
    task = advance_manual_task(task, "approved", actor="owner", approval_current=True)
    task = advance_manual_task(task, "copied", actor="owner", approval_current=True)
    assert task["status"] == "awaiting_manual_send" and task["delivery_status"] == "draft"
    task = advance_manual_task(task, "sent_confirmed", actor="owner", approval_current=True)
    assert task["status"] == "sent_confirmed_by_user"


async def test_paid_search_reserves_all_queries_and_reuses_cache(tmp_path, monkeypatch):
    import b2b_agent.pipeline as pipeline
    calls = []
    async def search(config, client):
        calls.append("search")
        return ["https://buyer.ru"]
    class Crawler:
        def __init__(self, *args):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def fetch(self, url):
            return Page(url, "Оптовые продажи. Каталог. Заявки.", "", [])
    monkeypatch.setattr(pipeline, "discover", search)
    store = Store(str(tmp_path / "search.sqlite"))
    config = Config(search={"provider": "brave", "queries": ["first", "second"]},
        budget={"tool_prices": {"search": {"per_call": .01, "currency": "USD", "source": "mock tariff"}}})
    await pipeline.run(config, [], store, Crawler)
    await pipeline.run(config, [], store, Crawler)
    assert calls == ["search"]
    row = store.db.execute("SELECT reserved_amount,calls FROM action_cost_reservations").fetchone()
    assert row[0] == "0.02" and row[1] == 2
    assert len(store.history("buyer.ru")) == 2
    store.close()


async def test_request_search_is_paid_guarded_and_shared_cache_survives_restart(tmp_path):
    from b2b_agent.models import RequestCandidate
    from b2b_agent.outbox import Outbox
    from b2b_agent.outreach import cached_request_search
    calls = []
    async def provider(config, client):
        calls.append("search")
        return [RequestCandidate(source_url="https://platform.ru/request/1", query="query")]
    path = str(tmp_path / "requests.sqlite")
    box = Outbox(path)
    unknown = Config(search={"provider": "brave", "request_queries": ["query"]})
    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError, match="price is unknown"):
            await cached_request_search(unknown, client, box.workflow, search_fn=provider)
        assert calls == []
        known = unknown.model_copy(update={"budget": Config(budget={"tool_prices": {
            "search": {"per_call": .01, "currency": "USD", "source": "offline tariff"}}}).budget})
        first = await cached_request_search(known, client, box.workflow, search_fn=provider)
        box.close()
        box = Outbox(path)
        second = await cached_request_search(known, client, box.workflow, search_fn=provider)
    assert first == second and calls == ["search"]
    assert box.db.execute("SELECT COUNT(*) FROM action_cost_reservations").fetchone()[0] == 1
    box.close()


async def test_auto_reply_tick_reuses_pending_source_and_bounds_crawl_steps(tmp_path):
    from datetime import UTC, datetime

    from b2b_agent.models import RequestCandidate
    from b2b_agent.outbox import Outbox
    from b2b_agent.outreach import auto_reply
    calls = []
    class Crawler:
        def __init__(self, *args):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def fetch(self, url):
            calls.append(url)
            html = f'<main><article><h1>Автоматизация обработки заявок</h1><time datetime="{datetime.now(UTC).date()}"></time><p>Ищем подрядчика для автоматизации обработки заявок.</p><p>Для откликов пишите buyer@customer.ru</p></article></main>'
            return Page(url, "", html, [])
    box = Outbox(str(tmp_path / "repeat.sqlite"))
    config = Config(outreach={"enabled": True, "from_email": "sender@seller.ru", "signature": "Owner"},
                    budget={"max_research_steps": 1})
    candidates = [RequestCandidate(source_url=f"https://platform.ru/request/{i}", query="test") for i in range(2)]
    first = await auto_reply(config, box, candidates, crawler_factory=Crawler)
    second = await auto_reply(config, box, candidates[:1], crawler_factory=Crawler)
    assert first[0].status == "pending_approval" and first[1].status == "deferred"
    assert second[0].action_id == first[0].action_id
    assert calls == [candidates[0].source_url]
    assert len(box.workflow.list_actions()) == 1
    box.close()


def test_find_requests_cli_uses_persistent_request_search_cache(tmp_path, monkeypatch):
    import sys

    import b2b_agent.outreach as outreach
    from b2b_agent.cli import main
    from b2b_agent.models import RequestCandidate
    calls = []
    async def provider(config, client):
        calls.append("search")
        return [RequestCandidate(source_url="https://platform.ru/request/1", query="query")]
    monkeypatch.setattr(outreach, "discover_requests", provider)
    config = Config(search={"provider": "brave", "request_queries": ["query"]}, budget={"tool_prices": {
        "search": {"per_call": .01, "currency": "USD", "source": "offline fixture"}}})
    config_file = tmp_path / "config.json"
    config_file.write_text(config.model_dump_json())
    monkeypatch.setattr(sys, "argv", ["b2b-agent", "find-requests", "--config", str(config_file),
        "--db", str(tmp_path / "agent.sqlite"), "--output", str(tmp_path / "export")])
    assert main() == 0
    assert main() == 0
    assert calls == ["search"]
    assert (tmp_path / "export" / "request_candidates.json").exists()
