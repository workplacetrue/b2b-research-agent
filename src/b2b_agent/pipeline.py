import logging
from urllib.parse import urldefrag, urljoin, urlsplit

import httpx

from .analysis import analyze, extract
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


async def run(config: Config, seeds: list[str], store: Store, crawler_factory=Crawler):
    results = []
    async with httpx.AsyncClient(timeout=config.timeout_seconds, follow_redirects=False) as client:
        try:
            seeds = seeds + await discover(config, client)
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
                                )
                            ):
                                if target not in seen and target not in queue:
                                    queue.append(target)
                    except Exception as error:
                        record_error(store, report, "crawl", error)
                if report.pages:
                    try:
                        await analyze(report, config, client)
                    except Exception as error:
                        record_error(store, report, "analysis", error)
                    report.status = "partial" if report.errors else "ok"
                store.save(report)
                results.append(report)
    return results
