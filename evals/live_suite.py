"""Live evaluation suite: inject each fault on the running demo, let the agent detect and diagnose it, score it.

    python evals/live_suite.py --list
    python evals/live_suite.py --only S1 S2 N1                 # container stops + no-fault: fully automatic
    python evals/live_suite.py --flags operator                # flag cases: you flip each flag in the flag UI
    python evals/live_suite.py --flags file                    # flag cases: the suite edits flagd's JSON file
    python evals/live_suite.py --summary [--run RUN_ID]

The expected answers below were written down before any run, from what each fault changes in the demo source
(src/*). They are used only after the agent's report is written; the agent never sees them and never reads flags.

Per case: wait for the shop to be quiet -> inject -> poll the detector (same rules as `cli.py watch`) -> investigate
-> clear the fault -> score. Diagnosis only: no approval or restart (the full chain is checked separately, see
evals/results/fullchain-run5). Results: evals/results/suite-<run>/results.jsonl and summary.md. Re-running with the
same --run skips cases already scored, so a long run can be resumed.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import live_eval  # noqa: E402  (sets cwd, loads .env, imports the agent)
from live_eval import AGENT, cli, detector, preflight  # noqa: E402


@dataclass(frozen=True)
class Case:
    id: str
    kind: str           # "stop" (docker stop <target>), "flag" (<target>=<variant>), "none"
    target: str
    variant: str
    accept: tuple       # services a correct diagnosis may name; () = no incident
    fix: str            # live_eval fix rule: "restart_service", "none" (a restart would not fix it), "any"
    note: str = ""
    needs: str = ""     # container that must exist, else the case is skipped (not scored)
    reset: str = ""     # container to restart after the flag is off (a leak stays after its flag is off)


CASES = [
    Case("S1", "stop", "recommendation", "", ("recommendation",), "restart_service", "crashed service"),
    Case("F1", "flag", "paymentFailure", "50%", ("payment",), "none", "payment rejects half the charges"),
    Case("S2", "stop", "cart", "", ("cart",), "restart_service"),
    Case("F2", "flag", "adHighCpu", "on", ("ad",), "any", "CPU burn in ad"),
    Case("N1", "none", "", "", (), "none", "no fault: expects no alert"),
    Case("S3", "stop", "product-catalog", "", ("product-catalog",), "restart_service"),
    Case("F3", "flag", "cartFailure", "50%", ("cart",), "none"),
    Case("F4", "flag", "paymentUnreachable", "on", ("checkout", "payment"), "none",
         "checkout dials a bad payment address; restarting payment would not help"),
    Case("S4", "stop", "payment", "", ("payment",), "restart_service"),
    Case("F5", "flag", "productCatalogFailure", "on", ("product-catalog",), "none",
         "one product id fails; 22 Nov: no failed call recorded anywhere, so likely invisible here"),
    Case("F6", "flag", "recommendationCacheFailure", "on", ("recommendation",), "any", "cache leak, slow",
         reset="recommendation"),
    Case("F7", "flag", "adFailure", "on", ("ad",), "none"),
    Case("S5", "stop", "currency", "", ("currency",), "restart_service"),
    Case("F8", "flag", "emailMemoryLeak", "100x", ("email",), "any", "memory grows in email",
         reset="email"),
    Case("F9", "flag", "productCatalogLockContention", "on", ("product-catalog",), "any", "DB lock waits"),
    Case("F10", "flag", "kafkaQueueProblems", "3sec", ("fraud-detection", "checkout", "kafka"), "none",
         "checkout floods the queue, fraud-detection consumes slowly", "fraud-detection"),
    Case("F11", "flag", "adManualGc", "on", ("ad",), "any", "long GC pauses in ad"),
    Case("F12", "flag", "intlShippingSlowdown", "5sec", ("shipping",), "none", "only international orders"),
    Case("F13", "flag", "imageSlowLoad", "5sec", ("frontend-proxy", "image-provider"), "none",
         "envoy delays image requests at frontend-proxy; browser traffic only"),
    Case("F14", "flag", "kafkaConsumerDead", "on", ("fraud-detection",), "none",
         "consumer leaves the group: no errors anywhere, expected blind spot", "fraud-detection"),
    Case("F15", "flag", "loadGeneratorFloodHomepage", "on", ("frontend", "frontend-proxy", "load-generator"), "none",
         "traffic flood, not a broken service"),
    Case("N2", "none", "", "", (), "none", "no fault, at the end of the run"),
]
BY_ID = {c.id: c for c in CASES}
RESULTS_DIR = AGENT / "evals" / "results"


# ---------- fault injection (the operator side; nothing here is visible to the agent) ----------

def docker(*args: str) -> str:
    out = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=120)
    if out.returncode:
        raise RuntimeError(f"docker {' '.join(args)}: {out.stderr.strip()}")
    return out.stdout.strip()


def flag_file() -> Path:
    """The flagd JSON of the RUNNING demo (it may be another checkout than this one)."""
    src = docker("inspect", "flagd", "--format", '{{range .Mounts}}{{if eq .Destination "/etc/flagd"}}{{.Source}}{{end}}{{end}}')
    path = Path(src) / "demo.flagd.json"
    if not path.is_file():
        raise RuntimeError(f"flagd file not found at {path}")
    return path


class Injector:
    def __init__(self, flags_mode: str):
        self.flags_mode = flags_mode
        self.original: str | None = None

    def set_flag(self, name: str, variant: str) -> None:
        if self.flags_mode == "operator":
            input(f"\n>>> In the flag UI set {name} = {variant}, save, then press Enter ")
            return
        path = flag_file()
        if self.original is None:
            self.original = path.read_text(encoding="utf-8")
        data = json.loads(path.read_text(encoding="utf-8"))
        if variant not in data["flags"][name]["variants"]:
            raise RuntimeError(f"{name} has no variant {variant!r}")
        data["flags"][name]["defaultVariant"] = variant
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def inject(self, c: Case) -> None:
        if c.kind == "stop":
            docker("stop", c.target)
        elif c.kind == "flag":
            self.set_flag(c.target, c.variant)

    def clear(self, c: Case) -> None:
        if c.kind == "stop":
            docker("start", c.target)
        elif c.kind == "flag":
            if self.flags_mode == "operator":
                input(f"\n>>> Set {c.target} back to off, save, then press Enter ")
            else:
                self.set_flag(c.target, "off")
            if c.reset:
                docker("restart", c.reset)

    def restore(self) -> None:
        if self.original is not None:
            flag_file().write_text(self.original, encoding="utf-8")


# ---------- one case ----------

GAP_S = 120  # a check that comes this much later than planned: the laptop slept (24 Nov: 55 min, no data at all)
GAPS: list[str] = []  # clock gaps seen during the current case


def poll(seconds: float, interval_s: float, stop_when_ready: bool, quiet_needed: int = 0):
    """Run the detector. Returns (ready breaches, seconds) or, with quiet_needed, whether the shop went quiet."""
    det, t0, quiet = detector.Detector(), time.monotonic(), 0
    last = time.time()
    while time.monotonic() - t0 < seconds:
        now = time.time()
        if now - last > interval_s + GAP_S:
            GAPS.append(f"{datetime.fromtimestamp(last):%H:%M}-{datetime.fromtimestamp(now):%H:%M}")
            print(f"  clock gap {GAPS[-1]} (sleep?): this case will be marked invalid", flush=True)
        last = now
        try:
            found = detector.breaches(detector.measure())
        except Exception as exc:  # noqa: BLE001 - keep polling through a Prometheus hiccup
            print(f"  metrics unavailable: {type(exc).__name__}", flush=True)
            found = None
        ready = det.check(found or [])
        print(f"  [{datetime.now():%H:%M:%S}] over threshold: {[b.service for b in found or []] or 'nothing'}", flush=True)
        if quiet_needed:
            quiet = quiet + 1 if found == [] else 0
            if quiet >= quiet_needed:
                return True, time.monotonic() - t0
        elif stop_when_ready and ready:
            return ready, time.monotonic() - t0
        time.sleep(interval_s)
    if time.time() - last > interval_s + GAP_S:  # the sleep can also end the loop (24 Nov: N1 "watched" 2 min)
        GAPS.append(f"{datetime.fromtimestamp(last):%H:%M}-{datetime.now():%H:%M}")
    return (False if quiet_needed else []), time.monotonic() - t0


def history_share(table: dict) -> float:
    """Share of served services the detector has a baseline for (after a sleep or a restart it has none)."""
    served = [m for m in table.values() if m.get("served")]
    have = [m for m in served if "history" in m or "history_warmup" in m]
    return len(have) / len(served) if served else 0.0


def wait_for_history(max_min: float, interval_s: float, need: float = 0.8) -> bool:
    t0 = time.monotonic()
    while True:
        try:
            share = history_share(detector.measure())
        except Exception:  # noqa: BLE001
            share = 0.0
        if share >= need:
            return True
        if time.monotonic() - t0 > max_min * 60:
            print(f"  baseline still missing for {1 - share:.0%} of services after {max_min:.0f} min", flush=True)
            return False
        print(f"  baseline for {share:.0%} of services: waiting (warm-up)", flush=True)
        time.sleep(interval_s)


def run_case(c: Case, run_id: str, inj: Injector, a) -> dict:
    print(f"\n===== {c.id}: {c.kind} {c.target} {c.variant}  ({c.note})", flush=True)
    GAPS.clear()
    warm = wait_for_history(a.warm_max_min, a.interval_s)
    quiet, waited = poll(a.quiet_max_min * 60, a.interval_s, False, quiet_needed=4)
    print(f"  shop {'quiet' if quiet else 'NOT quiet'} after {waited:.0f} s", flush=True)
    inj.inject(c)
    injected_at = datetime.now()
    try:
        ready, detect_s = poll(a.detect_min * 60, a.interval_s, True)
        alerted = bool(ready)
        symptom = detector.alert_text(ready) if alerted else "Users report errors and slow pages in the shop."
        print(f"  {'ALERT after %.0f s: %s' % (detect_s, symptom) if alerted else 'no alert'}", flush=True)
        inc = f"{run_id}-{c.id}"
        t0 = time.monotonic()
        if alerted or c.kind != "none":
            cli.main(["investigate", "--incident-id", inc, "--symptom", symptom,
                      "--source", "threshold-alert" if alerted else "manual-cli"])
        investigate_s = time.monotonic() - t0
    finally:
        inj.clear(c)
    path = AGENT / "runs" / inc / f"{inc}_report.json"
    report = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {"status": "NOT_RUN"}
    d = report.get("diagnosis") or {}
    said = (d.get("suspected_component") or "").lower()
    if c.accept:
        checks = live_eval.score(report, c.accept[0], c.fix, alerted)
        named = next((s for s in c.accept if re.search(rf"(?<![\w-]){re.escape(s)}(?![\w-])", said)), None)
        checks["diagnosis"] = report.get("status") == "DIAGNOSED" and named is not None
        if c.fix != "restart_service" and named:   # target rule against whichever accepted service was named
            checks["fix"] = live_eval.score(report, named, c.fix, alerted)["fix"]
        elif c.fix == "restart_service":
            checks["fix"] = any(p["runbook_id"] == "restart_service" and p["target_service"] in c.accept
                                for p in report.get("proposals") or [])
    else:  # no fault: pass only if nothing alerted (the manual-report investigation is skipped)
        checks = {"detection": not alerted, "diagnosis": not alerted, "fix": not alerted}
    why = ([f"clock gap {', '.join(GAPS)}"] if GAPS else []) + ([] if warm else ["no baseline (warm-up)"])
    return {"valid": not why, "invalid_reason": "; ".join(why), "run": run_id, "case": c.id, "kind": c.kind, "fault": f"{c.target} {c.variant}".strip() or "none",
            "accept": list(c.accept), "fix_rule": c.fix, "injected_at": injected_at.isoformat(timespec="seconds"),
            "start_quiet": quiet, "alerted": alerted, "symptom": symptom,
            "detect_s": round(detect_s) if alerted else None, "investigate_s": round(investigate_s),
            "status": report.get("status"), "said": d.get("suspected_component"), "confidence": d.get("confidence_label"),
            "proposals": [p["runbook_id"] + ":" + p["target_service"] for p in report.get("proposals") or []],
            "checks": checks, "passed": all(checks.values()), "report": str(path)}


# ---------- summary ----------

def summarize(run_id: str) -> str:
    path = RESULTS_DIR / f"suite-{run_id}" / "results.jsonl"
    every = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()] if path.is_file() else []
    order = {c.id: i for i, c in enumerate(CASES)}
    latest: dict[str, dict] = {}
    for r in every:  # a valid re-run replaces an invalid row; an invalid row never replaces a valid one
        if r["case"] not in latest or r.get("valid", True) or not latest[r["case"]].get("valid", True):
            latest[r["case"]] = r
    shown = sorted(latest.values(), key=lambda r: order.get(r["case"], 99))
    rows = [r for r in shown if r.get("valid", True)]
    yn = lambda v: "yes" if v else "**no**"  # noqa: E731
    out = [f"# Live suite {run_id}", "",
           "| Case | Fault | Expected | Said (confidence) | Detect s | Investigate s | Detected | Diagnosis | Fix | Pass |",
           "|---|---|---|---|---|---|---|---|---|---|"]
    for r in shown:
        c = r["checks"]
        verdict = ("PASS" if r["passed"] else "FAIL") if r.get("valid", True) else f"INVALID ({r['invalid_reason']})"
        out.append(f"| {r['case']} | {r['fault']} | {' / '.join(r['accept']) or 'no alert'} | "
                   f"{r['said'] or '-'} ({r['confidence'] or '-'}) | {r['detect_s'] if r['detect_s'] is not None else '-'} | "
                   f"{r['investigate_s']} | {yn(c['detection'])} | {yn(c['diagnosis'])} | {yn(c['fix'])} | "
                   f"{verdict}{'' if r['start_quiet'] else ' (start not quiet)'} |")
    bad = [r["case"] for r in shown if not r.get("valid", True)]
    if bad:
        out += ["", f"Not counted (invalid, re-run by resuming the same --run): {', '.join(bad)}."]
    faults = [r for r in rows if r["accept"]]
    det = [r for r in faults if r["alerted"]]
    times = sorted(r["detect_s"] for r in det)
    out += ["", f"Passed {sum(r['passed'] for r in rows)} / {len(rows)}.",
            f"Faults detected {len(det)} / {len(faults)}"
            + (f", median {times[len(times) // 2]} s, max {times[-1]} s" if times else "") + ".",
            f"Correct service named {sum(r['checks']['diagnosis'] for r in faults)} / {len(faults)}; "
            f"correct fix decision {sum(r['checks']['fix'] for r in faults)} / {len(faults)}.",
            f"No-fault runs without a false alarm {sum(r['passed'] for r in rows if not r['accept'])} / "
            f"{sum(1 for r in rows if not r['accept'])}.", "",
            "## Confidence vs correctness (faults that were investigated)", "",
            "| Confidence | Runs | Correct service | Share correct |", "|---|---|---|---|"]
    for label in ("high", "medium", "low", None):
        grp = [r for r in faults if r["confidence"] == label and r["status"] != "NOT_RUN"]
        if grp:
            ok = sum(r["checks"]["diagnosis"] for r in grp)
            out.append(f"| {label or 'none'} | {len(grp)} | {ok} | {ok / len(grp):.0%} |")
    wrong = [r["case"] for r in faults if r["confidence"] == "high" and not r["checks"]["diagnosis"]]
    out += ["", f"Confident (high) and wrong: {', '.join(wrong) or 'none'}."]
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--run", default=datetime.now().strftime("%Y%m%d"), help="run id; reuse it to resume")
    p.add_argument("--only", nargs="+", help="case ids to run (default: all)")
    p.add_argument("--flags", choices=["skip", "operator", "file"], default="skip",
                   help="how flag cases are injected (default: skip them)")
    p.add_argument("--detect-min", type=float, default=10, help="the per-operation rule uses a 10-min window")
    p.add_argument("--quiet-max-min", type=float, default=8, help="wait at most this long for a quiet shop first")
    p.add_argument("--warm-max-min", type=float, default=15, help="wait at most this long for the detector's baseline")
    p.add_argument("--interval-s", type=float, default=15)
    p.add_argument("--list", action="store_true")
    p.add_argument("--summary", action="store_true")
    a = p.parse_args(argv)
    if a.list:
        for c in CASES:
            print(f"{c.id:4} {c.kind:5} {(c.target + ' ' + c.variant).strip():38} expect {'/'.join(c.accept) or 'no alert':40} fix {c.fix}")
        return 0
    if a.summary:
        print(summarize(a.run))
        return 0
    cases = [BY_ID[i] for i in a.only] if a.only else CASES
    if a.flags == "skip":
        cases = [c for c in cases if c.kind != "flag"]
    out_dir = RESULTS_DIR / f"suite-{a.run}"
    out_dir.mkdir(parents=True, exist_ok=True)
    results = out_dir / "results.jsonl"
    rows = [json.loads(x) for x in results.read_text(encoding="utf-8").splitlines() if x.strip()] \
        if results.is_file() else []
    done = {r["case"] for r in rows if r.get("valid", True)}  # invalid cases run again
    print(preflight.resolve_opensearch())
    inj = Injector(a.flags)
    try:
        for c in cases:
            if c.needs and subprocess.run(["docker", "inspect", c.needs], capture_output=True).returncode:
                print(f"{c.id}: {c.needs} is not deployed in this demo, skipping (not scored)")
                continue
            if c.id in done:
                print(f"{c.id}: already scored in run {a.run}, skipping")
                continue
            row = run_case(c, a.run, inj, a)
            with results.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
            print(f"\n{c.id}: {'PASS' if row['passed'] else 'FAIL'}{'' if row['valid'] else ' INVALID: ' + row['invalid_reason']} {row['checks']} said={row['said']!r} "
                  f"confidence={row['confidence']} proposals={row['proposals']}", flush=True)
            (out_dir / "summary.md").write_text(summarize(a.run), encoding="utf-8")
    finally:
        inj.restore()
    print("\n" + summarize(a.run))
    return 0


if __name__ == "__main__":
    sys.exit(main())
