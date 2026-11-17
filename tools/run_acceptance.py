"""Offline acceptance report; test execution is evidence, never semantic certification."""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def summarize(manifest, junit):
    cases = ET.parse(junit).getroot().findall(".//testcase")
    results = {}
    for case in cases:
        node = case.attrib["classname"].replace(".", "/") + ".py::" + case.attrib["name"]
        status = "completed"
        if case.find("error") is not None:
            status = "harness_error"
        elif case.find("failure") is not None:
            status = "failed"
        elif case.find("skipped") is not None:
            status = "not_run"
        results[node] = status
    rows = []
    for scenario in manifest:
        evidence = []
        for ref in scenario["tests"]:
            matched = {k: v for k, v in results.items() if k == ref or k.startswith(ref + "[")}
            evidence.append({"test": ref, "runs": matched, "status": (
                "harness_error" if not matched else
                next((s for s in ("harness_error", "failed", "not_run")
                      if s in matched.values()), "completed"))})
        status = next((s for s in ("harness_error", "failed", "not_run")
                       if any(e["status"] == s for e in evidence)), "completed")
        rows.append({**scenario, "offline_status": status, "evidence": evidence,
                     "semantic_status": "not_run" if scenario["semantic"] != "—" else "not_applicable",
                     "live_status": "not_run"})
    return rows, dict(Counter(results.values()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    manifest_path = ROOT / "evaluation/scenarios.json"
    manifest = json.loads(manifest_path.read_text())
    env = {**os.environ, "CRAWL4_AI_BASE_DIRECTORY": str(output / "crawl-cache")}
    command = [sys.executable, "-m", "pytest", "-q", "-p", "tools.offline_guard",
               "--junitxml=" + str(output / "junit.xml"),
               "--basetemp=" + str(output / "state")]
    started = datetime.now(UTC)
    run = subprocess.run(command, cwd=ROOT, env=env, text=True, capture_output=True)
    (output / "pytest.log").write_text(run.stdout + run.stderr)
    try:
        rows, counts = summarize(manifest, output / "junit.xml")
        status = "completed" if run.returncode == 0 else "failed"
        if any(r["offline_status"] == "harness_error" for r in rows):
            status = "harness_error"
    except (OSError, ET.ParseError, KeyError):
        rows, counts, status = [], {}, "harness_error"
    report = {"run_id": uuid.uuid4().hex, "run_status": status,
              "started_at": started.isoformat(), "finished_at": datetime.now(UTC).isoformat(),
              "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
              "dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT)),
              "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
              "command": command, "tests": counts, "scenarios": rows,
              "agent_cost": 0, "judge_cost": 0, "currency": "USD",
              "llm_status": "not_run", "judge_status": "not_run", "human_review": "not_run",
              "release_acceptance": "blocked",
              "limitations": ["Offline tests do not establish semantic or live capability acceptance.",
                               "Temporary SQLite/fixtures retained under state; no model reasoning collected."]}
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    lines = ["# Offline acceptance", "", f"Run: {report['run_id']}; status: {status}",
             f"Commit: {report['commit']}; dirty: {report['dirty']}", "",
             f"Tests: {counts}", "", "LLM / judge / human / live: not_run. Release: blocked.", "",
             "| ID | Scenario | Offline | Semantic |", "|---|---|---|---|"]
    lines += [f"| {r['id']} | {r['title']} | {r['offline_status']} | {r['semantic_status']} |" for r in rows]
    lines += ["", "See report.json for executed test IDs and remaining semantic contracts.",
              "See pytest.log, junit.xml and state/ for failures and retained test state."]
    (output / "report.md").write_text("\n".join(lines) + "\n")
    print(json.dumps({"status": status, "tests": counts, "report": str(output / "report.json")}))
    return 0 if status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
