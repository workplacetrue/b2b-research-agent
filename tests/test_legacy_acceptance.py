"""Legacy CLI acceptance across one offline store and a shared controlled clock."""
import csv
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from b2b_agent.config import Config
from b2b_agent.crawler import Page
from b2b_agent.models import RequestCandidate
from b2b_agent.outbox import Outbox
from b2b_agent.storage import Store


def test_all_legacy_cli_commands_offline_preserve_progress_when_research_budget_ends(tmp_path, monkeypatch):
    import httpx

    import b2b_agent.cli as cli
    import b2b_agent.models as models
    import b2b_agent.outbox as outbox
    import b2b_agent.outreach as outreach
    import b2b_agent.pipeline as pipeline
    import b2b_agent.state as state
    fixed = datetime(2026, 11, 12, 12, tzinfo=UTC)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)
    monkeypatch.setattr(models, "datetime", Clock)
    monkeypatch.setattr(outreach, "datetime", Clock)
    monkeypatch.setattr(state, "utcnow", lambda: fixed)
    monkeypatch.setattr(outbox, "utcnow", lambda: fixed)
    monkeypatch.chdir(tmp_path)
    async def no_network(*args, **kwargs):
        raise AssertionError("Legacy acceptance must never make a network request")
    monkeypatch.setattr(httpx.AsyncClient, "send", no_network)
    crawls = []
    class Crawler:
        def __init__(self, *args):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def fetch(self, url):
            crawls.append(url)
            if "platform.ru" in url:
                html = '<main><article><h1>Автоматизация обработки заявок</h1><time datetime="2026-11-12"></time><p>Ищем подрядчика для автоматизации обработки заявок.</p><p>Для откликов пишите buyer@customer.ru</p></article></main>'
                return Page(url, "", html, [])
            return Page(url, "Упаковка оптом. Каталог. Заявки.", "", [])
    async def no_company_search(*args):
        return []
    async def request_search(*args):
        return [RequestCandidate(source_url="https://platform.ru/request/1", query="buyer request")]
    monkeypatch.setattr(pipeline, "discover", no_company_search)
    monkeypatch.setattr(outreach, "discover_requests", request_search)
    async def run(config, seeds, store, **kwargs):
        return await pipeline.run(config, seeds, store, crawler_factory=Crawler, **kwargs)
    real_auto_reply = outreach.auto_reply
    async def auto_reply(config, box, candidates=None, **kwargs):
        return await real_auto_reply(config, box, candidates, crawler_factory=Crawler, **kwargs)
    monkeypatch.setattr(cli, "run", run)
    monkeypatch.setattr(cli, "auto_reply", auto_reply)
    def command(*args):
        monkeypatch.setattr(sys, "argv", ["b2b-agent", *args])
        return cli.main()

    assert command("init", "--config", "config.json") == 0
    assert Path("INPUT/services.json").exists()
    config = Config(search={"provider": "brave", "queries": [], "request_queries": ["buyer request"]},
        budget={"max_research_steps": 1, "tool_prices": {"search": {"per_call": .01, "currency": "USD", "source": "offline mock tariff"}}},
        max_pages=1, outreach={"enabled": False, "from_email": "sender@seller.ru", "signature": "Owner"})
    Path("config.json").write_text(config.model_dump_json())
    Path("sites.csv").write_text("website\nhttps://first.ru\nhttps://second.ru\n")
    common = ["--config", "config.json", "--db", "agent.sqlite", "--output", "export"]
    assert command("run", *common, "--csv", "sites.csv", "--dry-run") == 2
    store = Store("agent.sqlite")
    reports = {report.domain: report for report in store.reports()}
    assert reports["first.ru"].facts and reports["first.ru"].status == "ok"
    assert reports["second.ru"].status == "failed" and reports["second.ru"].errors
    assert reports["first.ru"].observed_at == fixed
    assert len(store.history("first.ru")) == 1
    reports["first.ru"].draft = "=CMD()"
    store.save(reports["first.ru"])
    store.close()
    assert command("export", "--db", "agent.sqlite", "--output", "export") == 2
    with Path("export/results.csv").open(encoding="utf-8-sig") as stream:
        rows = {row["domain"]: row for row in csv.DictReader(stream)}
    assert rows["first.ru"]["draft"] == "'=CMD()"
    assert command("find-requests", *common) == 0
    assert command("auto-reply", *common, "--requests-json", "export/request_candidates.json", "--dry-run") == 0
    reply = json.loads(Path("export/outreach.json").read_text())[0]
    assert reply["status"] == "dry_run"
    assert command("suppress-email", "--email", "buyer@customer.ru", "--db", "agent.sqlite") == 0
    box = Outbox("agent.sqlite")
    assert box.workflow.is_suppressed({"recipient": "buyer@customer.ru"})
    assert not box.workflow.list_actions()
    assert box.db.execute("SELECT COUNT(*) FROM outbound_messages").fetchone()[0] == 0
    assert box.db.execute("SELECT COUNT(*) FROM research_snapshots WHERE domain='first.ru'").fetchone()[0] == 2
    box.close()
    assert crawls == ["https://first.ru", "https://platform.ru/request/1"]
