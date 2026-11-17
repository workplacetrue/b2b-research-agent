"""Opt-in paid evaluation of the actual research runtime, with an independent judge."""
import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Literal

import httpx
from pydantic import Field, model_validator

from b2b_agent.analysis import analyze, assess_external_call
from b2b_agent.config import load_config
from b2b_agent.memory import check_context_budget, estimate_tokens
from b2b_agent.models import Research, StrictModel
from b2b_agent.state import WorkflowStore

ROOT = Path(__file__).resolve().parents[1]
RUBRIC = json.loads((ROOT / "evaluation/rubrics/v1.json").read_text())


class Criterion(StrictModel):
    name: str
    applicable: bool
    score: Literal[0, 1, 2] | None
    evidence_refs: list[str]
    reason: str = Field(min_length=1)


class Verdict(StrictModel):
    case_id: str
    status: Literal["pass", "fail", "manual_review"]
    criteria: list[Criterion]
    critical_violations: list[str]
    needs_human_review: bool

    @model_validator(mode="after")
    def consistent(self):
        names = [c.name for c in self.criteria]
        if sorted(names) != sorted(RUBRIC["criteria"]):
            raise ValueError("Every rubric criterion must occur exactly once")
        for c in self.criteria:
            if c.applicable != (c.score is not None):
                raise ValueError("Applicability and score disagree")
            if c.applicable and not c.evidence_refs:
                raise ValueError("Applicable criterion needs evidence")
        if self.status == "pass" and (self.critical_violations or self.needs_human_review
                                      or any(c.score in (0, 1) for c in self.criteria)):
            raise ValueError("Contradictory pass")
        if self.status == "manual_review" and not self.needs_human_review:
            raise ValueError("Manual review must be explicit")
        return self


def validate_verdict(raw, case_id, refs):
    verdict = Verdict.model_validate_json(raw)
    if verdict.case_id != case_id:
        raise ValueError("Wrong case ID")
    if any(not set(c.evidence_refs) <= set(refs) for c in verdict.criteria):
        raise ValueError("Unknown judge reference")
    return verdict.model_dump()


