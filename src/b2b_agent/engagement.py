"""Bounded, explainable recommendations; external text never grants authority."""
from __future__ import annotations

from typing import Any


def as_dict(value: Any) -> dict:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return vars(value)


def recommend_next_action(conversation, assessment=None) -> dict:
    """Choose one next step without treating a reply as permission to dispatch."""
    c = as_dict(conversation)
    labels = set(as_dict(assessment).get("labels", c.get("last_labels", [])))
    holds = set(c.get("holds", []))
    reason = "Недостаточно проверенных сведений для содержательного ответа"
    action = "human_handoff"
    if c.get("is_terminal") or labels & {"opt_out", "account_opt_out", "not_interested"}:
        action, reason = "stop", "Явный отказ/закрытая текущая попытка"
    elif holds & {"suppression", "human_handoff", "manual_review"}:
        action, reason = "human_handoff", "Действует запрет или ручная передача"
    elif labels & {"auto_reply", "hard_bounce", "soft_bounce"}:
        action, reason = "no_action", "Служебное событие, не предметный интерес"
    elif "commercial_exception" in labels or "privacy_question" in labels:
        action, reason = "human_handoff", "Условия требуют решения владельца"
    elif "case_request" in labels:
        action, reason = "send_information", "Проверить релевантный разрешённый материал"
    elif labels & {"price_question", "timeline_question"}:
        action, reason = "answer_question", "Ответить известными утверждёнными сведениями письменно"
    elif "defer" in labels or c.get("activity") == "deferred":
        action, reason = "defer", "Собеседник попросил вернуться позже; срок не угадывается"
    elif "information_request" in labels:
        action, reason = "send_information", "Запрошены сведения; согласие на встречу не получено"
    elif "referral" in labels:
        action, reason = "human_handoff", "Новый адресат требует отдельной проверки и разрешения"
    elif "wrong_role" in labels:
        action, reason = "request_referral", "Уточнить ответственного без автоматического обращения к нему"
    elif "no_budget" in labels:
        action, reason = "ask_one_question", "Уточнить ограничение без обещания вписаться в бюджет"
    elif "satisfied" in labels:
        action, reason = "no_action", "Процесс устраивает клиента; проблема не подтверждена"
    elif "automated" in labels and "interest" not in labels:
        action, reason = "no_action", "Прежняя гипотеза пересмотрена: процесс автоматизирован"
    elif "existing_system" in labels:
        action, reason = ("ask_one_question", "Уточнить только открытый незакрытый участок") if "interest" in labels else ("no_action", "Наличие системы не подтверждает новую потребность")
    elif "meeting_interest" in labels:
        action, reason = "prepare_scheduling", "Есть согласие обсудить, но время требует отдельного согласования"
    elif "interest" in labels and (c.get("confirmed_task") or c.get("task")):
        action, reason = "propose_meeting", "Релевантная задача и предметный интерес позволяют полезный разговор"
    elif "interest" in labels:
        action, reason = "ask_one_question", "Уточнить одну существенную задачу"
    return {"action": action, "reason": reason, "requires_approval": action in {
        "answer_question", "ask_one_question", "send_information", "request_referral",
        "propose_meeting", "prepare_scheduling",
    }}


def plan_contact(opportunity, contact=None, context=None) -> dict:
    o, c, ctx = as_dict(opportunity), as_dict(contact), as_dict(context)
    fit = o.get("task_fit", as_dict(o.get("assessment")).get("task_fit", "unknown"))
    if isinstance(fit, dict):
        fit = fit.get("value", "unknown")
    permission = as_dict(c.get("permission", {})).get("status", c.get("permission_status", "unknown"))
    blockers = list(o.get("blockers", []))
    if fit == "unsupported":
        blockers.append("Задача вне поддерживаемой услуги")
    if permission != "allowed":
        blockers.append("Нет проверенного основания для контакта")
    return {
        "service_id": o.get("service_id"), "workflow": o.get("service_id"),
        "origin": o.get("origin", "company_research"), "task": o.get("task", ""),
        "recipient": c.get("value", c.get("recipient", "")), "channel": c.get("channel", "email"),
        "reason": o.get("reason", "Проверить применимость одного сценария"),
        "facts": o.get("evidence_ids", []), "hypothesis": o.get("hypothesis", ""),
        "role": c.get("role", "unknown"), "unknowns": o.get("unknowns", []),
        "goal": "Уточнить применимость конкретной задачи", "task_fit": fit,
        "recommended_action": "stop" if fit == "unsupported" else (
            "ask_one_question" if fit in {"partial", "unknown"} else "check_relevance"),
        "blockers": blockers, "context_version": ctx.get("context_version", 1),
        "question": "Какой именно участок процесса требуется автоматизировать?",
    }
