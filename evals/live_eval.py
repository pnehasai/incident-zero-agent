"""Live end-to-end evaluation on the running demo: detect -> investigate -> report -> score.

    python evals/live_eval.py --case LIVE-1 --expect payment [--fix none|restart_service|any] [--wait-min 10]
    python evals/live_eval.py --case LIVE-0 --expect none        # no fault injected: expects no alert

1. Start this, then inject ONE fault the way the mentors do (e.g. the demo's flag UI). This script never touches
   or reads flags; the expected answer is given by the operator and only used AFTER the report is written.
2. It polls the detector (same thresholds as `cli.py watch`) until an alert holds for 2 checks, or --wait-min.
3. It runs the normal investigation (cli.py investigate) with the alert text, then scores the report:
   detection, component, fix rule, time to detect, time to diagnose.
4. Each result is appended to evals/results/live_results.jsonl; --summary prints the table of all live runs.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

AGENT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT))
os.chdir(AGENT)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(AGENT / ".env")

import cli  # noqa: E402
import detector  # noqa: E402
import preflight  # noqa: E402

RESULTS = AGENT / "evals" / "results" / "live_results.jsonl"


def wait_for_alert(wait_s: float, interval_s: float) -> tuple[list, float]:
    det, t0 = detector.Detector(), time.monotonic()
    while time.monotonic() - t0 < wait_s:
        try:
            found = detector.breaches(detector.measure())
        except Exception as exc:  # noqa: BLE001 - keep polling through a Prometheus hiccup
            print(f"  metrics unavailable: {type(exc).__name__}")
            found = []
        ready = det.check(found)
        print(f"  [{datetime.now():%H:%M:%S}] over threshold: {[b.service for b in found] or 'nothing'}", flush=True)
        if ready:
            return ready, time.monotonic() - t0
        time.sleep(interval_s)
    return [], time.monotonic() - t0


def score(report: dict, expect: str | None, fix: str, alerted: bool) -> dict:
    d = report.get("diagnosis") or {}
    comp = (d.get("suspected_component") or "").lower()
    proposals = report.get("proposals") or []
    if expect is None:
        diag_ok = report.get("status") == "INCONCLUSIVE" or d.get("confidence_label") == "low"
        return {"detection": not alerted, "diagnosis": diag_ok, "fix": not proposals}
    named = bool(re.search(rf"(?<![\w-]){re.escape(expect.lower())}(?![\w-])", comp))
    if fix == "none":
        fix_ok = not proposals
    elif fix == "restart_service":
        fix_ok = any(p["runbook_id"] == "restart_service" and p["target_service"] == expect for p in proposals)
    else:
        fix_ok = all(p["target_service"] == expect for p in proposals)
    return {"detection": alerted, "diagnosis": report.get("status") == "DIAGNOSED" and named, "fix": fix_ok}


def summary() -> int:
    if not RESULTS.is_file():
        print("no live results yet")
        return 0
    rows = [json.loads(x) for x in RESULTS.read_text(encoding="utf-8").splitlines() if x.strip()]
    print("| Case | Expected | Said | Status | Detect s | Investigate s | Detection | Diagnosis | Fix |")
    print("|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        c = r["checks"]
        print(f"| {r['case']} | {r['expected']} | {r['said']} | {r['status']} | {r['detect_s']} | {r['investigate_s']} | "
              f"{'yes' if c['detection'] else 'NO'} | {'yes' if c['diagnosis'] else 'NO'} | {'yes' if c['fix'] else 'NO'} |")
    ok = sum(all(r["checks"].values()) for r in rows)
    print(f"\npassed {ok} / {len(rows)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--case")
    p.add_argument("--expect", help="service the diagnosis must name, or 'none' for no incident")
    p.add_argument("--fix", default="any", choices=["none", "restart_service", "any"])
    p.add_argument("--wait-min", type=float, default=10)
    p.add_argument("--interval-s", type=float, default=15)
    p.add_argument("--summary", action="store_true")
    a = p.parse_args(argv)
    if a.summary:
        return summary()
    if not a.case or not a.expect:
        p.error("--case and --expect are required (or use --summary)")
    expect = None if a.expect.lower() == "none" else a.expect
    print(preflight.resolve_opensearch())
    print(f"{a.case}: waiting up to {a.wait_min:.0f} min for a threshold alert. Inject the fault now.")
    ready, detect_s = wait_for_alert(a.wait_min * 60, a.interval_s)
    alerted = bool(ready)
    if alerted:
        symptom = detector.alert_text(ready)
        print(f"ALERT after {detect_s:.0f}s: {symptom}")
    else:
        symptom = "Users report errors and slow pages in the shop."
        print(f"no alert within {a.wait_min:.0f} min; investigating a manual report instead")
    t0 = time.monotonic()
    cli.main(["investigate", "--incident-id", a.case, "--symptom", symptom,
              "--source", "threshold-alert" if alerted else "manual-cli"])
    investigate_s = time.monotonic() - t0
    path = AGENT / "runs" / a.case / f"{a.case}_report.json"
    report = json.loads(path.read_text(encoding="utf-8"))
    checks = score(report, expect, a.fix, alerted)
    d = report.get("diagnosis") or {}
    row = {"case": a.case, "at": datetime.now().isoformat(timespec="seconds"), "expected": a.expect, "fix_rule": a.fix,
           "said": d.get("suspected_component"), "confidence": d.get("confidence_label"), "status": report["status"],
           "proposals": [p["runbook_id"] + ":" + p["target_service"] for p in report.get("proposals") or []],
           "symptom": symptom, "detect_s": round(detect_s) if alerted else None, "investigate_s": round(investigate_s),
           "checks": checks, "report": str(path)}
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    with RESULTS.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")
    print(f"\n{a.case}: {'PASS' if all(checks.values()) else 'FAIL'} {checks}  said={row['said']!r} "
          f"confidence={row['confidence']} proposals={row['proposals']}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
