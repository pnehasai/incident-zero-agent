# Evaluation results (30 Oct 2026; live suite 23-25 Nov 2026)

Model: `gemini-3.1-flash-lite` (fallback `gemini-3.5-flash-lite`) through LangChain, temperature 0.

## 1. Simulated incidents, full agent (`evals/run_scenarios.py`)

The simulated world answers the agent's real Prometheus / Jaeger / OpenSearch queries; everything above HTTP is
the real agent (detector, tools, evidence picture, LLM loop, guardrails, fix rules, report). Ground truth stays in
the runner. Each run is scored on four checks: detection (alert iff a real incident), diagnosis (the ONE named
service is the origin; for no incident: inconclusive or low confidence), fix rule, and no flag wording reaching
the model.

| Run | Passed | Detection | Diagnosis | Fix rule | No flag leak | Confident and wrong |
|---|---|---|---|---|---|---|
| Demo names, run 1 | 10 / 10 | 10/10 | 10/10 | 10/10 | 10/10 | 0 |
| Invented names (`--rename`) | 10 / 10 | 10/10 | 10/10 | 10/10 | 10/10 | 0 |
| Demo names, repeats 2-3 | 20 / 20 | 20/20 | 20/20 | 20/20 | 20/20 | 0 |
| **Total** | **40 / 40** | | | | | |

Run 1 was first scored 0/10 on "no flag leak": a bug in the scorer, which read the system prompt (it names flags
only to forbid them). Re-scored on what the model received from telemetry: no leaks.

Scenarios: payment errors, partial product-catalog failure with flag text in the error (must be withheld), CPU hog,
crash loop, cart application errors (restart must not be proposed), payment unreachable, slow dependency with
504s at the caller, leftover old fault plus a new one, no incident, memory leak. Typical cost: 4-5 model calls,
5-8 tool calls, 15-30 s.

Oracle mode (fixed policy, no LLM) also passes 10/10 with demo names and with invented names; this runs in
`pytest` (`tests/test_scenarios_e2e.py`) so a tool or ranking regression fails the test suite.

**What this does not show:** the simulator is cleaner than the real demo (one fault at a time, no noisy
neighbours beyond what is scripted, steady traffic). 40/40 means the pipeline and prompt handle these shapes; it
does not prove the same rate on live faults.

## 2. Live demo (`evals/live_eval.py`)

| Case | Injected | Detector | Agent said | Verdict |
|---|---|---|---|---|
| LIVE-0-nofault | nothing | no alert in 3 min (correct) | checkout, CPU 706% (8.4x its prior), restart proposed | Scorer: FAIL. Checked by hand: **the finding is real**. `docker stats` showed checkout at 1317% CPU with no fault injected, up from ~318% 20 min earlier. |

Not yet run live: injected faults. Injecting needs the flag UI, which the permission checker blocked in this
session. Run them by hand (one at a time, about 5 min apart so the previous fault fades):

```powershell
.\.venv\Scripts\python.exe evals\live_eval.py --case L1 --expect payment --fix none            # then: paymentFailure 50%
.\.venv\Scripts\python.exe evals\live_eval.py --case L2 --expect product-catalog --fix none    # productCatalogFailure on
.\.venv\Scripts\python.exe evals\live_eval.py --case L3 --expect ad --fix any                  # adHighCpu on
.\.venv\Scripts\python.exe evals\live_eval.py --summary
```

## 3. Live suite, real faults on the running demo (`evals/live_suite.py`, 23-25 Nov 2026)

Each case waits for a quiet shop and for the detector's baseline, injects one fault (stops a service, or for the
`F` cases turns on a feature flag), lets the real detector raise the alert, runs the full agent, restores the shop
and scores four checks: detected, correct service named, correct fix decision, no false alarm on the no-fault (`N`)
cases. A case is marked **INVALID** and not counted when the laptop slept during it (a clock gap over 120 s) or the
detector had no baseline; resuming the same `--run` repeats it.

| Run | What changed before it | Passed | Detected | Correct service | False alarms (N cases) | Invalid |
|---|---|---|---|---|---|---|
| `20261123` | - | 4 / 5 | 4 / 4 | 4 / 4 | 1 / 1 (email, latency jitter) | N1, S3 (laptop slept) |
| `20261124` | detector fixes (below) | 4 / 7 | 5 / 5, median 80 s | 2 / 5 | **0 / 2** | none |
| `20261124b` (S2-S4 again) | LLM retry fix (below) | **3 / 3** | 3 / 3, median 51 s | 3 / 3 | - | S4 once (slept; re-run passed) |

Latest result per case: S1-S5 and N1-N2 all pass (7 / 7), every diagnosis with high confidence, no case
confident and wrong. The three failures in `20261124` were not wrong answers: Gemini returned 503/504 on both the
main and the fallback model, so the agent escalated to a human (the safe outcome). In `20261124b` the same
overload was still there (S2: first request answered on attempt 4 after 52 s; S3: one request needed 5 attempts
over 136 s) and the new retry got through.

Detector replays of the recorded Prometheus data (`evals/replay_detector.py`) with the fixed rules: 24 Nov, 4 alerts,
0 false; 22 Nov, all 18 faults detected, 1 false alert (payment p95). Results: `evals/results/suite-2026112*/`.

Not yet run: the flag cases F1-F6 (the flag file can only be changed by a person in this setup):
`.\.venv\Scripts\python.exe evals\live_suite.py --run 20261124 --flags file`.

**What this does not show:** one run per case, one machine, one free LLM provider. S3 used 226 of its 240 s in
`20261124b`; a Gemini outage longer than the budget still ends in escalation.

## 4. Bugs found and fixed during this evaluation

| Problem | Seen in | Fix |
|---|---|---|
| Model wrote `supported_evidence_ids`; the unknown key was silently dropped, so a correct crash-loop diagnosis was rejected twice as "uncited" | live INC-20261024-083048 | finish schema accepts near-miss keys, requires evidence IDs, gives field descriptions; repair message names the exact fields |
| OpenSearch port changes on each demo restart; stale `.env` silently removed the logs backend | this session (49735 -> 50522) | `preflight.resolve_opensearch()` finds the live port; `cli.py doctor` checks every prerequisite |
| Detector alerted on warm-up after a restart (frontend p95 13.8 s vs a 13-min "baseline") | live, this session | no alert until a service has traffic a full 30 min ago |
| Finish with component "none" cost a correction round | simulated no-incident | treated as inconclusive directly; prompt rule for the no-incident case |
| Orchestrator defaults (180 s / 12 / 6) disagreed with the CLI (240 s / 16 / 8) | code review | aligned |
| Timeline logged "fix proposed" before "diagnosed" | code review | order fixed |
| Flag wording in the `uncertainty` field was not checked | code review | guardrail checks it |
| No-fault case N2 alerted on email: a p95 latency blip, 3x its median but within its usual peaks | live suite `20261123` | p95 rule for low-traffic services (<100 requests per window) also needs 2x the baseline's 90th-percentile peak |
| A service coming back after an outage looked like a traffic flood (rate vs a baseline of ~0) | replay 24 Nov | surge needs a baseline of at least 1 req/s, 2x its usual peak, and a baseline window in which the service had traffic |
| Laptop sleep silently spoiled cases (no baseline, missed minutes) | live suite `20261123` (N1, S3) | suite detects clock gaps and waits for the detector's history; such cases are INVALID, not counted, and re-run on resume |
| Gemini overload: one 8 s retry, then escalation with >160 s of budget left | live suite `20261124` (S2, S3, S4) | after the immediate switch to the fallback: waits of 8, 16, 32, 64 s alternating the two models, only while the time budget allows |
