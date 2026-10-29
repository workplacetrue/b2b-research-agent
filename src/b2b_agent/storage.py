import csv
import json
import sqlite3
from pathlib import Path

from .models import Research


class Store:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS companies (
                domain TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS errors (
                id INTEGER PRIMARY KEY, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                domain TEXT NOT NULL, stage TEXT NOT NULL, message TEXT NOT NULL);
        """)

    def save(self, report: Research):
        with self.db:
            self.db.execute(
                "INSERT INTO companies VALUES (?, ?) ON CONFLICT(domain) "
                "DO UPDATE SET payload=excluded.payload",
                (report.domain, report.model_dump_json()),
            )

    def error(self, domain: str, stage: str, message: str):
        with self.db:
            self.db.execute(
                "INSERT INTO errors(domain, stage, message) VALUES (?, ?, ?)",
                (domain, stage, message),
            )

    def reports(self) -> list[Research]:
        return [
            Research.model_validate_json(r[0])
            for r in self.db.execute("SELECT payload FROM companies ORDER BY domain")
        ]

    def close(self):
        self.db.close()


def safe_cell(value: str) -> str:
    # Neutralize spreadsheet formula injection for all exported string cells.
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value


def export(reports: list[Research], directory: str):
    path = Path(directory)
    path.mkdir(parents=True, exist_ok=True)
    (path / "results.json").write_text(
        json.dumps([r.model_dump(mode="json") for r in reports], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    fields = [
        "domain",
        "website",
        "status",
        "icp",
        "automation",
        "facts",
        "contacts",
        "hypotheses",
        "draft",
        "errors",
    ]
    with (path / "results.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for report in reports:
            data = report.model_dump(mode="json")
            row = {key: data[key] for key in fields if key in data}
            row.update(icp=report.score.icp, automation=report.score.automation)
            writer.writerow(
                {
                    k: safe_cell(
                        json.dumps(v, ensure_ascii=False) if isinstance(v, list) else str(v or "")
                    )
                    for k, v in row.items()
                }
            )
