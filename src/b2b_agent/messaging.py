"""Truthful bounded templates. Preparation never invokes a delivery adapter."""
from __future__ import annotations

import hashlib
import re

from .engagement import as_dict, recommend_next_action
from .models import DraftClaim, MessageDraft


class DraftValidationError(ValueError):
    pass


def price_answer(offer) -> str:
    price = as_dict(as_dict(offer).get("price"))
    dependencies = ", ".join(price.get("dependencies") or [])
    if price.get("minimum") is not None:
        lo = f"{price['minimum']:g}" if isinstance(price['minimum'], (float, int)) else str(price['minimum'])
        if price.get("maximum") is not None:
            hi = f"{price['maximum']:g}" if isinstance(price['maximum'], (float, int)) else str(price['maximum'])
            text = f"Утверждённый диапазон стоимости: {lo}–{hi} {price.get('currency', '')}."
        else:
            text = f"Известная минимальная стоимость: {lo} {price.get('currency', '')}."
    elif price.get("maximum") is not None:
        ceiling = f"{price['maximum']:g}" if isinstance(price['maximum'], (float, int)) else str(price['maximum'])
        text = f"Известная верхняя граница стоимости: {ceiling} {price.get('currency', '')}; нижняя граница не указана."
    else:
        text = "Утверждённая стоимость пока неизвестна; её можно определить после уточнения объёма."
    if dependencies:
        text += f" Стоимость зависит от: {dependencies}."
    return text


def _safe_line(value: str) -> str:
    return " ".join(str(value).split())


def stamp_draft_content(draft, source: str = "bounded_template", actor: str | None = None):
    """Mark an exact deterministic render, or an explicit individual owner review.

    This is an internal preparation operation, never a tool exposed to an LLM.
    A content stamp is independent of, and does not grant, send approval.
    """
    if source not in {"bounded_template", "owner_review"}:
        raise ValueError("Only deterministic templates or explicit owner reviews can validate content")
    if source == "owner_review" and (not actor or actor.strip().lower() in {"agent", "llm", "model", "assistant", "system"}):
        raise ValueError("Owner content review requires an individual operator identity")
    data = as_dict(draft)
    stamp = {
        "source": source, "template_version": "bounded-v2.1",
        "subject_hash": hashlib.sha256(str(data.get("subject", "")).encode()).hexdigest(),
        "body_hash": hashlib.sha256(str(data.get("body", "")).encode()).hexdigest(),
    }
    if actor is not None:
        stamp["actor"] = actor.strip()
    if isinstance(draft, dict):
        draft["content_validation"] = stamp
    else:
        draft.content_validation = stamp
    return draft


def validate_content_provenance(draft, required: bool = True) -> list[str]:
    data = as_dict(draft)
    stamp = data.get("content_validation")
    if not stamp:
        return ["Текст требует проверенного шаблона или явной проверки владельцем"] if required else []
    if not isinstance(stamp, dict):
        return ["Невалидное подтверждение проверки содержания"]
    errors = []
    if stamp.get("source") not in {"bounded_template", "owner_review"}:
        errors.append("Источник проверки содержания не подтверждён")
    if stamp.get("template_version") != "bounded-v2.1":
        errors.append("Версия проверки содержания устарела")
    if stamp.get("source") == "owner_review" and (not str(stamp.get("actor", "")).strip() or str(stamp.get("actor")).lower() in {"agent", "llm", "model", "assistant", "system"}):
        errors.append("Проверка владельцем не имеет действительного actor")
    for field in ("subject", "body"):
        expected = hashlib.sha256(str(data.get(field, "")).encode()).hexdigest()
        if stamp.get(field + "_hash") != expected:
            errors.append("Содержание изменилось после проверки: " + field)
    return errors


