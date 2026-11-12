import argparse
import asyncio
import json
import logging
from pathlib import Path

import httpx

from .config import Config, load_config
from .discovery import read_csv, read_requests_csv
from .mail_delivery import DeliveryConfigurationError
from .models import RequestCandidate
from .outbox import Outbox
from .outreach import auto_reply
from .pipeline import run
from .storage import Store, export


def main():
    parser = argparse.ArgumentParser(
        description="B2B research, approved outreach, replies and first meetings"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--config", default="config.json")
    execute = sub.add_parser("run")
    execute.add_argument("--config", default="config.json")
    execute.add_argument("--csv")
    execute.add_argument("--requests-csv")
    execute.add_argument("--db", default="output/research.sqlite")
    execute.add_argument("--output", default="output")
    execute.add_argument(
        "--dry-run", action="store_true", help="Preview outbound replies without sending"
    )
    out = sub.add_parser("export")
    out.add_argument("--db", default="output/research.sqlite")
    out.add_argument("--output", default="output")
    requests = sub.add_parser("find-requests", help="Find public requests for operator review")
    requests.add_argument("--config", default="config.json")
    requests.add_argument("--output", default="output")
    requests.add_argument("--db", default="output/research.sqlite")
    replies = sub.add_parser("auto-reply", help="Research ads and prepare individually approved drafts (never sends)")
    replies.add_argument("--config", default="config.json")
    replies.add_argument(
        "--requests-json", help="Candidate JSON from find-requests; pages are rechecked"
    )
    replies.add_argument("--db", default="output/research.sqlite")
    replies.add_argument("--output", default="output")
    replies.add_argument("--dry-run", action="store_true")
    suppress = sub.add_parser("suppress-email", help="Block further responses to a recipient")
    suppress.add_argument("--email", required=True)
    suppress.add_argument("--db", default="output/research.sqlite")
    from .ops_cli import COMMANDS, handle, register
    register(sub)
    args = parser.parse_args()
    if args.command in COMMANDS:
        try:
            return handle(args)
        except (ValueError, KeyError, OSError) as error:
            print(str(error))
            return 2
    if args.command == "init":
        with Path(args.config).open("x", encoding="utf-8") as f:
            f.write(json.dumps(Config().model_dump(), ensure_ascii=False, indent=2))
        from .catalog import initialize_input
        initialize_input(Config().input_path, Config())
        return 0
    if args.command == "suppress-email":
        box = Outbox(args.db)
        try:
            box.suppress(args.email)
        finally:
            box.close()
        print("Адрес исключён из автооткликов.")
        return 0
    Path(args.output).mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        handlers=[
            logging.FileHandler(Path(args.output) / "errors.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # HTTPX INFO logs include provider URLs; keep them out of persisted logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    store, box = None, None
    try:
        if args.command == "find-requests":
            config = load_config(args.config)
            box = Outbox(args.db)

            async def search():
                from .outreach import cached_request_search
                async with httpx.AsyncClient(timeout=config.timeout_seconds) as client:
                    return await cached_request_search(config, client, box.workflow)

            candidates = asyncio.run(search())
            (Path(args.output) / "request_candidates.json").write_text(
                json.dumps([c.model_dump() for c in candidates], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(json.dumps({"unverified_requests": len(candidates)}))
            return 0 if candidates else 2
        if args.command == "auto-reply":
            config = load_config(args.config)
            candidates = None
            if args.requests_json:
                candidates = [
                    RequestCandidate.model_validate(item)
                    for item in json.loads(
                        Path(args.requests_json).read_text(encoding="utf-8"),
                    )
                ]
            box = Outbox(args.db)
            replies = asyncio.run(auto_reply(config, box, candidates, dry_run=args.dry_run))
            _export_replies(replies, args.output)
            return _reply_exit_code(replies)
        store = Store(args.db)
        replies = []
        if args.command == "export":
            reports = store.reports()
        else:
            config = load_config(args.config)
            reports = asyncio.run(
                run(
                    config,
                    read_csv(args.csv) if args.csv else [],
                    store,
                    request_sources=read_requests_csv(args.requests_csv)
                    if args.requests_csv
                    else None,
                )
            )
            if config.outreach.enabled:
                box = Outbox(args.db)
                replies = asyncio.run(auto_reply(config, box, dry_run=args.dry_run))
                _export_replies(replies, args.output)
        export(reports, args.output)
        summary = {
            "companies": len(reports),
            "ok": sum(r.status == "ok" for r in reports),
            "partial": sum(r.status == "partial" for r in reports),
            "failed": sum(r.status == "failed" for r in reports),
            "public_requests_to_verify": sum(
                r.qualification.interest == "public_request_to_verify" for r in reports
            ),
        }
        print(json.dumps(summary, ensure_ascii=False))
        if any(r.status != "ok" for r in reports):
            return 2
        return _reply_exit_code(replies) if replies else (0 if reports else 2)
    except DeliveryConfigurationError as error:
        print(str(error))
        return 2
    except Exception as error:
        logging.error("Command failed: %s", type(error).__name__)
        print("Не удалось завершить команду; проверьте конфигурацию и журнал ошибок.")
        return 2
    finally:
        if store is not None:
            store.close()
        if box is not None:
            box.close()


def _export_replies(replies, directory):
    (Path(directory) / "outreach.json").write_text(
        json.dumps([r.model_dump(mode="json") for r in replies], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    counts = {}
    for reply in replies:
        counts[reply.status] = counts.get(reply.status, 0) + 1
    print(json.dumps({"outreach": counts}, ensure_ascii=False))


def _reply_exit_code(replies):
    return (
        2
        if not replies
        or any(r.status in {"failed", "uncertain", "research_failed"} for r in replies)
        else 0
    )


if __name__ == "__main__":
    raise SystemExit(main())
