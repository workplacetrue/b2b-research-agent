import csv
import ipaddress
import os
from urllib.parse import urlsplit

import httpx

from .config import Config


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
