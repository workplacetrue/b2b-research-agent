import csv
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from b2b_agent.analysis import analyze, extract
from b2b_agent.config import Config
from b2b_agent.crawler import Crawler, Page, public_url
from b2b_agent.discovery import discover, normalize_site, read_csv
from b2b_agent.models import Research
from b2b_agent.pipeline import run
from b2b_agent.storage import Store, export


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://127.0.0.1",
        "https://user:secret@host.ru",
        "http://host.local",
        "http://host.ru:8080",
    ],
)
def test_invalid_sites(url):
    with pytest.raises(ValueError):
        normalize_site(url)


def test_csv_and_normalization(tmp_path):
    p = tmp_path / "sites.csv"
    p.write_text("website\nhttps://www.ПРИМЕР.рф/path\n", encoding="utf-8-sig")
    domain, site = normalize_site(read_csv(str(p))[0])
    assert domain == "xn--e1afmkfd.xn--p1ai"
    assert site == "https://www.xn--e1afmkfd.xn--p1ai"


def page(url="https://supplier.ru"):
    return Page(
        url,
        "Упаковка оптом. Каталог. Отправить заявку на заказ. "
        "Контакты: sales@supplier.ru +7 (495) 123-45-67",
        "",
        ["/catalog", "https://external.ru/contacts", "/catalog?sort=1"],
    )


async def test_rules_evidence_and_no_invented_contacts():
    report = Research(domain="supplier.ru", website="https://supplier.ru")
    extract(page(), report, Config())
    extract(page(), report, Config())
    async with httpx.AsyncClient() as client:
        await analyze(report, Config(), client)
    assert len(report.contacts) == 2
    assert {c.value for c in report.contacts} == {"sales@supplier.ru", "+74951234567"}
    assert report.score.icp == 100
    assert report.score.automation == 100
    assert report.hypotheses
    assert all(set(h.evidence_ids) <= {f.id for f in report.facts} for h in report.hypotheses)
    assert all(f.quote in page().text for f in report.facts)
    assert "Не знаем" in report.draft


async def test_no_evidence_no_hypothesis_or_draft():
    report = Research(domain="empty.ru", website="https://empty.ru")
    async with httpx.AsyncClient() as client:
        await analyze(report, Config(), client)
    assert report.score.icp == 0
    assert not report.hypotheses and report.draft is None


async def test_llm_unknown_citation_rejected(monkeypatch):
    monkeypatch.setenv("B2B_LLM_API_KEY", "test-not-a-secret")
    content = {
        "hypotheses": [
            {"statement": "Возможно", "evidence_ids": ["invented"], "validation_question": "Как?"}
        ]
    }
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(content)}}]}
        )
    )
    report = Research(domain="supplier.ru", website="https://supplier.ru")
    config = Config(llm={"enabled": True})
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ValueError, match="nonexistent"):
            await analyze(report, config, client)