def validate_draft(draft, evidence=(), offer=None, proof_assets=(), *, require_provenance=False) -> list[str]:
    """Validate references and actual claim text, not merely valid JSON/IDs."""
    data, o = as_dict(draft), as_dict(offer)
    facts = {as_dict(f)["id"]: as_dict(f) for f in evidence}
    assets = {as_dict(a)["id"]: as_dict(a) for a in proof_assets}
    allowed = {as_dict(claim).get("text", "") for claim in o.get("allowed_claims", [])}
    errors = validate_content_provenance(data, required=require_provenance or data.get("source") == "llm")
    if data.get("kind", "initial") in {"initial", "cold"} and data.get("subject", "").lower().startswith("re:"):
        errors.append("Первое письмо не может иметь ложный Re:")
    for claim in data.get("claims", []):
        claim = as_dict(claim)
        if claim.get("offer_id"):
            if claim["offer_id"] != o.get("id") or claim.get("text") not in allowed:
                errors.append("Утверждение отсутствует в разрешённых данных услуги")
        elif claim.get("proof_asset_id"):
            asset = assets.get(claim["proof_asset_id"], {})
            if not asset or not asset.get("publication_allowed") or asset.get("availability") != "available":
                errors.append("Материал недоступен или не разрешён к раскрытию")
            elif claim.get("text") != asset.get("allowed_summary"):
                errors.append("Текст выходит за разрешённое описание материала")
        elif claim.get("evidence_ids"):
            for ref in claim["evidence_ids"]:
                fact = facts.get(ref)
                if not fact:
                    errors.append(f"Неизвестная ссылка evidence: {ref}")
                elif fact.get("freshness") == "stale" or fact.get("contradictions"):
                    errors.append(f"Устаревшее/противоречивое evidence: {ref}")
                elif claim.get("text") != fact.get("quote"):
                    errors.append("Ссылка на цитату не подтверждает произвольный вывод")
                elif fact.get("research_version") and not ref.startswith(fact["research_version"] + ":"):
                    # Global extraction IDs include the research origin in their
                    # cryptographic digest. They must still resolve in this exact
                    # supplied research snapshot; format alone never resolves one.
                    if not re.fullmatch(r"ev:[0-9a-f]{64}", ref):
                        errors.append("Evidence ID не имеет namespace версии исследования")
        else:
            errors.append("Утверждение не имеет проверяемого источника")
    return errors


validate_message_draft = validate_draft


def build_draft(plan, approved_facts, offer, conversation=None, sender=None) -> MessageDraft:
    """Build one applicability question from owner declarations, never the first LLM hypothesis."""
    p, o, c, s = map(as_dict, (plan, offer, conversation, sender))
    service_id = o.get("id", p.get("service_id", ""))
    task = _safe_line(o.get("name", "проверка применимости автоматизации"))
    identity = _safe_line(s.get("name") or s.get("brand") or "")
    signature = s.get("signature", "")
    lines = ["Здравствуйте!" + (f" Я представляю {identity}." if identity else "")]
    if p.get("origin") == "public_request":
        lines.append("Обращаемся по вашему опубликованному запросу.")
    else:
        lines.append("Предлагаем проверить применимость одного сценария автоматизации.")
    lines.append(f"Наше направление: {task}.")
    if p.get("task_fit") in {"partial", "unknown"}:
        lines.append("Соответствие всей задаче пока не подтверждено; границы нужно уточнить.")
    allowed = [as_dict(claim) for claim in o.get("allowed_claims", [])]
    claims = []
    if allowed:
        declaration = allowed[0]
        lines.append(declaration["text"])
        claims.append(DraftClaim(text=declaration["text"], offer_id=service_id,
                                 kind=declaration.get("kind", "capability_declaration")))
    lines.append("Какой участок процесса для вас сейчас актуальнее всего?")
    if signature:
        lines.append(signature)
    lines.append('Если дальнейшие обращения не нужны, ответьте «не писать».')
    body = "\n\n".join(lines)
    identifier = "draft_" + hashlib.sha256(f"{p.get('opportunity_id')}:{service_id}:{body}".encode()).hexdigest()[:24]
    draft = MessageDraft(
        id=identifier, context_version=c.get("context_version", 1), stats_version=c.get("stats_version", 0),
        opportunity_id=p.get("opportunity_id", c.get("opportunity_id", "")),
        conversation_id=c.get("id", ""), recipient=p.get("recipient", c.get("recipient", "")),
        channel=p.get("channel", c.get("channel", "email")), sender_account=s.get("from_email", ""),
        kind="initial", goal=p.get("goal", p.get("customer_benefit", "Проверить применимость задачи")),
        subject=f"Проверка применимости: {task}"[:150], body=body, claims=claims,
    )
    stamp_draft_content(draft)
    draft.validation_results = validate_draft(draft, approved_facts, offer, require_provenance=True)
    return draft