async def judge(config, store, payload, client, identifier):
    check_context_budget(payload, budget=config.memory.input_tokens,
                         model_window=config.memory.context_window,
                         response_reserve=config.memory.response_reserve_tokens)
    reservation = assess_external_call(config, "llm", paid=True, store=store,
        reservation_id=identifier, input_tokens=estimate_tokens(payload), run_scope="eval")
    actual = None
    try:
        response = await client.post(config.llm.base_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": "Bearer " + os.environ[config.llm.api_key_env]}, json=payload)
        response.raise_for_status()
        data = response.json()
        prices = config.budget.tool_prices["llm"]
        usage = data.get("usage", {})
        if prices.get("per_call") is not None:
            actual = prices["per_call"]
        elif all(k in usage for k in ("prompt_tokens", "completion_tokens")) and all(
                k in prices for k in ("input_per_million", "output_per_million")):
            actual = (usage["prompt_tokens"] * prices["input_per_million"]
                      + usage["completion_tokens"] * prices["output_per_million"]) / 1e6
        return data["choices"][0]["message"]["content"]
    finally:
        if reservation:
            reservation[0].reconcile(reservation[1], actual, billing_known=actual is not None)


def check_research(case, result):
    expected = case["expected"]
    failures = []
    if len(result.hypotheses) < expected["min_hypotheses"]:
        failures.append("missing_useful_hypothesis")
    if any(h.workflow not in expected["allowed_workflows"] for h in result.hypotheses):
        failures.append("unsupported_workflow")
    if result.qualification.interest != expected["interest"]:
        failures.append("wrong_interest")
    return failures


def checked_config(path):
    config = load_config(str(path))
    if not config.llm.enabled or config.outreach.enabled or config.inbox.enabled:
        raise ValueError("Enable LLM only; outreach and inbox must be disabled")
    if not config.llm.base_url.startswith("https://") or not os.environ.get(config.llm.api_key_env):
        raise ValueError("HTTPS endpoint and credential environment required")
    if config.budget.max_campaign_cost is None or config.budget.max_run_cost is None:
        raise ValueError("Explicit campaign and run cost caps required")
    return config


async def run(args):
    config = checked_config(args.config)
    judge_config = checked_config(args.judge_config) if args.judge_config else None
    if judge_config and judge_config.llm.model == config.llm.model:
        raise ValueError("Use a separate judge model")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    agent_store = WorkflowStore(output / "agent.sqlite")
    judge_store = WorkflowStore(output / "judge.sqlite")
    cases = json.loads((ROOT / "evaluation/cases/research.json").read_text())
    rows = []
    async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
        for case in cases:
            if case["split"] != args.split:
                continue
            for attempt in range(1, args.repeats + 1):
                identifier = f"{case['id']}-{attempt}"
                folder = output / identifier
                folder.mkdir()
                row = {"case_id": case["id"], "run_id": identifier, "status": "harness_error",
                       "judge_status": "not_run", "attempt": attempt}
                started = time.monotonic()
                captured = []

                async def capture(request):
                    # Never serialize headers/credentials. Synthetic inputs only.
                    captured.append(json.loads(request.content))

                result = Research.model_validate(case["input"])
                result.research_id = identifier
                try:
                    async with httpx.AsyncClient(timeout=60, follow_redirects=False,
                                                 event_hooks={"request": [capture]}) as agent_client:
                        await analyze(result, config, agent_client, policy_store=agent_store,
                                      run_scope="eval")
                    row["failures"] = check_research(case, result)
                    row["status"] = "failed" if row["failures"] else "completed"
                except ValueError as exc:
                    row.update(status="failed", error=type(exc).__name__)
                except Exception as exc:
                    row.update(status="harness_error", error=type(exc).__name__)
                (folder / "agent-request.json").write_text(json.dumps(captured, ensure_ascii=False, indent=2))
                observed = result.model_dump(mode="json")
                (folder / "initial.json").write_text(json.dumps(case["input"], ensure_ascii=False, indent=2))
                (folder / "observed.json").write_text(json.dumps(observed, ensure_ascii=False, indent=2))
                row["input_token_upper_bound"] = [estimate_tokens(p) for p in captured]
                if judge_config and row["status"] in {"completed", "failed"}:
                    refs = {f["id"]: f for f in case["input"]["facts"]}
                    refs["observed"] = observed
                    payload = {"model": judge_config.llm.model, "temperature": 0,
                        "max_tokens": judge_config.memory.response_reserve_tokens,
                        "response_format": {"type": "json_object"}, "messages": [
                            {"role": "system", "content": RUBRIC["instruction"] + json.dumps(Verdict.model_json_schema())},
                            {"role": "user", "content": "DATA\n" + json.dumps({"case_id": case["id"],
                                "contract": case["expected"], "sources": refs,
                                "code_failures": row.get("failures", []), "code_status": row["status"]}, ensure_ascii=False)}]}
                    (folder / "judge-request.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2))
                    try:
                        raw = await judge(judge_config, judge_store, payload, client, identifier)
                        (folder / "judge-raw.txt").write_text(raw)
                        row["verdict"] = validate_verdict(raw, case["id"], refs)
                        row["judge_status"] = "completed"
                    except Exception as exc:
                        row.update(judge_status="grader_error", judge_error=type(exc).__name__)
                row["duration_seconds"] = time.monotonic() - started
                rows.append(row)
                (folder / "result.json").write_text(json.dumps(row, ensure_ascii=False, indent=2))
                (output / "progress.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2))
    def cost(store):
        from b2b_agent.action_policy import BudgetLedger
        return BudgetLedger(store.db).totals("campaign:default")
    criterion_counts = {}
    for row in rows:
        for criterion in row.get("verdict", {}).get("criteria", []):
            counts = criterion_counts.setdefault(criterion["name"], Counter())
            counts[str(criterion["score"]) if criterion["applicable"] else "not_applicable"] += 1
    stability = {}
    for row in rows:
        group = stability.setdefault(row["case_id"], {"passed": 0, "attempts": 0})
        group["attempts"] += 1
        group["passed"] += row["status"] == "completed"
    report = {"run_id": uuid.uuid4().hex, "commit": subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT)),
        "model": config.llm.model, "judge_model": judge_config.llm.model if judge_config else None,
        "config_sha256": hashlib.sha256(args.config.read_bytes()).hexdigest(),
        "rubric_version": RUBRIC["version"], "calibration_status": "not_run",
        "split": args.split, "planned": len(rows), "runs": rows,
        "agent_status_counts": dict(Counter(r["status"] for r in rows)),
        "judge_status_counts": dict(Counter(r["judge_status"] for r in rows)),
        "criterion_scores": criterion_counts, "code_stability": stability,
        "critical_violations": [{"run_id": r["run_id"], "violations": r["verdict"]["critical_violations"]}
                                for r in rows if r.get("verdict", {}).get("critical_violations")],
        "generation": {"temperature": 0, "max_tokens": config.memory.response_reserve_tokens},
        "policy": config.policy.model_dump(), "catalog_version": "not_applicable_research_facts_only",
        "rubric_sha256": hashlib.sha256((ROOT / "evaluation/rubrics/v1.json").read_bytes()).hexdigest(),
        "judge_config_sha256": hashlib.sha256(args.judge_config.read_bytes()).hexdigest() if args.judge_config else None,
        "agent_cost_reserved_or_actual": cost(agent_store),
        "judge_cost_reserved_or_actual": cost(judge_store),
        "release_acceptance": "blocked", "live_transport": "not_run",
        "gaps": ["Reply classification is deterministic; no production LLM intent/action adapter.",
                 "Judge calibration and human acceptance have not been completed.",
                 "Only research fixtures executed; not full coverage of 62 requirements."]}
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    agent_store.close()
    judge_store.close()
    return 0 if all(r["status"] == "completed" and r["judge_status"] != "grader_error"
                    and r.get("verdict", {"status": "pass"})["status"] == "pass" for r in rows) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-paid", action="store_true")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--judge-config", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--split", choices=["development", "holdout"], default="development")
    parser.add_argument("--repeats", type=int, choices=range(1, 4), default=3)
    args = parser.parse_args()
    if not args.allow_paid:
        parser.error("Paid model calls require --allow-paid; default offline run uses run_acceptance")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
