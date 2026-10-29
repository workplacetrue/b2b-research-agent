import argparse
import asyncio
import json
import logging
from pathlib import Path

from .config import Config, load_config
from .discovery import read_csv
from .pipeline import run
from .storage import Store, export


def main():
    parser = argparse.ArgumentParser(description="B2B research; drafts only, never sends messages")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--config", default="config.json")
    execute = sub.add_parser("run")
    execute.add_argument("--config", default="config.json")
    execute.add_argument("--csv")
    execute.add_argument("--db", default="output/research.sqlite")
    execute.add_argument("--output", default="output")
    out = sub.add_parser("export")
    out.add_argument("--db", default="output/research.sqlite")
    out.add_argument("--output", default="output")
    args = parser.parse_args()
    if args.command == "init":
        with Path(args.config).open("x", encoding="utf-8") as f:
            f.write(json.dumps(Config().model_dump(), ensure_ascii=False, indent=2))
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
    store = Store(args.db)
    try:
        if args.command == "export":
            reports = store.reports()
        else:
            config = load_config(args.config)
            reports = asyncio.run(run(config, read_csv(args.csv) if args.csv else [], store))
        export(reports, args.output)
        summary = {
            "companies": len(reports),
            "ok": sum(r.status == "ok" for r in reports),
            "partial": sum(r.status == "partial" for r in reports),
            "failed": sum(r.status == "failed" for r in reports),
        }
        print(json.dumps(summary, ensure_ascii=False))
        return 0 if reports and summary["ok"] == len(reports) else 2
    except Exception as error:
        logging.error("Command failed: %s", type(error).__name__)
        print("Не удалось завершить команду; проверьте конфигурацию и журнал ошибок.")
        return 2
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
