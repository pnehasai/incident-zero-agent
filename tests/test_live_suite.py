"""The live suite's case table and summary maths (no demo needed)."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

import live_suite  # noqa: E402


def test_case_table_is_consistent():
    ids = [c.id for c in live_suite.CASES]
    assert len(ids) == len(set(ids))
    for c in live_suite.CASES:
        assert (c.kind == "none") == (not c.accept), c.id
        assert c.fix in ("none", "restart_service", "any"), c.id
        if c.kind == "stop":
            assert c.fix == "restart_service" and c.accept == (c.target,), c.id


def _row(case, accept, alerted, ok, conf, detect_s=90):
    checks = {"detection": alerted if accept else not alerted, "diagnosis": ok, "fix": ok}
    return {"run": "t", "case": case, "fault": "x", "accept": accept, "alerted": alerted, "start_quiet": True,
            "detect_s": detect_s if alerted else None, "investigate_s": 40, "status": "DIAGNOSED",
            "said": "a", "confidence": conf, "checks": checks, "passed": all(checks.values())}


def test_summary_counts_and_calibration(tmp_path, monkeypatch):
    monkeypatch.setattr(live_suite, "RESULTS_DIR", tmp_path)
    rows = [_row("S1", ["a"], True, True, "high", 80), _row("S2", ["b"], True, False, "high", 120),
            _row("F1", ["c"], True, True, "medium", 100), _row("N1", [], False, True, None)]
    d = tmp_path / "suite-t"
    d.mkdir()
    (d / "results.jsonl").write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    s = live_suite.summarize("t")
    assert "Passed 3 / 4." in s
    assert "Faults detected 3 / 3, median 100 s, max 120 s." in s
    assert "Correct service named 2 / 3" in s
    assert "| high | 2 | 1 | 50% |" in s
    assert "Confident (high) and wrong: S2." in s
    assert "No-fault runs without a false alarm 1 / 1." in s


def test_invalid_rows_are_listed_not_counted_and_a_valid_rerun_replaces_them(tmp_path, monkeypatch):
    monkeypatch.setattr(live_suite, "RESULTS_DIR", tmp_path)
    slept = {**_row("S3", ["a"], True, False, None, 596), "valid": False, "invalid_reason": "clock gap 12:24-13:19"}
    rows = [_row("S1", ["a"], True, True, "high"), slept, {**_row("N1", [], False, True, None), "valid": False,
                                                         "invalid_reason": "no baseline (warm-up)"}]
    d = tmp_path / "suite-t"
    d.mkdir()
    f = d / "results.jsonl"
    f.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    s = live_suite.summarize("t")
    assert "Passed 1 / 1." in s and "INVALID (clock gap 12:24-13:19)" in s
    assert "Not counted (invalid, re-run by resuming the same --run): N1, S3." in s
    f.write_text(f.read_text(encoding="utf-8") + "\n" + json.dumps({**_row("S3", ["a"], True, True, "high"), "valid": True}),
                 encoding="utf-8")
    s = live_suite.summarize("t")
    assert "Passed 2 / 2." in s and "resuming the same --run): N1." in s


def test_history_share_counts_served_services_with_a_baseline():
    t = {"a": {"served": 1.0, "history": 1.0}, "b": {"served": 1.0, "history_warmup": 1.0},
         "c": {"served": 1.0}, "loadgen": {"rate": 3.0}}
    assert abs(live_suite.history_share(t) - 2 / 3) < 1e-9