def case_answer(catalog, offer, request_text: str = "", context=None) -> tuple[str | None, list[str], list[str]]:
    """Return only a publishable, correctly labelled material, or an owner handoff."""
    o = as_dict(offer)
    service_id = o.get("id")
    assets = [asset for asset in catalog.proof_assets
              if asset.availability == "available" and asset.publication_allowed
              and asset.allowed_summary and (service_id in asset.service_ids or asset.id in o.get("proof_asset_ids", []))]
    # Exact case criteria cannot silently be replaced by a demo or a pilot.
    mandatory = any(word in request_text.lower() for word in ("обязател", "только с кейс", "без кейса", "required"))
    clients = [a for a in assets if a.type == "client_case"]
    if mandatory:
        criteria = as_dict(context)
        process = criteria.get("required_case_process") or criteria.get("confirmed_task") or criteria.get("task")
        industry = criteria.get("required_case_industry") or criteria.get("industry")
        scale = criteria.get("required_case_scale") or criteria.get("scale")
        comparable = []
        for asset in clients:
            same_process = bool(process and str(process).strip().casefold() in {p.strip().casefold() for p in asset.processes})
            same_industry = bool(industry and str(industry).strip().casefold() == asset.industry.strip().casefold())
            same_scale = bool(scale and str(scale).strip().casefold() == asset.scale.strip().casefold())
            if same_process and same_industry and same_scale:
                comparable.append(asset)
        if not comparable:
            return None, [], ["Клиент требует сопоставимый кейс; процесс/отрасль/масштаб не подтверждены как сопоставимые"]
        assets = comparable
    if assets:
        priority = {"client_case": 0, "team_experience": 1, "demo": 2, "prototype": 3, "pilot": 4, "reference": 5}
        asset = sorted(assets, key=lambda a: priority[a.type])[0]
        labels = {"client_case": "Разрешённый клиентский пример", "team_experience": "Личный опыт участника команды",
                  "demo": "Тестовая демонстрация", "prototype": "Тестовый прототип", "pilot": "Ограниченный пилот",
                  "reference": "Разрешённая рекомендация"}
        answer = f"{labels[asset.type]}: {asset.allowed_summary}"
        if asset.type in {"demo", "prototype"}:
            answer += " Это показывает механизм, но не подтверждает производственные результаты на вашей нагрузке."
        if asset.type == "team_experience":
            answer += f" Роль участника: {asset.company_role}. Это не внедрение нашей компании."
        if asset.type == "client_case" and not mandatory:
            answer += " Сопоставимость процесса, отрасли и масштаба с вашей задачей требует отдельной проверки."
        if asset.url:
            answer += f"\n{asset.url}"
        return answer, [asset.id], []
    if catalog.public_case_status != "none_confirmed":
        return None, [], ["Уточнить у владельца наличие и право раскрытия релевантного кейса"]
    answer = "Подходящего публичного кейса пока нет."
    if any("пилот" in item.lower() for item in o.get("first_steps", [])):
        answer += " Можем сначала согласовать небольшой проверяемый пилот: объём, критерии результата, стоимость и условия остановки."
    else:
        answer += " Можем обсудить применимость заявленной услуги без обещания готового демо."
    return answer, [], []


