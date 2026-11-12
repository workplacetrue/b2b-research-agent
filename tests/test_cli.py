"""Exercise public commands through separate database connections; no live mail."""
import json
import sys
from types import SimpleNamespace

import pytest

from b2b_agent.cli import main
from b2b_agent.config import Config
from b2b_agent.outbox import Outbox
from b2b_agent.state import WorkflowStore


@pytest.fixture
def commands(tmp_path, monkeypatch, capsys):
    config = Config(input_path=str(tmp_path / "absent-input"),
        outreach={"from_email":"owner@example.org", "signature":"Оператор"},
        inbox={"mailbox_id":"work-mailbox"})
    config_path = tmp_path / "custom-config.json"
    config_path.write_text(config.model_dump_json(),encoding="utf-8")
    database = tmp_path / "private.sqlite"
    output = tmp_path / "exports"

    def invoke(command, *args):
        monkeypatch.setattr(sys,"argv",["b2b-agent",command,*map(str,args),
            "--config",str(config_path),"--db",str(database),"--output",str(output)])
        result = main()
        text = capsys.readouterr().out.strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = text
        return result,data

    invoke.db = str(database)
    invoke.config = config
    invoke.config_path = config_path
    invoke.tmp = tmp_path
    return invoke


def inspect(commands):
    page = commands.tmp / "request.html"
    page.write_text('''<html><main><article><h1>Обработка заявок</h1>
      <time datetime="2026-11-04"></time>
      <p>Ищем подрядчика для автоматизации обработки заявок.</p>
      <p>Для откликов пишите на buyer@customer.ru</p></article></main></html>''',encoding="utf-8")
    code,action = commands("inspect-request","--html",page,"--url","https://platform.example/tasks/1","--now","2026-11-05T10:00:00+00:00")
    assert code == 0,action
    assert action["status"] == "pending_approval"
    return action


def test_cli_approval_pause_restart_simulation_and_immutable_history(commands):
    action = inspect(commands)
    assert commands("send-action",action["id"],"--simulate")[0] == 2
    assert commands("approve-action",action["id"],"--actor","owner")[0] == 0
    assert commands("pause","--scope","global","--reason","pause for review")[0] == 0
    reopened = WorkflowStore(commands.db)
    assert reopened.pause_reason({}) == "pause for review"
    reopened.close()
    assert commands("send-action",action["id"],"--simulate")[0] == 2
    assert commands("resume","--scope","global")[0] == 0
    code,sent = commands("send-action",action["id"],"--simulate")
    assert code == 0,sent
    assert sent["status"] == "simulated_sent" and sent["simulation"]
    assert commands("edit-action",action["id"],"--subject","Rewrite history")[0] == 2
    box = Outbox(commands.db)
    assert box.workflow.get_action(action["id"])["subject"] == action["subject"]
    assert box.db.execute("SELECT status FROM outbound_messages").fetchone()[0] == "simulated_sent"
    box.close()
    assert commands("send-action",action["id"],"--simulate")[0] == 2


def test_cli_edit_invalidates_exact_approval(commands):
    action = inspect(commands)
    assert commands("approve-action",action["id"],"--actor","owner")[0] == 0
    body = commands.tmp / "reviewed.txt"
    body.write_text(action["body"] + "\nСпасибо.",encoding="utf-8")
    code,edited = commands("edit-action",action["id"],"--body-file",body)
    assert code == 0 and edited["status"] == "pending_approval"
    assert commands("send-action",action["id"],"--simulate")[0] == 2
    assert commands("approve-action",action["id"],"--actor","owner")[0] == 0
    assert commands("send-action",action["id"],"--simulate")[0] == 0


def test_cli_import_keeps_sync_stale_until_entire_batch_applied(commands,monkeypatch):
    action = inspect(commands)
    assert commands("approve-action",action["id"],"--actor","owner")[0] == 0
    store = WorkflowStore(commands.db)
    conv = store.get_conversation(action["conversation_id"])
    conv["provider_thread_id"] = "thread-cli"
    store.put_conversation(conv)
    store.mark_sync("work-mailbox")
    store.close()
    messages = commands.tmp / "replies.json"
    messages.write_text(json.dumps([{"provider_message_id":"reply-cli","thread_id":"thread-cli", "sender":"buyer@customer.ru", "recipients":["owner@example.org"], "body":"Не пишите мне больше."}]),encoding="utf-8")
    from b2b_agent import conversations
    original = conversations.apply_inbound_event

    def check_stale(store,event,*args):
        reader = WorkflowStore(commands.db)
        assert not reader.sync_is_fresh("work-mailbox")
        reader.close()
        return original(store,event,*args)

    monkeypatch.setattr(conversations,"apply_inbound_event",check_stale)
    code,result = commands("import-replies",messages)
    assert code == 0,result
    assert commands("import-replies",messages)[0] == 0
    store = WorkflowStore(commands.db)
    assert store.sync_is_fresh("work-mailbox")
    assert len(store.list_inbound()) == 1
    assert store.get_action(action["id"])["status"] == "cancelled"
    assert store.is_suppressed({"recipient":"buyer@customer.ru"})
    store.close()
    assert commands("send-action",action["id"],"--simulate")[0] == 2


