"""Inspect public requests and queue individual approvals; never send from discovery."""

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


class OutboundAssessment(StrictModel):
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
    opportunity_id: str | None = None
    action_id: str | None = None
    task_fit: str = "unknown"
    channel: str = "email"
    dependency_version: str | None = None
    catalog_version: str | None = None
    unknowns: list[str] = Field(default_factory=list)


# Historical name describes an outgoing assessment, not an inbound message.
ReplyAssessment = OutboundAssessment


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
            ".related, .recommended, .recommendations, blockquote, q"
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
    required_channel = re.search(
        r"не\s+(?:пишите|присылайте|отправляйте).{0,40}(?:почт|email)|"
        r"(?:отклик\w*|общение|связь|ответ\w*|пишите).{0,30}(?:только|исключительно|обязательно).{0,40}(?:сайт|платформ|бирж|форм|telegram|телеграм|linkedin)",
        text,
        re.I,
    )
    if required_channel:
        requirement = required_channel.group()
        result.channel = ("telegram" if re.search(r"telegram|телеграм", requirement, re.I)
                          else "linkedin" if re.search(r"linkedin", requirement, re.I) else "platform")
        result.contact_quote = requirement
    if re.search(r"порнограф|азартн|казино|политическ|мошеннич", text, re.I):
        result.reasons = ["Исключённое владельцем направление"]
        return result
    if re.search(
        r"(?:архивн(?:ое|ый|ая)\s+(?:объявлен|запрос|проект)|"
        r"(?:объявлен\w*|запрос|проект)\s+(?:находится\s+)?в\s+архиве|"
        r"архив\s+(?:объявлений|запросов|проектов))",
        text, re.I,
    ) or str(root.get("data-status", "")).casefold() in {"archived", "closed", "expired"} or any(
        value.casefold() in {"archived", "closed", "expired"} for value in root.get("class", [])
    ):
        result.reasons = ["Объявление отмечено как архивное или закрытое"]
        return result
    if re.search(
        r"(?:не\s+для\s+бизнеса|для\s+(?:(?:моих|своих)\s+)?(?:личных|домашних)\s+(?:дел|нужд|задач)|"
        r"для\s+(?:личного|частного)\s+пользования|для\s+(?:моей|своей|собственной)\s+семьи)",
        text, re.I,
    ):
        result.reasons = ["Личный B2C-запрос: бизнес-заказчик и применимость услуги не подтверждены"]
        return result
    if re.search(r"(?:предлагаем|оказываем)\s+услуги|наши\s+клиенты|ищем\s+заказы", text, re.I):
        result.reasons = ["Публикация исполнителя, а не подтверждённая задача заказчика"]
        return result
    # A quoted demand is evidence about a quotation, not about this author's
    # current procurement. Keep literal text for audit, but require buyer intent
    # outside quoted passages before preparing an initial response.
    direct_text = re.sub(r'«[^»]*»|“[^”]*”|"[^"\n]*"', " ", text)
    if DEMAND.search(text) and not DEMAND.search(direct_text):
        result.reasons = ["Запрос приведён только как цитата; актуальная задача автора не подтверждена"]
        return result
    if not DEMAND.search(text):
        result.reasons = ["Не найден прямой запрос заказчика на выполнение задачи"]
        return result
    from .workflow import current_catalog

    try:
        catalog = current_catalog(config)
    except ValueError:
        result.reasons = ["Каталог INPUT не прошёл проверку; подготовка обращения приостановлена"]
        return result
    offers = [offer for offer in catalog.services if offer.enabled]
    for offer in offers:
        workflow = getattr(offer, "workflow", None) or offer.id
        pattern = WORKFLOWS.get(workflow, (None, None))[0]
        if workflow not in config.outreach.workflows and workflow in WORKFLOWS:
            continue
        supported = list(offer.supported_tasks)
        matches = bool(pattern and re.search(pattern, text, re.I))
        matches = matches or any(task.casefold() in text.casefold() for task in supported if task)
        if matches:
            result.workflow = offer.id
            selected = offer
            break
    if not result.workflow:
        result.reasons = ["Задача не соответствует настроенным услугам"]
        return result
    result.task_fit = "full"
    full_crm = re.search(r"(?:CRM|СРМ).{0,30}(?:полностью|под ключ)|(?:внедрить|внедрение)\s+(?:CRM|СРМ)", text, re.I)
    if full_crm and not any(
        re.search(r"(?:CRM|СРМ).{0,30}(?:полностью|под ключ)|(?:внедрить|внедрение)\s+(?:CRM|СРМ)", task, re.I)
        for task in selected.supported_tasks
    ):
        result.task_fit = "partial"
    if any(task.casefold() in text.casefold() for task in selected.unsupported_tasks if task):
        result.task_fit = "partial"
    result.catalog_version = catalog.version
    result.dependency_version = catalog.dependency_version(selected.id)
    result.unknowns = ["Компания/полномочия автора пока неизвестны", "Интерес к нашему предложению не подтверждён", "Объём, бюджет и сроки требуют уточнения"]
    if result.channel != "email":
        # The page is a manual destination reference, never a customer account
        # identity or an invented email/Telegram username. An operator must use
        # the request's actual allowed reply flow after individual approval.
        result.recipient = result.source_url
        result.unknowns.append("Адресат/форма выбранного канала требуют ручной проверки перед отправкой")
    else:
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
    from .messaging import price_answer

    scenario = selected.name
    declaration = next((claim.text for claim in selected.allowed_claims
                        if claim.kind == "capability_declaration"), "")
    result.subject = f"Отклик на задачу: {result.title}"
    boundary = ("Поддерживаем только указанный участок; полное внедрение CRM не обещаем. "
                if result.task_fit == "partial" else "")
    result.body = (
        f"Здравствуйте!\n\n{(catalog.sender_profile.name or catalog.sender_profile.brand) or config.sender}. "
        f"Пишу по вашему объявлению «{result.title}»:\n{result.source_url}\n\n"
        f"Предлагаем обсудить применимость услуги «{scenario}». {declaration} {boundary}"
        f"{price_answer(selected)}\n\n"
        "Актуально ли обсудить этот участок задачи и необходимый объём?\n\n"
        f"{catalog.sender_profile.signature or config.outreach.signature or config.sender}\n\n"
        "Если не хотите получать сообщения, ответьте «не писать»."
    )
    fingerprint = result.recipient + "\n" + re.sub(r"\s+", " ", result.title.lower()).strip()
    result.message_key = hashlib.sha256(fingerprint.encode()).hexdigest()
    result.status = "ready" if result.channel == "email" else "ready_manual"
    result.reasons = ([] if result.channel == "email" else
                      ["Указан обязательный канал отклика; подготовлена ручная задача без email-обхода"])
    return result


