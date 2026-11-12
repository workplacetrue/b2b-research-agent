import csv
import hashlib
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
            CREATE TABLE IF NOT EXISTS research_snapshots (
                snapshot_id TEXT PRIMARY KEY, domain TEXT NOT NULL,
                observed_at TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS research_snapshot_domain ON research_snapshots(domain);
            CREATE TABLE IF NOT EXISTS evidence_snapshots (
                evidence_id TEXT NOT NULL, snapshot_id TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(evidence_id,snapshot_id));
            CREATE TABLE IF NOT EXISTS audit_snapshots (
                snapshot_id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS external_cache (
                cache_key TEXT PRIMARY KEY, payload TEXT NOT NULL, observed_at TEXT NOT NULL);
        """)
        # Old current-only payloads become immutable history before any new crawl
        # can replace them. No old JSON or f1 citation is rewritten.
        with self.db:
            for domain, payload in self.db.execute("SELECT domain,payload FROM companies").fetchall():
                data = json.loads(payload)
                snapshot_id = "snapshot:" + hashlib.sha256(payload.encode()).hexdigest()
                self.db.execute("INSERT OR IGNORE INTO research_snapshots VALUES(?,?,?,?)",
                    (snapshot_id, domain, data.get("observed_at", "unknown"), payload))

    def save(self, report: Research):
        payload = report.model_dump_json()
        snapshot_id = "snapshot:" + hashlib.sha256(payload.encode()).hexdigest()
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO research_snapshots VALUES(?,?,?,?)",
                (snapshot_id, report.domain, report.observed_at.isoformat(), payload))
            for evidence in report.evidence:
                self.db.execute("INSERT OR IGNORE INTO evidence_snapshots VALUES(?,?,?)",
                    (evidence.id, snapshot_id, evidence.model_dump_json()))
            self.db.execute(
                "INSERT INTO companies VALUES (?, ?) ON CONFLICT(domain) "
                "DO UPDATE SET payload=excluded.payload",
                (report.domain, payload),
            )

    put = save

    def history(self, domain: str) -> list[Research]:
        return [Research.model_validate_json(row[0]) for row in self.db.execute(
            "SELECT payload FROM research_snapshots WHERE domain=? ORDER BY observed_at,snapshot_id",
            (domain,))]

    def save_snapshot(self, kind: str, data: dict) -> str:
        """Offer/context/decision snapshots share a content-addressed audit store."""
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        key = f"{kind}:" + hashlib.sha256(payload.encode()).hexdigest()
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO audit_snapshots VALUES(?,?,?)", (key, kind, payload))
        return key

    def cached(self, key: str, max_age_seconds: int = 86400):
        from datetime import UTC, datetime
        row = self.db.execute("SELECT payload,observed_at FROM external_cache WHERE cache_key=?", (key,)).fetchone()
        if row and (datetime.now(UTC) - datetime.fromisoformat(row[1])).total_seconds() <= max_age_seconds:
            return json.loads(row[0])
        return None

    def cache(self, key: str, data):
        from datetime import UTC, datetime
        with self.db:
            self.db.execute("INSERT INTO external_cache VALUES(?,?,?) ON CONFLICT(cache_key) DO UPDATE SET payload=excluded.payload,observed_at=excluded.observed_at",
                (key, json.dumps(data, ensure_ascii=False), datetime.now(UTC).isoformat()))

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
    reports = sorted(
        reports,
        key=lambda r: (
            r.qualification.interest == "public_request_to_verify",
            r.score.icp,
            r.score.automation,
        ),
        reverse=True,
    )
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
        "sales",
        "documents",
        "qualification",
        "facts",
        "contacts",
        "hypotheses",
        "draft",
        "errors",
        "research_id",
        "version",
        "opportunity_id",
        "assessment",
        "engagement_plan",
        "evidence",
    ]
    with (path / "results.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for report in reports:
            data = report.model_dump(mode="json")
            row = {key: data[key] for key in fields if key in data}
            row.update(
                icp=report.score.icp,
                automation=report.score.automation,
                sales=report.score.sales,
                documents=report.score.documents,
            )
            writer.writerow(
                {
                    k: safe_cell(
                        json.dumps(v, ensure_ascii=False)
                        if isinstance(v, (list, dict))
                        else str(v if v is not None else "")
                    )
                    for k, v in row.items()
                }
            )
