"""End-to-end evaluation on simulated telemetry: detector -> investigation -> guardrails -> fix proposal -> report,
scored against each scenario's ground truth.

    python evals/run_scenarios.py                         # LLM from .env (LLM_PROVIDER / LLM_MODEL)
    python evals/run_scenarios.py --mode oracle           # no LLM: deterministic tool-following baseline
    python evals/run_scenarios.py --only crash-loop cpu-hog --repeat 3 --rename

Modes
  llm     the real agent (system prompt + model + orchestrator). Measures the whole agent.
  oracle  a fixed policy instead of the model: fleet tools, traces/logs/health of the leading candidates, then
          finish with the top candidate of the evidence picture. Measures whether the TOOLS and the evidence
          picture carry the answer, so a miss in llm mode can be blamed on the right layer.
--rename gives every service an invented name (svc-aa, svc-ab, ...): shows that nothing depends on known names.

Writes evals/results/<stamp>-<mode>/: per-scenario report + transcript, results.json, summary.md.
The agent never sees `expected`; only telemetry goes through the tools.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

AGENT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(AGENT / ".env")

import candidates  # noqa: E402
import detector  # noqa: E402
import resources  # noqa: E402
import runbooks  # noqa: E402
import telemetry  # noqa: E402
from contracts import CallToolDecision, FinishDecision, InconclusiveDecision, ToolCall  # noqa: E402
from integration import EvidenceRegistry, make_telemetry_runner  # noqa: E402
from models import ConfidenceLabel, Diagnosis, Incident, TimeWindow  # noqa: E402
from orchestrator import RECOVERY_NOT_PROVIDED, Dependencies, InvestigationConfig, investigate  # noqa: E402
from report_io import write_report  # noqa: E402
from scenarios import BY_ID, SCENARIOS, renaming  # noqa: E402
from sim_world import SimWorld, install  # noqa: E402

GENERIC_SYMPTOM = "Users report errors and slow pages in the shop."


# ------------------------------------------------------------------ deterministic baseline "reasoner"
class OracleReasoner:
    """Fixed tool-following policy over the evidence picture (no LLM). Same interface as LLMReasoner."""

    def __init__(self) -> None:
        self.step, self.n = 0, 0

    def _calls(self, *specs: tuple[str, dict]) -> CallToolDecision:
        calls = []
        for name, args in specs:
            self.n += 1
            calls.append(ToolCall(call_id=f"oracle_{self.n}", tool_name=name, arguments=args))
        return CallToolDecision(calls=calls, rationale="oracle policy")

    def next_step(self, ctx):
        known = set(ctx.services)
        self.step += 1
        if self.step == 1:
            return self._calls(("overview", {}), ("resource_overview", {}), ("log_overview", {}))
        ranked = candidates.rank(ctx.evidence, known)
        if ctx.rejection and ctx.allow_tools:  # e.g. "follow up the flagged containers": do exactly that
            named = [s for s in known if re.search(rf"(?<![\w-]){re.escape(s)}(?![\w-])", ctx.rejection)]
            if named:
                return self._calls(*[("service_health", {"service": s}) for s in named[: ctx.remaining_tool_calls]])
        if self.step == 2 and ctx.allow_tools:
            alerting = [s for s in known if re.search(rf"(?<![\w-]){re.escape(s)}(?![\w-])", ctx.incident.symptom)]
            targets = list(dict.fromkeys(alerting[:2] + [c.service for c in ranked[:2]]))
            specs = [("search_traces", {"service": s}) for s in targets]
            if ranked:
                specs += [("query_logs", {"service": ranked[0].service}), ("service_health", {"service": ranked[0].service})]
            return self._calls(*specs[: ctx.remaining_tool_calls])
        if not ranked or ranked[0].tier <= 1:
            return InconclusiveDecision(reason="no candidate with strong evidence",
                                        relevant_evidence_ids=[e.evidence_id for e in ctx.evidence][:5])
        top = ranked[0]
        ok = {e.evidence_id for e in ctx.evidence if e.status.value == "ok"}
        cited = sorted(top.evidence_ids & ok)
        conf = ConfidenceLabel.HIGH if top.tier >= 3 and len(top.sources) >= 2 else ConfidenceLabel.MEDIUM
        alts = [f"also active: {c.service} - {'; '.join((c.direct + c.current)[:1])}" for c in ranked[1:3] if c.tier >= top.tier]
        fault = runbooks.process_fault(ctx.evidence, top.service, known)
        diag = Diagnosis(suspected_component=top.service, suspected_cause="; ".join((top.direct + top.current)[:3]),
                         supporting_evidence_ids=cited, alternatives=alts, uncertainty="cause inside the service unknown",
                         confidence_label=conf, next_step=f"inspect {top.service}")
        fix = ({"operation": "restart_service", "target_service": top.service, "reason": f"process-level fault: {fault}"}
               if fault else {"operation": "none"})
        return FinishDecision(diagnosis=diag, fix=fix)


# ------------------------------------------------------------------ scoring
_FLAG = telemetry._FLAG_PATTERN


def _names(component: str, services: set[str]) -> list[str]:
    text = component.lower()
    return sorted(s for s in services if re.search(rf"(?<![\w-]){re.escape(s.lower())}(?![\w-])", text))


def telemetry_texts(transcript: Path | None) -> list[str]:
    """What the model received from outside: tool results and the per-round user messages (incident, evidence
    picture). The system prompt is excluded: it names flags only to forbid them."""
    if not transcript or not transcript.is_file():
        return []
    msgs = json.loads(transcript.read_text(encoding="utf-8")).get("messages", [])
    return [str(m.get("content")) for m in msgs if m.get("role") in ("tool", "human")]


def score(sc, rep, world: SimWorld, alerted: bool, transcript: Path | None) -> dict:
    exp = sc.expected
    want = world.n(exp["component"]) if exp.get("component") else None
    services = {world.n(s) for s in set(world.services) | set(world.containers)}
    d = rep.diagnosis
    named = _names(d.suspected_component, services) if d else []
    out = {"scenario": sc.id, "status": rep.status.value, "alert": alerted,
           "said": d.suspected_component if d else None, "confidence": d.confidence_label.value if d else None,
           "fix": rep.proposals[0].runbook_id if rep.proposals else "none",
           "model_calls": rep.timeline[-1].metadata.get("model_calls"),
           "tool_calls": rep.timeline[-1].metadata.get("tool_calls"),
           "seconds": rep.timeline[-1].metadata.get("elapsed_s")}
    checks = {"detection": alerted == exp["alert"]}
    if want is None:
        accept = exp.get("accept", ["INCONCLUSIVE"])
        checks["diagnosis"] = rep.status.value in accept or (d is not None and d.confidence_label.value in accept)
    else:
        checks["diagnosis"] = d is not None and named == [want]
    fix = exp.get("fix", "any")
    if fix == "restart_service" and want not in runbooks.operations()["restart_service"]["allowed_targets"]:
        fix = "any"  # renamed world: the Action API allowlist names real services only, so no restart can be proposed
    if fix == "none":
        checks["fix"] = not rep.proposals
    elif fix == "restart_service":
        checks["fix"] = any(p.runbook_id == "restart_service" and p.target_service == want for p in rep.proposals)
    else:
        checks["fix"] = all(p.target_service == want for p in rep.proposals)
    texts = [json.dumps(e.observed_value, default=str) for e in rep.evidence]
    texts += telemetry_texts(transcript)
    checks["no_flag_leak"] = not any(_FLAG.search(t) for t in texts)
    out["confident_wrong"] = bool(want and d and named != [want] and d.confidence_label == ConfidenceLabel.HIGH)
    out["checks"] = checks
    out["passed"] = all(checks.values())
    return out


# ------------------------------------------------------------------ one run
def run_one(sc, mode: str, out: Path, provider: str, model: str, rename: bool, tag: str = "") -> dict:
    world = SimWorld(sc, renaming(sc) if rename else None)
    uninstall = install(world, telemetry, resources)
    try:
        det = detector.Detector()
        found = detector.breaches(detector.measure())
        det.check(found)
        ready = det.check(found)  # the detector needs two consecutive checks
        alerted = bool(ready)
        symptom = detector.alert_text(ready) if ready else (world.text(sc.manual_symptom) or GENERIC_SYMPTOM)
        inc_id = f"SIM-{sc.id}{tag}"
        run_dir = out / inc_id
        now = datetime.now(timezone.utc)
        incident = Incident(id=inc_id, source="threshold-alert" if alerted else "manual", symptom=symptom,
                            detection_time=now, observation_window=TimeWindow(start_time=now - timedelta(minutes=3), end_time=now),
                            recovery_condition=RECOVERY_NOT_PROVIDED)
        run_tool, policy, _ = make_telemetry_runner(run_dir / "evidence")
        transcript = run_dir / "llm_transcript.json"
        if mode == "oracle":
            reasoner = OracleReasoner()
        else:
            from reasoning import LLMReasoner
            reasoner = LLMReasoner(model, provider=provider, request_timeout_s=20, transcript_path=transcript)
        deps = Dependencies(reasoner=reasoner, run_tool=run_tool, policy=policy, registry=EvidenceRegistry())
        rep = investigate(incident, InvestigationConfig(), deps)
        rep, _art = write_report(rep, run_dir)
        result = score(sc, rep, world, alerted, transcript if mode == "llm" else None)
        result.update(symptom=symptom, run_dir=str(run_dir),
                      limitations=[x for x in rep.limitations if not x.startswith(("Telemetry tools are live-only",
                                                                                     "Confidence labels", "No recovery"))])
        return result
    finally:
        uninstall()


def summarise(results: list[dict], mode: str, model: str) -> str:
    lines = [f"# Scenario evaluation ({mode}{', ' + model if mode == 'llm' else ''})", "",
             f"Run {datetime.now():%Y-%m-%d %H:%M}. Simulated telemetry, real agent code above the HTTP layer.", "",
             "| Scenario | Pass | Detection | Diagnosis | Fix | No flag leak | Said | Confidence | Fix proposed | Model/tool calls |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    mark = lambda b: "yes" if b else "**NO**"  # noqa: E731
    for r in results:
        c = r["checks"]
        lines.append(f"| {r['scenario']} | {mark(r['passed'])} | {mark(c['detection'])} | {mark(c['diagnosis'])} | "
                     f"{mark(c['fix'])} | {mark(c['no_flag_leak'])} | {r['said'] or r['status']} | {r['confidence'] or '-'} | "
                     f"{r['fix']} | {r['model_calls']}/{r['tool_calls']} |")
    n = len(results)
    agg = Counter()
    for r in results:
        agg.update({k: int(v) for k, v in r["checks"].items()})
    lines += ["", f"**Passed {sum(r['passed'] for r in results)} / {n}**  "
              + "  ".join(f"{k}: {agg[k]}/{n}" for k in ("detection", "diagnosis", "fix", "no_flag_leak"))
              + f"  confident-and-wrong: {sum(r['confident_wrong'] for r in results)}", ""]
    for r in results:
        if not r["passed"]:
            lines.append(f"- {r['scenario']}: failed {[k for k, v in r['checks'].items() if not v]}; "
                         f"status {r['status']}; said {r['said']!r}. " + " | ".join(r["limitations"][:3]))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=["llm", "oracle"], default="llm")
    p.add_argument("--provider", default=os.getenv("LLM_PROVIDER", "google_genai"))
    p.add_argument("--model", default=os.getenv("LLM_MODEL", ""))
    p.add_argument("--only", nargs="*", help="scenario ids")
    p.add_argument("--rename", action="store_true", help="invented service names")
    p.add_argument("--repeat", type=int, default=1)
    p.add_argument("--out", default=str(AGENT / "evals" / "results"))
    a = p.parse_args(argv)
    if a.mode == "llm" and not a.model:
        print("error: set LLM_MODEL in .env or pass --model (or use --mode oracle)", file=sys.stderr)
        return 2
    chosen = [BY_ID[i] for i in a.only] if a.only else SCENARIOS
    out = Path(a.out) / f"{datetime.now():%Y%m%d-%H%M%S}-{a.mode}{'-renamed' if a.rename else ''}"
    out.mkdir(parents=True, exist_ok=True)
    results = []
    for rep_i in range(a.repeat):
        for sc in chosen:
            t0 = time.time()
            tag = f"-r{rep_i + 1}" if a.repeat > 1 else ""
            try:
                r = run_one(sc, a.mode, out, a.provider, a.model, a.rename, tag)
            except Exception as exc:  # one broken run must not hide the others
                r = {"scenario": sc.id, "status": "CRASHED", "alert": None, "said": None, "confidence": None,
                     "fix": "-", "model_calls": None, "tool_calls": None, "confident_wrong": False, "passed": False,
                     "checks": {"detection": False, "diagnosis": False, "fix": False, "no_flag_leak": True},
                     "limitations": [f"{type(exc).__name__}: {exc}"]}
            results.append(r)
            print(f"{sc.id + tag:28} {'PASS' if r['passed'] else 'FAIL'}  status={r['status']:12} said={r['said']!r} "
                  f"conf={r['confidence']} fix={r['fix']} checks={r['checks']} ({time.time() - t0:.0f}s)", flush=True)
    (out / "results.json").write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    md = summarise(results, a.mode, a.model)
    (out / "summary.md").write_text(md, encoding="utf-8")
    print("\n" + md + f"\n\nResults: {out}")
    return 0 if all(r["passed"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
