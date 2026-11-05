import csv
import ipaddress
import os
from urllib.parse import urldefrag, urlsplit

import httpx

from .config import Config
from .models import RequestCandidate


def normalize_site(value: str) -> tuple[str, str]:
    value = value.strip()
    u = urlsplit(value if "://" in value else "https://" + value)
    if u.scheme not in {"http", "https"} or not u.hostname or u.username or u.password:
        raise ValueError("Invalid public website")
    host = u.hostname.lower().encode("idna").decode()
    if u.port not in (None, 80, 443) or "." not in host:
        raise ValueError("Only public HTTP(S) websites on standard ports are supported")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("IP address seeds are not supported")
    if host.endswith((".localhost", ".local", ".internal")):
        raise ValueError("Private hostnames are not supported")
    domain = host.removeprefix("www.")
    return domain, f"{u.scheme}://{host}"


def read_csv(path: str) -> list[str]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or "website" not in reader.fieldnames:
            raise ValueError("CSV must contain a website column")
        return [row["website"].strip() for row in reader if row.get("website", "").strip()]


async def discover(config: Config, client: httpx.AsyncClient) -> list[str]:
    if config.search.provider == "none":
        return []
    key = os.environ.get(config.search.api_key_env)
    if not key:
        raise ValueError(f"Missing search credential: {config.search.api_key_env}")
    sites = []
    for query in config.search.queries:
        response = await client.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": config.search.limit},
            headers={"X-Subscription-Token": key},
        )
        response.raise_for_status()
        sites.extend(item["url"] for item in response.json().get("web", {}).get("results", []))
    return sites


async def discover_requests(config: Config, client: httpx.AsyncClient) -> list[RequestCandidate]:
    """Search buyer requests separately; a platform URL is never a company website."""
    if config.search.provider != "brave":
        raise ValueError("Request search requires search.provider=brave")
    key = os.environ.get(config.search.api_key_env)
    if not key:
        raise ValueError(f"Missing search credential: {config.search.api_key_env}")
    candidates = {}
    for query in config.search.request_queries:
        response = await client.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": config.search.limit},
            headers={"X-Subscription-Token": key},
        )
        response.raise_for_status()
        for item in response.json().get("web", {}).get("results", []):
            url = item.get("url", "")
            try:
                normalize_site(url)
            except ValueError:
                continue
            url = urldefrag(url)[0]
            candidates.setdefault(
                url,
                RequestCandidate(
                    source_url=url,
                    query=query,
                    title=item.get("title", ""),
                    snippet=item.get("description", ""),
                ),
            )
    return list(candidates.values())


def read_requests_csv(path: str) -> dict[str, list[str]]:
    """Operator associates a public request with a company; verify identity separately."""
    requests = {}
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or not {"website", "request_url"} <= set(reader.fieldnames):
            raise ValueError("Request CSV must contain website and request_url columns")
        for row in reader:
            website, url = row.get("website", "").strip(), row.get("request_url", "").strip()
            if not website and not url:
                continue
            normalize_site(website)
            normalize_site(url)
            sources = requests.setdefault(website, [])
            url = urldefrag(url)[0]
            if url not in sources:
                sources.append(url)
    return requests
