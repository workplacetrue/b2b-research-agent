"""Research concrete public ads and reply only to their explicit response address."""

import hashlib
import json
import re
from datetime import UTC, date, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup
from pydantic import Field

from .config import Config
from .crawler import Crawler, Page
from .discovery import discover_requests, normalize_site
from .mail_delivery import (
    DeliveryRejected,
    deliver,
    gmail_access_token,
    preflight,
)
from .models import RequestCandidate, StrictModel
from .outbox import Outbox

EMAIL = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
DEMAND = re.compile(
    r"ищ(?:ем|у|ет)\s+(?:подрядчика|исполнителя|интегратора|разработчика|специалиста)|"
    r"нуж(?:ен|на|но)\s+(?:подрядчик|исполнитель|интегратор|автоматизац|настроить|разработать)|"
    r"(?:требуется|необходимо)\s+(?:внедр|настроить|разработать|автоматиз|интегрир)",
    re.I,
)
WORKFLOWS = {
    "sales_quotes": (
        r"коммерческ\w*\s+предлож|подбор\w*.{0,30}позиц|подготов\w*.{0,20}\bКП\b",
        "автоматизацией подготовки коммерческих предложений",
    ),
    "sales_intake": (
        r"лид(?:ов|ами|ы)?\b|квалификац|(?:обработк|при[её]м).{0,30}(?:заяв|обращ)|"
        r"автоматизац\w*\s+продаж|CRM|СРМ|ИИ-менеджер",
        "автоматизацией обработки обращений и работы с лидами",
    ),
    "sales_followup": (
        r"повторн\w*.{0,30}(?:продаж|заказ|контакт)|напоминан\w*.{0,30}менеджер",
        "автоматизацией напоминаний о повторных контактах с клиентами",
    ),
    "documents": (
        r"документ|сч[её]т|накладн|договор|документооборот",
        "автоматизацией подготовки и обработки документов",
    ),
}


class ReplyAssessment(StrictModel):
    source_url: str
    title: str = ""
    published_on: date | None = None
    recipient: str | None = None
    contact_quote: str = ""
    workflow: str | None = None
    subject: str | None = None
    body: str | None = None
    message_key: str | None = None
    status: str = "needs_review"
    reasons: list[str] = Field(default_factory=list)
    provider_id: str | None = None


def canonical_ad_url(value: str) -> str:
    _, origin = normalize_site(value)
    parsed = urlsplit(value)
    normalized = urlsplit(origin)
    query = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}
    ]
    return urlunsplit(
        (
            normalized.scheme,
            normalized.netloc.removeprefix("www."),
            parsed.path or "/",
            urlencode(sorted(query)),
            "",
        )
    )


def _json_nodes(value):
    if isinstance(value, list):
        for item in value:
            yield from _json_nodes(item)
    elif isinstance(value, dict):
        yield value
        yield from _json_nodes(value.get("@graph", []))


def _as_date(value: str) -> date:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).date()


def _publication(soup, text: str) -> tuple[set[date], bool, list[date]]:
    dates, expired_dates, listing, vacancy = set(), [], False, False
    for tag in soup.select('script[type="application/ld+json"]'):
        try:
            nodes = list(_json_nodes(json.loads(tag.string or tag.get_text())))
        except (TypeError, ValueError):
            continue
        for node in nodes:
            types = node.get("@type", [])
            types = [types] if isinstance(types, str) else types
            listing |= "ItemList" in types
            vacancy |= "JobPosting" in types
            for key in ("datePublished", "datePosted"):
                if isinstance(node.get(key), str):
                    try:
                        dates.add(_as_date(node[key]))
                    except ValueError:
                        pass
            if isinstance(node.get("validThrough"), str):
                try:
                    expired_dates.append(_as_date(node["validThrough"]))
                except ValueError:
                    pass
    for tag in soup.select('meta[property="article:published_time"], time[datetime]'):
        try:
            dates.add(_as_date(tag.get("content") or tag.get("datetime")))
        except (TypeError, ValueError):
            pass
    months = (
        "января февраля марта апреля мая июня июля августа сентября октября ноября декабря".split()
    )
    match = re.search(
        r"(?:опубликован[ао]?|дата публикации)\s*:?\s*(\d{1,2})\s+(\w+)\s+(20\d{2})", text, re.I
    )
    if match and match[2].lower() in months:
        try:
            dates.add(date(int(match[3]), months.index(match[2].lower()) + 1, int(match[1])))
        except ValueError:
            pass
    return dates, (listing or vacancy), expired_dates


