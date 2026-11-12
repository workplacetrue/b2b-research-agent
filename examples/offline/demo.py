"""Reproducible synthetic lifecycle: never connects to search, LLM, mail or calendar."""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path

import httpx

from b2b_agent.company_stats import build_report, export_stats
from b2b_agent.config import load_config
from b2b_agent.conversations import apply_inbound_event
from b2b_agent.crawler import Page
from b2b_agent.inbox import import_replies
from b2b_agent.meetings import build_meeting_brief, mark_meeting
from b2b_agent.outbox import Outbox
from b2b_agent.outreach import inspect_ad
from b2b_agent.workflow import current_catalog, dispatch_action, queue_public_request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="output/demo")
    args = parser.parse_args()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    db_path = output / "demo.sqlite"
    if db_path.exists():
        raise SystemExit("Use a new --output directory; the demo preserves existing state.")
    root = Path(__file__).resolve().parents[2]
    config = load_config(str(root / "examples/offline/config.json"))
    config.input_path = str(root / "examples/INPUT")
    box = Outbox(str(db_path))
    try:
        page = Page("https://platform.example/requests/demo-1", "",
            (root / "examples/offline/request.html").read_text(encoding="utf-8"), [])
        assessment = inspect_ad(page, config, now=datetime(2026, 11, 12, 12, tzinfo=UTC))
        assert assessment.status == "ready"
        initial = queue_public_request(box, assessment, config)
        assert initial["status"] == "pending_approval"
        # These approvals are fixture operator events in a synthetic database,
        # not an autonomous approval mechanism available to the production agent.
        box.workflow.approve_action(initial["id"], "offline-demo-operator")
        async def simulate(action_id):
            async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: (_ for _ in ()).throw(AssertionError("Network forbidden in offline demo")))) as client:
                return await dispatch_action(box, action_id, config, client, simulated=True)
        sent = asyncio.run(simulate(initial["id"]))
        reply = EmailMessage()
        reply["From"], reply["To"] = "buyer@customer.example", config.outreach.from_email
        reply["Subject"] = "Re: " + initial["subject"]
        reply["Message-ID"] = "<demo-inbound-1@customer.example>"
        reply["In-Reply-To"] = sent["rfc_message_id"]
        reply.set_content("Интересно. Нужна обработка заявок от организаций, давайте обсудим применимость.")
        reply_path = output / "synthetic-reply.eml"
        reply_path.write_bytes(reply.as_bytes())
        events = import_replies(reply_path, mailbox_id=config.inbox.mailbox_id)
        box.workflow.mark_sync(config.inbox.mailbox_id)
        update = apply_inbound_event(box.workflow, events[0], current_catalog(config), config)
        meeting_draft = update["draft"]
        box.workflow.approve_action(meeting_draft["id"], "offline-demo-operator")
        asyncio.run(simulate(meeting_draft["id"]))
        conv_id = initial["conversation_id"]
        mark_meeting(box.workflow, conv_id, "scheduled", local_time="2026-11-16T15:00:00",
            timezone="Europe/Moscow", actor="offline-demo-operator", confirmed=True)
        brief = build_meeting_brief(box.workflow, conv_id, current_catalog(config))
        (output / "meeting-brief.json").write_text(brief.model_dump_json(indent=2), encoding="utf-8")
        mark_meeting(box.workflow, conv_id, "held", actor="offline-demo-operator", notes="Synthetic fixture outcome")
        export_stats(box.workflow, output / "STATS")
        report = build_report(box.workflow)
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"simulation": True, "real_messages_sent": 0, "conversation_id": conv_id,
            "report": str(output / "report.json"), "brief": str(output / "meeting-brief.json")}, ensure_ascii=False))
    finally:
        box.close()


if __name__ == "__main__":
    main()