def prepare_reply_draft(conversation, classification, catalog, event, duration_minutes=25) -> dict | None:
    c, assessment, e = map(as_dict, (conversation, classification, event))
    labels = set(assessment.get("labels", []))
    service_id = c.get("service_id", "")
    try:
        offer = catalog.service(service_id)
    except ValueError:
        return None
    if not offer.enabled:
        return None
    next_step = recommend_next_action(c, assessment)
    if not next_step["requires_approval"]:
        return None
    proof_ids = []
    claims = []
    if "case_request" in labels:
        text, proof_ids, blockers = case_answer(catalog, offer, assessment.get("new_text", ""), context=c)
        if blockers:
            return None
        claims = [{"text": catalog.proof(ref).allowed_summary, "proof_asset_id": ref, "kind": catalog.proof(ref).type}
                  for ref in proof_ids]
    elif "price_question" in labels:
        text = price_answer(offer)
    elif "timeline_question" in labels:
        text = ("Ограничения сроков: " + "; ".join(offer.timing_constraints) + "." if offer.timing_constraints
                else "Подтверждённый срок пока неизвестен; сначала нужно уточнить объём и доступность команды.")
    elif "information_request" in labels:
        text = f"Услуга: {offer.name}."
        if offer.allowed_claims:
            text += " " + offer.allowed_claims[0].text
            claims = [{"text": offer.allowed_claims[0].text, "offer_id": offer.id,
                       "kind": offer.allowed_claims[0].kind}]
        if offer.limitations:
            text += " Границы: " + "; ".join(offer.limitations) + "."
    elif next_step["action"] == "propose_meeting":
        text = (f"Предлагаю предметный разговор на {duration_minutes} минут о применимости «{offer.name}». "
                "Разберём один реальный пример процесса и определим границы следующего шага. "
                "Полезно участие ответственного за этот процесс. Готовы согласовать удобное время и часовой пояс?")
    elif next_step["action"] == "prepare_scheduling":
        text = "Давайте согласуем конкретные дату, время и часовой пояс для предметного разговора. Какой вариант вам удобен?"
    elif next_step["action"] == "request_referral":
        text = "Подскажите, пожалуйста, кто отвечает за этот процесс? К новому адресату обратимся только после отдельной проверки."
    elif "no_budget" in labels:
        text = "Спасибо за уточнение. Ограничение бюджета временное или задачу пока следует отложить?"
    else:
        text = "Какую конкретную задачу в этом процессе вы хотели бы проверить в первую очередь?"
    sender = catalog.sender_profile
    body = str(text) + (f"\n\n{sender.signature}" if sender.signature else "")
    body += '\n\nЕсли дальнейшие обращения не нужны, ответьте «не писать».'
    subject = c.get("subject", e.get("subject", "Обсуждение задачи"))
    if not subject.lower().startswith("re:"):
        subject = "Re: " + subject
    payload = {
        "conversation_id": c["id"], "opportunity_id": c.get("opportunity_id"), "account_id": c.get("account_id"),
        "recipient": e.get("sender", c.get("recipient")), "channel": c.get("channel", "email"),
        "sender_account": sender.from_email, "mailbox_id": c.get("mailbox_id", "default"),
        "service_id": service_id, "proof_asset_ids": proof_ids, "kind": "reply", "goal": next_step["reason"],
        "business_action": next_step["action"],
        "meeting_proposal": ({"duration_minutes": duration_minutes,
            "goal": "Разобрать один реальный пример процесса и определить применимость следующего шага"}
            if next_step["action"] == "propose_meeting" else None),
        "subject": subject, "original_subject": c.get("subject", e.get("subject", "Обсуждение задачи")),
        "body": body, "attachments": [], "inbound_event_id": e["id"],
        "context_version": c.get("context_version", 1), "stats_version": c.get("stats_version", 0),
        "catalog_version": catalog.version, "dependency_version": catalog.dependency_version(service_id, proof_ids),
        "contact_permission": c.get("contact_permission", {"status": "unknown"}), "requires_fresh_sync": True,
        "offer_snapshot": offer.model_dump(mode="json"), "sender_snapshot": sender.model_dump(mode="json"),
        "validation_results": [], "claims": claims,
        "evidence_refs": [e["id"]], "evidence_snapshot": [{"id": e["id"], "kind": "conversation_confirmation", "quote": assessment.get("new_text", ""), "inbound_message_id": e["id"], "author_id": e.get("sender")}],
        "thread_id": e.get("thread_id", c.get("provider_thread_id")), "in_reply_to": e.get("rfc_message_id"),
        "references": c.get("rfc_message_ids", []), "source_url": c.get("source_url", ""),
        "estimated_cost": {"known": False, "source": "Transport tariff is not configured"},
        "reason": next_step["reason"], "unknowns": c.get("unknowns", []),
        "allowed_delivery_method": "pending_approval",
    }
    stamp_draft_content(payload)
    payload["validation_results"] = validate_draft(
        payload, payload["evidence_snapshot"], offer, catalog.proof_assets,
        require_provenance=True,
    )
    return payload
