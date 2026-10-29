"""Conservative fetch policy; Crawl4AI parses only pre-fetched HTML (no site JS)."""

import asyncio
import ipaddress
import os
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup
from protego import Protego

from .config import Config
from .discovery import normalize_site


@dataclass
class Page:
    url: str
    text: str
    html: str
    links: list[str]


async def public_url(url: str, proxy_dns: bool = False) -> None:
    normalize_site(url)
    host = urlsplit(url).hostname
    if proxy_dns:
        # Explicit opt-in for managed egress proxies that enforce public destinations.
        if not os.environ.get("HTTPS_PROXY"):
            raise ValueError("Proxy DNS mode requires a trusted HTTPS_PROXY")
        return
    addresses = await asyncio.to_thread(socket.getaddrinfo, host, None)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError("Non-public DNS destination blocked")


class Crawler:
    def __init__(self, config: Config, client: httpx.AsyncClient):
        self.config, self.client = config, client
        self.robots: dict[str, Protego] = {}
        self.last: dict[str, float] = {}
        self.engine = None

    async def __aenter__(self):
        from crawl4ai import AsyncWebCrawler
        from crawl4ai.async_crawler_strategy import AsyncHTTPCrawlerStrategy

        # HTTP raw strategy performs extraction without Chromium or site subrequests.
        self.engine = AsyncWebCrawler(crawler_strategy=AsyncHTTPCrawlerStrategy())
        await self.engine.__aenter__()
        return self

    async def __aexit__(self, *args):
        await self.engine.__aexit__(*args)

    async def _request(self, url: str, delay: float) -> httpx.Response:
        await public_url(url, proxy_dns=self.config.dns_mode == "proxy")
        origin = f"{urlsplit(url).scheme}://{urlsplit(url).netloc}"
        now = asyncio.get_running_loop().time()
        await asyncio.sleep(max(0, delay - (now - self.last.get(origin, 0))))
        self.last[origin] = asyncio.get_running_loop().time()
        async with self.client.stream(
            "GET", url, follow_redirects=False, headers={"User-Agent": self.config.user_agent}
        ) as response:
            if response.is_redirect:
                raise ValueError("Redirect skipped; supply the canonical website URL")
            if int(response.headers.get("content-length", 0)) > 2_000_000:
                raise ValueError("Response exceeds 2MB")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > 2_000_000:
                    raise ValueError("Response exceeds 2MB")
            return httpx.Response(
                response.status_code,
                headers={
                    k: v
                    for k, v in response.headers.items()
                    if k not in {"content-encoding", "content-length"}
                },
                content=bytes(body),
                request=response.request,
            )

    async def fetch(self, url: str) -> Page:
        u = urlsplit(url)
        origin = f"{u.scheme}://{u.netloc}"
        if origin not in self.robots:
            response = await self._request(origin + "/robots.txt", self.config.delay_seconds)
            if response.status_code not in (200, 404):
                raise ValueError(f"robots.txt unavailable: HTTP {response.status_code}")
            robot = Protego.parse(response.text if response.status_code == 200 else "")
            self.robots[origin] = robot
        robot = self.robots[origin]
        if not robot.can_fetch(url, self.config.user_agent):
            raise ValueError("robots.txt disallows page")
        delay = max(self.config.delay_seconds, robot.crawl_delay(self.config.user_agent) or 0)
        rate = robot.request_rate(self.config.user_agent)
        if rate and rate.requests:
            delay = max(delay, rate.seconds / rate.requests)
        response = await self._request(url, delay)
        response.raise_for_status()
        if "text/html" not in response.headers.get("content-type", ""):
            raise ValueError("Only HTML pages supported")
        from crawl4ai import CacheMode, CrawlerRunConfig

        result = await self.engine.arun(
            url="raw:" + response.text,
            config=CrawlerRunConfig(cache_mode=CacheMode.BYPASS),
        )
        if not result.success:
            raise ValueError("Crawl4AI extraction failed")
        soup = BeautifulSoup(response.text, "html.parser")
        links = [a.get("href", "") for a in soup.find_all("a", href=True)]
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        # Visible quotes are grounded in the fetched HTML, markdown is not an LLM fact source.
        # Use Crawl4AI's cleaned output for research, with quotes remaining literal text.
        cleaned = BeautifulSoup(result.cleaned_html or "", "html.parser")
        text = cleaned.get_text(" ", strip=True) or soup.get_text(" ", strip=True)
        return Page(url, text, response.text, links)