async def test_search_adapter(monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "test-key")

    def handler(request):
        assert request.headers["X-Subscription-Token"] == "test-key"
        assert request.url.params["q"] == "упаковка оптом"
        return httpx.Response(200, json={"web": {"results": [{"url": "https://supplier.ru"}]}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await discover(
            Config(search={"provider": "brave", "queries": ["упаковка оптом"]}), client
        ) == ["https://supplier.ru"]


class FakeCrawler:
    calls = []

    def __init__(self, *args):
        self.calls = []
        FakeCrawler.calls = self.calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def fetch(self, url):
        self.calls.append(url)
        if url.endswith("/catalog"):
            raise ValueError("robots.txt disallows page")
        return page(url)


async def test_pipeline_dedup_errors_storage_export(tmp_path):
    store = Store(str(tmp_path / "db.sqlite"))
    try:
        for _ in range(2):
            reports = await run(
                Config(), ["supplier.ru", "https://www.supplier.ru/path"], store, FakeCrawler
            )
        assert len(store.reports()) == 1
        report = reports[0]
        assert report.status == "partial"
        assert report.draft
        assert FakeCrawler.calls == ["https://supplier.ru", "https://supplier.ru/catalog"]
        assert store.db.execute("SELECT COUNT(*) FROM errors").fetchone()[0] == 2
        export(reports, str(tmp_path / "exports"))
        data = json.loads((tmp_path / "exports/results.json").read_text())
        assert data[0]["facts"][0]["source_url"] == "https://supplier.ru"
        with (tmp_path / "exports/results.csv").open(encoding="utf-8-sig") as f:
            assert len(list(csv.DictReader(f))) == 1
    finally:
        store.close()


async def test_robots_denied_and_unavailable(monkeypatch):
    monkeypatch.setattr("b2b_agent.crawler.public_url", AsyncMock())
    for status, body in [(200, "User-agent: *\nDisallow: /"), (503, "unavailable")]:
        transport = httpx.MockTransport(
            lambda req, status=status, body=body: httpx.Response(status, text=body)
        )
        async with httpx.AsyncClient(transport=transport) as client:
            crawler = Crawler(Config(delay_seconds=1), client)
            with pytest.raises(ValueError, match="robots.txt"):
                await crawler.fetch("https://supplier.ru")


async def test_private_dns_blocked(monkeypatch):
    monkeypatch.setattr("socket.getaddrinfo", lambda *args: [(2, 1, 6, "", ("10.0.0.1", 80))])
    with pytest.raises(ValueError, match="Non-public"):
        await public_url("https://supplier.ru")


async def test_redirect_not_followed(monkeypatch):
    monkeypatch.setattr("b2b_agent.crawler.public_url", AsyncMock())
    transport = httpx.MockTransport(
        lambda req: httpx.Response(302, headers={"location": "http://169.254.169.254"})
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ValueError, match="Redirect skipped"):
            await Crawler(Config(), client)._request("https://supplier.ru", 0)


def test_export_formula_safe(tmp_path):
    report = Research(domain="supplier.ru", website="https://supplier.ru", draft="=CMD()")
    export([report], str(tmp_path))
    with (tmp_path / "results.csv").open(encoding="utf-8-sig") as f:
        assert next(csv.DictReader(f))["draft"] == "'=CMD()"


async def test_real_crawl4ai_extraction_with_mock_http(monkeypatch):
    monkeypatch.setattr("b2b_agent.crawler.public_url", AsyncMock())
    html = """<html><head><title>Поставщик</title></head><body><main>
    <h1>Упаковка оптом для бизнеса</h1><p>Каталог упаковки и расходных материалов.
    Оставьте заявку на заказ через менеджера. Контакты для заказа:
    sales@supplier.ru +7 (495) 123-45-67</p><a href="/contacts">Контакты</a>
    </main></body></html>"""

    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        return httpx.Response(200, text=html, headers={"content-type": "text/html; charset=utf-8"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        async with Crawler(Config(delay_seconds=1), client) as crawler:
            result = await crawler.fetch("https://supplier.ru")
    report = Research(domain="supplier.ru", website=result.url)
    extract(result, report, Config())
    assert "sales@supplier.ru" in {c.value for c in report.contacts}
    assert {f.kind for f in report.facts} >= {"niche", "b2b", "catalog", "order"}
    assert result.links == ["/contacts"]


async def test_compressed_response_not_decoded_twice(monkeypatch):
    import gzip

    monkeypatch.setattr("b2b_agent.crawler.public_url", AsyncMock())
    transport = httpx.MockTransport(
        lambda req: httpx.Response(
            200,
            content=gzip.compress(b"<html>catalog</html>"),
            headers={"content-encoding": "gzip", "content-type": "text/html"},
        )
    )
    async with httpx.AsyncClient(transport=transport) as client:
        response = await Crawler(Config(), client)._request("https://supplier.ru", 0)
    assert response.text == "<html>catalog</html>"


async def test_robots_wildcard(monkeypatch):
    monkeypatch.setattr("b2b_agent.crawler.public_url", AsyncMock())
    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, text="User-agent: *\nDisallow: /private*")
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ValueError, match="disallows"):
            await Crawler(Config(), client).fetch("https://supplier.ru/private-page")


async def test_valid_llm_response(monkeypatch):
    monkeypatch.setenv("B2B_LLM_API_KEY", "test-key")
    report = Research(domain="supplier.ru", website="https://supplier.ru")
    extract(page(), report, Config())
    content = {
        "hypotheses": [
            {
                "statement": "Возможно, заявки обрабатываются вручную",
                "evidence_ids": [report.facts[0].id],
                "validation_question": "Какие этапы занимают больше времени?",
            }
        ]
    }
    transport = httpx.MockTransport(
        lambda req: httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(content)}}]}
        )
    )
    async with httpx.AsyncClient(transport=transport) as client:
        await analyze(report, Config(llm={"enabled": True}), client)
    assert report.analysis_mode == "llm"
    assert "Какие этапы" in report.draft
    assert report.contacts[0].value == "sales@supplier.ru"


async def test_llm_missing_key_explicit_failure(monkeypatch):
    monkeypatch.delenv("B2B_LLM_API_KEY", raising=False)
    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError, match="Missing LLM credential"):
            await analyze(
                Research(domain="supplier.ru", website="https://supplier.ru"),
                Config(llm={"enabled": True}),
                client,
            )
