"""End-to-end pipeline test on simulated telemetry (no network, no LLM).

Every scenario runs through the real detector, telemetry tools, evidence capture, evidence picture, orchestrator,
guardrails, fix validation and report export, with the deterministic oracle policy in place of the model.
A failure here means the tools or the code around the model lost the answer, before any LLM is involved.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

import run_scenarios  # noqa: E402
from scenarios import SCENARIOS  # noqa: E402


@pytest.mark.parametrize("rename", [False, True], ids=["demo-names", "invented-names"])
@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.id for s in SCENARIOS])
def test_scenario_end_to_end(scenario, rename, tmp_path):
    r = run_scenarios.run_one(scenario, "oracle", tmp_path, provider="", model="", rename=rename)
    assert r["passed"], (r["checks"], r["status"], r["said"], r["limitations"])
    assert (Path(r["run_dir"]) / f"SIM-{scenario.id}_report.json").is_file()


def test_flag_wording_never_reaches_the_evidence(tmp_path):
    sc = next(s for s in SCENARIOS if s.id == "catalog-partial-errors")
    r = run_scenarios.run_one(sc, "oracle", tmp_path, provider="", model="", rename=False)
    assert r["checks"]["no_flag_leak"]
    text = "".join(p.read_text(encoding="utf-8") for p in Path(r["run_dir"]).glob("*_report.*"))
    assert "Feature Flag" not in text and "withheld by policy" in text
