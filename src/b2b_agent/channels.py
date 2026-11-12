"""Channel capability declarations; copying a draft is never recorded as delivery."""
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class ChannelCapabilities:
    channel: str
    can_send: bool = False
    can_read: bool = False
    recipient_types: tuple[str, ...] = ()
    threading: str = "manual"
    opt_out: str = "manual_import"
    price: str = "unknown"
    quota: str = "unknown"
    limitation: str = "Официальный адаптер не подключён; требуется ручная отправка"


class ChannelAdapter:
    """An adapter must declare rights and costs before it can dispatch."""
    capabilities: ChannelCapabilities

    def __init__(self, capabilities: ChannelCapabilities):
        self.capabilities = capabilities

    def validate(self, *, channel: str, required_channel: str | None = None) -> None:
        if channel != self.capabilities.channel:
            raise ValueError("Channel does not match the registered adapter")
        if required_channel and required_channel != channel:
            raise ValueError("Заказчик указал другой обязательный канал отклика")


def channel_capabilities(channel: str, *, inbox_connected: bool = False) -> ChannelCapabilities:
    if channel == "email":
        return ChannelCapabilities(channel="email", can_send=True, can_read=inbox_connected,
            recipient_types=("verified_literal_mailbox",), threading="RFC Message-ID/provider thread",
            opt_out="reply_import", price="configured_transport", limitation="Каждое сообщение требует отдельного approval и проверки транспорта")
    if channel == "telegram":
        return ChannelCapabilities(channel=channel, recipient_types=("existing_authorized_chat",),
            limitation="Обычный Bot API не поддерживает cold DM произвольному username; используйте ручную задачу")
    return ChannelCapabilities(channel=channel)


def prepare_manual_task(draft: dict, *, required_channel: str | None = None) -> dict:
    channel = draft.get("channel", "platform")
    if required_channel and channel != required_channel:
        raise ValueError("Нельзя обходить обязательный канал отклика")
    if channel == "email":
        raise ValueError("Email uses the guarded mail dispatch, not a manual adapter")
    return {**draft, "status": "draft_ready", "delivery_status": "draft",
            "capabilities": channel_capabilities(channel).__dict__}


def advance_manual_task(task: dict, event: Literal["approved", "copied", "sent_confirmed"], *,
                        actor: str, approval_current: bool = False) -> dict:
    if not actor.strip():
        raise ValueError("Actor is required")
    current = task.get("status", "draft_ready")
    if event == "approved":
        if not approval_current:
            raise ValueError("Exact current message approval is required")
        status = "approved_for_manual_send"
    elif event == "copied":
        if not approval_current or current not in {"approved_for_manual_send", "awaiting_manual_send"}:
            raise ValueError("Copying requires a current separately approved manual task")
        status = "awaiting_manual_send"
    elif event == "sent_confirmed":
        if not approval_current or current != "awaiting_manual_send":
            raise ValueError("User confirmation cannot create a retroactive approval")
        status = "sent_confirmed_by_user"
    else:
        raise ValueError("Unknown manual event")
    return {**task, "status": status, "actor": actor,
            "delivery_status": "sent_confirmed_by_user" if event == "sent_confirmed" else "draft"}


def prepare_manual_send(store, action_id: str, *, current_dependencies: str) -> dict:
    """Expose approved text to the operator without claiming a send occurred."""
    action = store.get_action(action_id)
    if not action:
        raise ValueError("Unknown action")
    if action.get("channel", "email") == "email":
        raise ValueError("Email requires guarded transport dispatch")
    blocker = store.approval_blocker(action, current_dependencies=current_dependencies)
    if blocker:
        raise ValueError("Manual send on hold: " + blocker)
    required = action.get("required_channel")
    if required and required != action["channel"]:
        raise ValueError("Нельзя обходить обязательный канал отклика")
    store.update_action(action_id, status="awaiting_manual_send")
    return store.get_action(action_id)


def confirm_manual_sent(store, action_id: str, *, actor: str, current_dependencies: str) -> dict:
    """User confirmation checks the same unconsumed exact approval and stop rules."""
    from .state import encode
    if not actor.strip():
        raise ValueError("Actor is required")
    store.db.execute("BEGIN IMMEDIATE")
    try:
        action = store.get_action(action_id)
        if not action or action.get("status") != "awaiting_manual_send":
            raise ValueError("No approved manual send awaits confirmation")
        check = {**action, "status": "approved"}
        blocker = store.approval_blocker(check, current_dependencies=current_dependencies)
        if blocker:
            raise ValueError("Manual confirmation on hold: " + blocker)
        approval = store._get("workflow_approvals", action["approval_id"])
        approval["status"] = "consumed"
        store.db.execute("UPDATE workflow_approvals SET payload=? WHERE id=?", (encode(approval), approval["id"]))
        action.update(status="sent_confirmed_by_user", delivery_status="sent_confirmed_by_user", confirmed_by=actor)
        store._write_action(action)
        store.db.commit()
    except BaseException:
        store.db.rollback()
        raise
    return store.get_action(action_id)
