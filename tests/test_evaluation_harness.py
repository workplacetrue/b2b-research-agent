import argparse
import json
from pathlib import Path

import httpx
import pytest

from tools.run_acceptance import summarize
from tools.run_model_eval import RUBRIC, run, validate_verdict


def verdict(**overrides):
    payload = {"case_id": "sample", "status": "pass", "criteria": [
        {"name": name, "applicable": True, "score": 2,
         "evidence_refs": ["observed"], "reason": "Supported by supplied observation"}
        for name in RUBRIC["criteria"]], "critical_violations": [], "needs_human_review": False}
    return {**payload, **overrides}


@pytest.mark.parametrize("change", ["unknown_ref", "duplicate", "critical_pass", "wrong_case", "invalid_json"])
def test_judge_rejects_invalid_or_contradictory_results(change):
    payload = verdict()
    if change == "unknown_ref":
        payload["criteria"][0]["evidence_refs"] = ["fabricated"]
    elif change == "duplicate":
        payload["criteria"].append(payload["criteria"][0])
    elif change == "critical_pass":
        payload["critical_violations"] = ["invented_guarantee"]
    elif change == "wrong_case":
        payload["case_id"] = "another"
    raw = "bad-json" if change == "invalid_json" else json.dumps(payload)
    with pytest.raises(ValueError):
        validate_verdict(raw, "sample", ["observed"])


def test_valid_judge_evidence_and_na():
    payload = verdict()
    payload["criteria"][0].update(applicable=False, score=None, evidence_refs=[])
    assert validate_verdict(json.dumps(payload), "sample", ["observed"])["status"] == "pass"


def test_missing_and_failed_tests_are_not_green(tmp_path):
    junit = tmp_path / "junit.xml"
    junit.write_text('<testsuites><testsuite><testcase classname="tests.test_x" name="test_y[a]"/>'
        '<testcase classname="tests.test_x" name="test_y[b]"><failure/></testcase>'
        '</testsuite></testsuites>')
    manifest = [{"id": "x", "tests": ["tests/test_x.py::test_y"], "semantic": "needed"},
                {"id": "y", "tests": ["tests/test_missing.py::test_x"], "semantic": "—"}]
    rows, counts = summarize(manifest, junit)
    assert rows[0]["offline_status"] == "failed"
    assert rows[1]["offline_status"] == "harness_error"
    assert rows[0]["semantic_status"] == "not_run"
    assert counts == {"completed": 1, "failed": 1}


@pytest.mark.parametrize("judge_valid", [True, False])
async def test_model_runner_real_runtime_with_fake_provider(tmp_path, monkeypatch, judge_valid):
    monkeypatch.setenv("TEST_EVAL_KEY", "synthetic-key")
    config = {"llm": {"enabled": True, "model": "agent-test", "base_url": "https://llm.example/v1",
                      "api_key_env": "TEST_EVAL_KEY"},
              "budget": {"max_campaign_cost": 1, "max_run_cost": 1,
                         "tool_prices": {"llm": {"per_call": 0.01, "currency": "USD", "source": "fixture"}}},
              "memory": {"input_tokens": 50000, "context_window": 100000}}
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config))
    config["llm"]["model"] = "judge-test"
    judge_path = tmp_path / "judge.json"
    judge_path.write_text(json.dumps(config))
    requests = []

    def provider(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if payload["model"] == "agent-test":
            assert "expected" not in request.content.decode()
            assert "min_hypotheses" not in request.content.decode()
            content = json.dumps({"hypotheses": [{"workflow": "documents", "statement": "Проверить подготовку документов",
                "evidence_ids": ["f-docs"], "validation_question": "Актуальна ли помощь?"}]})
        else:
            assert "contract" in payload["messages"][1]["content"]
            content = json.dumps(verdict(case_id="documents_holdout")) if judge_valid else "invalid-json"
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(
        **kwargs, transport=httpx.MockTransport(provider)))
    output = tmp_path / "run"
    code = await run(argparse.Namespace(config=config_path, judge_config=judge_path,
        output=output, split="holdout", repeats=1))
    report = json.loads((output / "report.json").read_text())
    assert report["runs"][0]["status"] == "completed"
    assert report["runs"][0]["judge_status"] == ("completed" if judge_valid else "grader_error")
    assert code == (0 if judge_valid else 1)
    assert report["agent_cost_reserved_or_actual"] == {"USD": 0.01}
    assert report["judge_cost_reserved_or_actual"] == {"USD": 0.01}
    assert report["release_acceptance"] == "blocked"
    assert len(requests) == 2
    assert "synthetic-key" not in (output / "documents_holdout-1/agent-request.json").read_text()


def test_all_62_scenarios_have_contracts_and_evidence():
    root = Path(__file__).parents[1]
    manifest = json.loads((root / "evaluation/scenarios.json").read_text())
    assert {r["id"] for r in manifest} == {f"TZ-18.{i:02}" for i in range(1, 63)}
    assert all(r["contract"] and r["tests"] and r["coverage"] == "partial" for r in manifest)