async def cached_request_search(config: Config, client: httpx.AsyncClient, store,
                                search_fn=None) -> list[RequestCandidate]:
    """Persist the known answer and reserve every paid query before the search."""
    import uuid

    from .analysis import assess_external_call
    from .storage import Store
    search_fn = search_fn or discover_requests
    store.db.execute("CREATE TABLE IF NOT EXISTS external_cache (cache_key TEXT PRIMARY KEY,payload TEXT NOT NULL,observed_at TEXT NOT NULL)")
    store.db.commit()
    key = "request-search:" + hashlib.sha256(json.dumps({
        "provider": config.search.provider, "queries": config.search.request_queries,
        "limit": config.search.limit}, sort_keys=True).encode()).hexdigest()
    cached = Store.cached(store, key)
    if cached is not None:
        return [RequestCandidate.model_validate(item) for item in cached]
    if not config.search.request_queries:
        return []
    operation = "request-search:" + uuid.uuid4().hex
    reservation = assess_external_call(config, "search", paid=config.search.provider != "none",
        store=store, reservation_id=operation, run_scope=operation,
        calls=len(config.search.request_queries))
    actual_cost = None
    try:
        candidates = await search_fn(config, client)
        if config.budget.tool_prices.get("search", {}).get("per_call") is not None:
            actual_cost = config.budget.tool_prices["search"]["per_call"] * len(config.search.request_queries)
        Store.cache(store, key, [candidate.model_dump(mode="json") for candidate in candidates])
        return candidates
    finally:
        if reservation:
            reservation[0].reconcile(reservation[1], actual_amount=actual_cost,
                                     billing_known=actual_cost is not None)


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
    # The legacy enabled flag enables transport availability only. Discovery
    # and draft preparation cannot create an approval or contact a mail service.
    candidates = candidates if candidates is not None else await cached_request_search(config, client, outbox.workflow)
    results, seen = [], set()
    from .analysis import assess_external_call
    from .workflow import queue_public_request
    research_steps = 0

    async with crawler_factory(config, client) as crawler:
        for candidate in candidates[: config.max_companies]:
            try:
                url = canonical_ad_url(candidate.source_url)
                if url in seen:
                    continue
                seen.add(url)
                existing = next((action for action in outbox.workflow.list_actions()
                    if action.get("source_url") == url and action.get("status") in {
                        "pending_approval", "approved", "deferred", "approved_for_manual_send",
                        "awaiting_manual_send", "sending", "uncertain", "sent", "simulated_sent"}), None)
                if existing is not None:
                    results.append(ReplyAssessment(source_url=url, title=candidate.title,
                        channel=existing.get("channel", "email"),
                        recipient=existing.get("recipient"), workflow=existing.get("service_id"),
                        subject=existing.get("subject"), body=existing.get("body"),
                        message_key=existing.get("message_key"), status=existing["status"],
                        action_id=existing["id"], opportunity_id=existing.get("opportunity_id"),
                        reasons=["Сохранённое действие по этому источнику уже ожидает следующего события; повторное исследование не требуется"]))
                    continue
                if research_steps >= config.budget.max_research_steps:
                    results.append(ReplyAssessment(source_url=url, status="deferred",
                        reasons=["Достигнут лимит исследовательских шагов; прогресс сохранён"]))
                    continue
                assess_external_call(config, "crawl", paid=False, store=outbox.workflow,
                                     account_id="request:" + hashlib.sha256(url.encode()).hexdigest())
                research_steps += 1
                page = await crawler.fetch(candidate.source_url)
                result = inspect_ad(page, config)
            except Exception as error:
                results.append(ReplyAssessment(source_url=candidate.source_url,
                    status="research_failed", reasons=[type(error).__name__]))
                continue
            results.append(result)
            if result.status not in {"ready", "ready_manual"}:
                continue
            if dry_run:
                result.status = "dry_run"
                continue
            if not config.outreach.enabled and result.channel == "email":
                result.status = "draft"
                continue
            action = queue_public_request(outbox, result, config)
            result.action_id = action["id"]
            result.opportunity_id = action.get("opportunity_id")
            result.status = action.get("status", "pending_approval")
    return results