def test_cli_failed_import_leaves_mailbox_stale(commands,monkeypatch):
    messages = commands.tmp / "replies.json"
    messages.write_text(json.dumps([{"provider_message_id":"reply","sender":"buyer@example.com","body":"Hello"}]),encoding="utf-8")
    store = WorkflowStore(commands.db)
    store.mark_sync("work-mailbox")
    store.close()

    def fail(*args):
        raise ValueError("synthetic apply failure")

    monkeypatch.setattr("b2b_agent.conversations.apply_inbound_event",fail)
    assert commands("import-replies",messages)[0] == 2
    store = WorkflowStore(commands.db)
    assert not store.sync_is_fresh("work-mailbox")
    store.close()


def test_cli_manual_confirmation_uses_current_individual_approval(commands):
    action = inspect(commands)
    store = WorkflowStore(commands.db)
    store.update_action(action["id"],channel="platform",required_channel="platform")
    store.close()
    assert commands("approve-action",action["id"],"--actor","owner")[0] == 0
    code,manual = commands("send-action",action["id"],"--simulate")
    assert code == 0,manual
    assert manual["status"] == "awaiting_manual_send"
    code,confirmed = commands("confirm-manual-send",action["id"],"--actor","owner")
    assert code == 0,confirmed
    assert confirmed["status"] == "sent_confirmed_by_user"
    store = WorkflowStore(commands.db)
    assert store.db.execute("SELECT COUNT(*) FROM outbound_messages").fetchone()[0] == 0
    store.close()


def test_cli_bounded_gmail_marks_partial_batch_stale(commands,monkeypatch):
    config = commands.config.model_copy(deep=True)
    config.inbox.enabled = True
    config.inbox.provider = "gmail"
    commands.config_path.write_text(config.model_dump_json(),encoding="utf-8")
    batches = [SimpleNamespace(messages=[],cursor="history-1",has_more=True,history_reset=False),
               SimpleNamespace(messages=[],cursor="history-2",has_more=False,history_reset=False)]

    class FakeInbox:
        def __init__(self,*args,**kwargs):
            pass

        async def sync(self,cursor):
            reader = WorkflowStore(commands.db)
            assert not reader.sync_is_fresh("work-mailbox")
            reader.close()
            return batches.pop(0)

    async def token(*args):
        return "synthetic-token"

    monkeypatch.setattr("b2b_agent.inbox.GmailInboxProvider",FakeInbox)
    monkeypatch.setattr("b2b_agent.mail_delivery.gmail_access_token",token)
    assert commands("sync-replies")[0] == 0
    store = WorkflowStore(commands.db)
    assert not store.sync_is_fresh("work-mailbox")
    store.close()
    assert commands("sync-replies")[0] == 0
    store = WorkflowStore(commands.db)
    assert store.sync_is_fresh("work-mailbox")
    assert store.db.execute("SELECT cursor FROM workflow_sync WHERE mailbox_id='work-mailbox'").fetchone()[0] == "history-2"
    store.close()


def test_legacy_export_and_suppress_email_remain_offline(tmp_path,monkeypatch,capsys):
    database = str(tmp_path / "legacy.sqlite")
    monkeypatch.setattr(sys,"argv",["b2b-agent","suppress-email","--db",database,"--email","blocked@example.org"])
    assert main() == 0
    capsys.readouterr()
    monkeypatch.setattr(sys,"argv",["b2b-agent","export","--db",database,"--output",str(tmp_path / "out")])
    assert main() == 2  # Empty research corpus retains the legacy exit code.
    capsys.readouterr()
    box = Outbox(database)
    assert box.workflow.is_suppressed({"recipient":"blocked@example.org"})
    assert box.db.execute("SELECT COUNT(*) FROM outbound_messages").fetchone()[0] == 0
    box.close()