def inspect_ad(page: Page, config: Config, now: datetime | None = None) -> ReplyAssessment:
    result = ReplyAssessment(source_url=canonical_ad_url(page.url))
    soup = BeautifulSoup(page.html, "html.parser")
    articles = [a for a in soup.find_all("article") if not a.find_parent("article")]
    if len(articles) > 1:
        result.reasons = ["Страница содержит несколько объявлений"]
        return result
    root = articles[0] if articles else soup.find("main")
    if root is None or urlsplit(page.url).path in {"", "/"}:
        result.reasons = ["Нужна отдельная страница объявления с article или main"]
        return result
    for tag in list(
        root.select(
            'nav, footer, aside, script:not([type="application/ld+json"]), '
            'style, noscript, [hidden], [aria-hidden="true"], '
            ".related, .recommended, .recommendations"
        )
    ):
        tag.decompose()
    headings = root.find_all("h1")
    if len(headings) != 1:
        result.reasons = ["Не удалось выделить единственный заголовок задачи"]
        return result
    result.title = re.sub(r"[\x00-\x1f\x7f]+", " ", headings[0].get_text(" ", strip=True))[:180]
    text = root.get_text(" ", strip=True)
    dates, unsuitable_page, expiry = _publication(soup, text)
    if unsuitable_page or re.search(
        r"ваканси|зарплат|полный рабочий день|ТК РФ|резюме", text, re.I
    ):
        result.reasons = ["Лента или вакансия: нужен конкретный проект заказчика"]
        return result
    today = (now or datetime.now(UTC)).astimezone(ZoneInfo(config.outreach.timezone)).date()
    if len(dates) != 1:
        result.reasons = ["Дата публикации отсутствует или неоднозначна"]
        return result
    result.published_on = next(iter(dates))
    age = (today - result.published_on).days
    if age < 0 or age > config.outreach.max_ad_age_days or any(d < today for d in expiry):
        result.reasons = ["Объявление просрочено, устарело или опубликовано будущей датой"]
        return result
    if re.search(
        r"(?:заказ|проект|запрос)\s+(?:закрыт|заверш[её]н)|исполнитель\s+найден|"
        r"поиск\s+заверш[её]н|статус\s*:?\s*(?:закрыт|архив)|"
        r"не\s+ищ(?:ем|у|ет)\s+(?:подрядчика|исполнителя)",
        text,
        re.I,
    ):
        result.reasons = ["Есть признак закрытого запроса или отсутствия спроса"]
        return result
    if re.search(
        r"не\s+(?:пишите|присылайте|отправляйте).{0,40}(?:почт|email)|"
        r"(?:отклики|общение|связь).{0,30}только.{0,40}(?:сайт|платформ|бирж|форм|telegram|телеграм)",
        text,
        re.I,
    ):
        result.reasons = ["В объявлении указан другой обязательный способ отклика"]
        return result
    if not DEMAND.search(text):
        result.reasons = ["Не найден прямой запрос заказчика на выполнение задачи"]
        return result
    for workflow, (pattern, _) in WORKFLOWS.items():
        if workflow in config.outreach.workflows and re.search(pattern, text, re.I):
            result.workflow = workflow
            break
    if not result.workflow:
        result.reasons = ["Задача не соответствует настроенным услугам"]
        return result
    contacts = {}
    for match in EMAIL.finditer(text):
        before = text[max(0, match.start() - 100) : match.start()]
        if re.search(
            r"(?:отклик\w*.{0,70}|пишите.{0,70}|присылайте.{0,70}|"
            r"отправляйте.{0,70}|для связи.{0,45})$",
            before,
            re.I,
        ):
            if not re.search(r"реклам|поддержк|администрац|support", before, re.I):
                contacts[match[0].lower()] = before + match[0]
    if len(contacts) != 1:
        result.reasons = ["Нужен единственный явно указанный email для откликов в тексте задачи"]
        return result
    result.recipient, result.contact_quote = next(iter(contacts.items()))
    scenario = WORKFLOWS[result.workflow][1]
    result.subject = f"Отклик на задачу: {result.title}"
    result.body = (
        f"Здравствуйте!\n\nПишу по вашему объявлению «{result.title}» "
        f"на {urlsplit(result.source_url).hostname}:\n{result.source_url}\n\n"
        f"Могу помочь с {scenario}. Готов предложить вариант под ваш бюджет; "
        "точную стоимость и ближайший реалистичный срок смогу подтвердить после "
        "уточнения объёма задачи и необходимых доступов.\n\n"
        "Если задача актуальна, ответьте на это письмо — уточним детали и первый шаг.\n\n"
        f"{config.outreach.signature or config.sender}"
    )
    fingerprint = result.recipient + "\n" + re.sub(r"\s+", " ", result.title.lower()).strip()
    result.message_key = hashlib.sha256(fingerprint.encode()).hexdigest()
    result.status, result.reasons = "ready", []
    return result


