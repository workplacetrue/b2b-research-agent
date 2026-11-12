import hashlib
import json
import logging
import uuid
from urllib.parse import urldefrag, urljoin, urlsplit

import httpx

from .analysis import analyze, assess_external_call, assess_research, extract, extract_request
from .config import Config
from .crawler import Crawler
from .discovery import discover, normalize_site
from .models import Research
from .storage import Store

log = logging.getLogger(__name__)


def record_error(store: Store, report: Research, stage: str, error: Exception):
    # Never serialize exception bodies/URLs/headers: provider errors may expose secrets.
    message = f"{stage}: {type(error).__name__}"
    if type(error) is ValueError:
        message += ": " + str(error)[:180]
    elif isinstance(error, httpx.HTTPStatusError):
        message += f": HTTP {error.response.status_code}"
    report.errors.append(message)
    store.error(report.domain, stage, message)
    log.warning("%s %s", report.domain, message)


async def run(
    config: Config,
    seeds: list[str],
    store: Store,
    crawler_factory=Crawler,
    request_sources: dict[str, list[str]] | None = None,
):
    results = []
    run_id = uuid.uuid4().hex
    research_steps = 0
    requests_by_domain = {}
    seeds = list(request_sources or {}) + seeds
    for website, sources in (request_sources or {}).items():
        domain, _ = normalize_site(website)
        requests_by_domain.setdefault(domain, []).extend(sources)
    async with httpx.AsyncClient(timeout=config.timeout_seconds, follow_redirects=False) as client:
        try:
            cache_key = "discovery:" + hashlib.sha256(json.dumps(config.search.model_dump(), sort_keys=True).encode()).hexdigest()
            discovered = store.cached(cache_key)
            if discovered is None:
                reservation = assess_external_call(config, "search", paid=config.search.provider != "none" and bool(config.search.queries),
                    store=store, reservation_id=f"{run_id}:search", calls=len(config.search.queries), run_scope=run_id)
                try:
                    discovered = await discover(config, client)
                finally:
                    if reservation:
                        reservation[0].reconcile(reservation[1], billing_known=False)
                store.cache(cache_key, discovered)
            seeds = seeds + discovered
        except Exception as error:
            report = Research(domain="discovery", website="")
            record_error(store, report, "search", error)
            if not seeds:
                raise RuntimeError("Search failed and no CSV seeds supplied") from None
        sites = {}
        for seed in seeds:
            try:
                domain, website = normalize_site(seed)
                sites.setdefault(domain, website)
            except ValueError as error:
                # Don't log the supplied seed; it may include URL credentials.
                store.error("invalid-seed", "discovery", str(error))
        async with crawler_factory(config, client) as crawler:
            for domain, website in list(sites.items())[: config.max_companies]:
                report = Research(domain=domain, website=website)
                queue, seen = [website], set()
                while queue and len(seen) < config.max_pages:
                    url = queue.pop(0)
                    if url in seen:
                        continue
                    seen.add(url)
                    try:
                        if research_steps >= config.budget.max_research_steps:
                            raise ValueError("Research step budget exhausted; remaining work is on hold")
                        assess_external_call(config, "crawl", paid=False, store=store, account_id=domain)
                        research_steps += 1
                        page = await crawler.fetch(url)
                        report.pages.append(page.url)
                        extract(page, report, config)
                        for link in page.links:
                            target = urldefrag(urljoin(url, link))[0]
                            parsed = urlsplit(target)
                            # Exact origin; never crawl external contacts/social links or query traps.
                            if (parsed.scheme, parsed.netloc) != (
                                urlsplit(website).scheme,
                                urlsplit(website).netloc,
                            ):
                                continue
                            if parsed.query or parsed.username or parsed.password:
                                continue
                            if any(
                                k in target.lower()
                                for k in (
                                    "catalog",
                                    "katalog",
                                    "contact",
                                    "kontakt",
                                    "order",
                                    "delivery",
                                    "каталог",
                                    "контакт",
                                    "заказ",
                                    "about",
                                    "price",
                                    "crm",
                                    "tender",
                                    "procurement",
                                    "закуп",
                                    "тендер",
                                )
                            ):
                                if target not in seen and target not in queue:
                                    queue.append(target)
                    except Exception as error:
                        record_error(store, report, "crawl", error)
                # Only explicitly supplied sources; never follow their links, extract
                # platform contacts, or use platform text to score the company's fit.
                sources = list(dict.fromkeys(requests_by_domain.get(domain, [])))
                for source_url in sources[: config.max_request_pages]:
                    try:
                        if research_steps >= config.budget.max_research_steps:
                            raise ValueError("Research step budget exhausted; request is on hold")
                        assess_external_call(config, "crawl", paid=False, store=store, account_id=domain)
                        research_steps += 1
                        source = await crawler.fetch(source_url)
                        extract_request(source, report)
                    except Exception as error:
                        record_error(store, report, "request", error)
                if report.pages or any(f.kind == "buyer_request" for f in report.facts):
                    try:
                        await analyze(report, config, client, policy_store=store, run_scope=run_id)
                        from .workflow import current_catalog
                        catalog = current_catalog(config)
                        opportunity = assess_research(report, catalog)
                        report.assessment = opportunity.assessment.model_dump(mode="json")
                        store.save_snapshot("catalog", catalog.model_dump(mode="json"))
                        from .state import WorkflowStore
                        workflow = WorkflowStore(":memory:", db=store.db)
                        workflow.put_opportunity(opportunity.model_dump(mode="json"))
                    except Exception as error:
                        record_error(store, report, "analysis", error)
                    report.status = "partial" if report.errors else "ok"
                store.save(report)
                results.append(report)
    return results