async def auto_reply(
    config: Config,
    outbox: Outbox,
    candidates: list[RequestCandidate] | None = None,
    dry_run: bool = False,
    crawler_factory=Crawler,
    client: httpx.AsyncClient | None = None,
) -> list[ReplyAssessment]:
    if client is None:
        async with httpx.AsyncClient(
            timeout=config.timeout_seconds, follow_redirects=False
        ) as owned:
            return await auto_reply(config, outbox, candidates, dry_run, crawler_factory, owned)
    sending = config.outreach.enabled and not dry_run
    if sending:
        preflight(config.outreach)
    candidates = candidates if candidates is not None else await discover_requests(config, client)
    results, seen, attempts, token = [], set(), 0, None
    async with crawler_factory(config, client) as crawler:
        for candidate in candidates[: config.max_companies]:
            try:
                url = canonical_ad_url(candidate.source_url)
                if url in seen:
                    continue
                seen.add(url)
                page = await crawler.fetch(candidate.source_url)
                result = inspect_ad(page, config)
            except Exception as error:
                results.append(
                    ReplyAssessment(
                        source_url=candidate.source_url,
                        status="research_failed",
                        reasons=[type(error).__name__],
                    )
                )
                continue
            results.append(result)
            if result.status != "ready":
                continue
            if not sending:
                result.status = "dry_run"
                continue
            if attempts >= config.outreach.max_per_run:
                result.status = "run_limit"
                continue
            if config.outreach.provider == "gmail" and token is None:
                token = await gmail_access_token(config.outreach, client)
            reservation = outbox.reserve(result.model_dump(mode="json"), config.outreach)
            if reservation != "reserved":
                result.status = reservation
                continue
            attempts += 1
            try:
                result.provider_id = await deliver(
                    config.outreach,
                    client,
                    result.recipient,
                    result.subject,
                    result.body,
                    result.message_key,
                    gmail_token=token,
                )
                result.status = "sent"
                outbox.finish(result.message_key, "sent", result.provider_id)
            except Exception as error:
                result.status = "failed" if isinstance(error, DeliveryRejected) else "uncertain"
                result.reasons = [type(error).__name__]
                outbox.finish(result.message_key, result.status, error=type(error).__name__)
    return results
