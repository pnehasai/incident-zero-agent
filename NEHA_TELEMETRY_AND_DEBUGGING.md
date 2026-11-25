# My part: the agent's eyes, its Grafana view, and the debugging behind them

**Neha (Member 2), Team 6 · Bosch Incident Zero hackathon · 23-24 Oct 2026**

The brief: build an AI on-call engineer for the OpenTelemetry Demo shop. When something breaks, an alert starts an
investigation; the agent queries metrics, traces and logs, finds where the problem started, proposes a safe fix, waits
for human approval, acts through the Action API and verifies recovery.

My part is the agent's **eyes**: the read-only tools that turn Prometheus (metrics), Jaeger (traces) and OpenSearch
(logs) into evidence the LLM can reason about and cite. Around that I built the automatic detector (step 1), the
evidence ranking, the Grafana view, and I spent most of the hackathon **debugging why the agent picked the wrong
service** and fixing the tools so it doesn't.

---

## 1. Background: what the tools read

| Signal | Backend | What it is | How I query it |
|---|---|---|---|
| **Metrics** | Prometheus | Numbers over time. The collector's *span metrics* turn every traced call into counters (`traces_span_metrics_calls_total`) and latency histograms (`traces_span_metrics_duration_milliseconds_bucket`), labelled by `service_name`, `span_name`, `span_kind`, `status_code`. docker_stats adds container CPU, memory, uptime. | PromQL over the HTTP API (`/api/v1/query`) |
| **Traces** | Jaeger | One request's journey: a tree of *spans* (one per operation), each with a parent, a kind (server = receiving a call, client = making one), a status (error or not), a duration and attributes. | Jaeger v3 API (`/jaeger/ui/api/v3/traces`), OTLP JSON |
| **Logs** | OpenSearch | Log lines with severity, body, service and trace ID. | PPL, OpenSearch's SQL-like query language (`/_plugins/_ppl`) |

PromQL functions I rely on, and why:

- `rate(counter[3m])`: per-second increase over a window. Error rate = rate of error calls / rate of all calls.
- `histogram_quantile(0.95, …)`: p95 latency from histogram buckets.
- `offset 3m`: the same query, but for the window before. This is how "now vs before" works.
- `resets(gauge[30m])`: counts drops in a value; uptime dropping means a restart.
- `deriv(gauge[3m])`: slope; positive memory slope means memory is growing.
- `max_over_time`, `avg_over_time` with subqueries: a container's peak and average in an earlier window.

**The key design idea: judge changes, not levels.** This demo is never fully healthy (some services always error a
little, two run at their memory limits). A high value that was already high before is the baseline. So every tool
compares *now* with *before*, and says when something started or stopped.

---

## 2. Design principles for the tools

1. **Read-only and bounded.** Only GET-style queries; fixed PromQL templates (the model never writes free-form queries);
   service names validated against the live inventory; windows limited; result sizes limited.
2. **Every result is evidence.** Each call returns a `ToolResult` with an `evidence_id`, the exact query, time window,
   status and value, and saves a capture file. The LLM must cite these IDs; guardrails check they exist.
3. **Status is never ambiguous.** `ok`, `empty` (backend answered, no data), `unavailable` (backend down) and `error` are
   different. "Unavailable" is never read as "healthy".
4. **Flag safety.** Reading feature-flag settings is disqualifying, so flag services are excluded, calls to them are
   dropped, and any flag wording is redacted before the model sees it.
5. **Full output, no cutting.** An early version cut results at 3,500-6,000 characters, which broke the JSON. Now results
   are complete; size is controlled by summarising (patterns), not truncating.

---

## 3. The tools, function by function (`telemetry.py`)

### Shared plumbing

| Function / class | What it does |
|---|---|
| `ToolStatus`, `ToolResult` | The result shape: status, `evidence_id`, tool, source, service, query, window, value, excerpt, trace IDs, log IDs, capture path, message. `for_llm()` serialises the full result as compact JSON. |
| `_request` | One HTTP call with timeout and one retry for transient failures. |
| `_capture` | Saves `{result, raw backend response}` to `evidence/<evidence_id>.json` (proof the evidence exists). |
| `_result`, `_failure` | Build a `ToolResult`; `_failure` maps connection errors to `unavailable` and others to `error`. |
| `_window`, `_rfc3339`, `_now` | Validate and build the time window (default 3 min). |
| `_short`, `_redact` | Shorten long text; replace flag wording with a neutral marker. |
| `service_inventory` | Live list of services from Prometheus labels (`service_name`, `container_name`) and Jaeger; excludes flag services and our own tooling containers. Cached 60 s. |
| `validate_service` | Rejects names that are malformed or not in the inventory. |
| `TOOLS`, `tool_specs`, `run_tool` | Registry: each tool with a Pydantic argument model and description. `tool_specs` gives OpenAI-style function specs for the LLM; `run_tool` validates arguments and runs the tool. |
| `check_backends`, `main` | CP0 check (Prometheus, Jaeger, OpenSearch → HTTP 200, services seen) and a CLI for every tool. |

### Metrics

| Function | What it does |
|---|---|
| `_prom` | Runs a PromQL query; drops NaN and infinity (no data is not zero). |
| `query_metrics` | One fixed template for one service: `request_rate`, `error_rate`, `errors_by_operation`, `p95_latency_ms`, `p95_latency_by_operation`, `container_cpu`, `container_memory`, `container_memory_trend`, `container_uptime`. Flag-service operations are filtered out. |
| `_overview_queries` | The three per-service queries (request rate, error rate, p95) for a window and offset. |
| `_operation_queries` | The same per **server operation** (`span_kind="SPAN_KIND_SERVER"`, by `span_name`). |
| `_changes` | Turns now / previous window / 30-min baseline into words: `error_rate up 0.000 -> 0.300`, `p95 up …`, `recovering: …`, `elevated … (ongoing)`, `traffic down …`. An error rise needs ≥ 2 failed calls (≥ 1 for one operation); p95 needs ≥ 2× and +100 ms. |
| `overview` | **Fleet-wide first look.** Every service and operation: now vs previous 3 min vs 30-min baseline; lists `changed_since_previous_window`, `operations_changed`, `recovering_since_previous_window`; says `NO BASELINE` when history is missing. |
| `service_health` | One service in one call: traffic, errors, p95, CPU, memory, memory trend, uptime, recently restarted. |

### Traces

| Function | What it does |
|---|---|
| `_attrs`, `_attr_value`, `_hex_id`, `_is_error`, `_kind` | Decode OTLP JSON: attributes, IDs (hex or base64), error status, span kind. |
| `_parse_span` | One span → service, name, kind, duration, error, error message (redacted), safe attributes, and whether it calls the flag service (decided only by the call's target). |
| `_call_target` | For a client span: which service it was calling (e.g. `oteldemo.PaymentService` → `payment`). |
| `_error_origin` | **Where a failure starts:** the deepest failing span whose children did not fail. If that span is an outgoing call and the callee left no span, it names the **callee** as `suspected`, with the caller as `observed_in`. |
| `_latency_origin` | **Where the time went** in a slow trace: follow the longest child while it covers ≥ 50 % of its parent. If finished children explain < 50 %, it says `unexplained` (a call still running has no span yet). |
| `_is_timeout` | HTTP 504, gRPC DEADLINE_EXCEEDED or "timeout" text: the span gave up waiting, so it is a symptom, marked `timed_out_waiting`. |
| `_patterns` | Groups traces that fail the same way (origin, message, slow step) into patterns with a count and example IDs. |
| `search_traces` | Asks Jaeger for **error traces explicitly** (`error=true`) plus a normal sample, then returns error-origin counts, slow-path origins, unanswered calls, caller→callee edges, failure patterns and the 3 newest traces in full. |

### Logs

| Function | What it does |
|---|---|
| `_ppl`, `_rows`, `_pick` | Run a PPL query; turn the schema + rows answer into dicts; pick the first present field. |
| `query_logs` | One service's WARN+ lines (time, severity, body, trace ID, log ID). Flag lines are kept but their text is withheld. |
| `_log_time` | Parse OpenSearch timestamps. |
| `log_overview` | **Fleet-wide logs:** every WARN+ message grouped by service and text, with how often it appeared now and in the last 33 min, **first seen** and **last seen**: `started_recently` (≤ 15 min), baseline, or `stopped_earlier`. |

### Containers (`resources.py`, which I took over and fixed)

| Function | What it does |
|---|---|
| `_instant` | One PromQL query → value per container. |
| `resource_overview` | Fleet-wide container check: current CPU (rate of the CPU-time counter), its average 30 min ago and 30-60 min ago, memory % and its prior average and peak, memory slope, uptime, restarts in 30 min. |
| `analyse` | Pure function that decides the flags: CPU ≥ 100 % and ≥ 2× its own baseline; memory ≥ 85 % and above its prior peak; memory growth; **CRASH LOOP** (≥ 2 restarts, last one < 10 min ago); restarts older than 10 min listed as `restarts_ended`; observability containers never flagged. |
| `_memory_runway` | "memory 37 % and rising 26 MB/min, ~13 min to its limit". |

### Glue and debugging tools

| Function | What it does |
|---|---|
| `integration.to_evidence`, `EvidenceRegistry` | Turn a `ToolResult` into the team's `Evidence` model; keep the run's evidence; reject duplicate IDs. |
| `candidates.build`, `rank`, `summary`, `consistency` | The **evidence picture**: every service in telemetry ranked by the kind of evidence against it (tier 4 direct + current, 3 direct, 2 current from 2+ sources, 1 one source, 0 old or a symptom). Shown to the LLM each round; one check asks it to look again only if a clearly stronger candidate exists. No service names in the logic. |
| `detector.measure`, `breaches`, `Detector`, `watch` | **Step 1:** every 15 s, error rate and p95 per service vs its 30-min baseline and the previous 2 min; ≥ 20 requests, ≥ 3 failed calls, 2 checks in a row, 5-min cooldown; then starts the investigation. |
| `trace_evidence.py` | Runs the same tools **without the LLM** and saves the exact text the model would get. My main debugging instrument. |
| `evals/replay_ranking.py` | Re-ranks the evidence of saved runs against the true fault (kept outside the agent's reach). |

---

## 4. Grafana (brief)

The organisers' Grafana belongs to the demo, so I run **our own** Grafana (same 13.2.1 image, port 3001) that reads
the demo's Prometheus read-only. Built-in features only (the container has no internet for plugins).

| Function | What it does |
|---|---|
| `grafana_publish.post_alert`, `publish` | Post the alert, and then the diagnosis, as annotations (time regions on every graph). |
| `grafana_dashboards.build` | From `runs/*/…_report.json`: an **Incidents** table (time in IST, incident, status, component, diagnosis, confidence, proposed fix) and **one page per incident** (diagnosis, every evidence item, the evidence picture, graphs of its services ±15 min). Data embedded as CSV through the built-in TestData source. |
| `reload_dashboards`, `clear_annotations`, `cli.py reset-dashboard` | Make Grafana pick up new pages; start a demo clean (old runs archived, not deleted). |

Two lessons from it: a page per incident turned out to be the fastest way to *see* what went wrong (it exposed two bugs
on first use, see #17 and #18), and timestamps must be shown in the viewer's zone (IST), not UTC.

---

## 5. Debugging journal

How I worked: reproduce with a real fault on the live demo → run `trace_evidence.py` to see exactly what the model was
given → decide whether **the tools hid the evidence** or **the model misread it** → fix the general cause, not the one
case → add a test with the real data shape → re-run live.

### Phase 1: making the tools trustworthy

**1. Live data did not match the documentation.** OpenSearch nests severity (`{number, text}`), log IDs live in `_id`,
the cache and database emit no spans, ports are dynamic. → Read nested fields, return `log_ids`, add
`container_name` to the inventory, `check` command for ports. *Learning: test against the live backend, not the docs.*

**2. Cutting results broke the JSON.** Results were cut at a fixed length; the model got invalid JSON and lost evidence.
→ Never cut; control size by summarising. *Learning: truncation is data loss.*

**3. A healthy demo looked broken.** Fixed limits flagged checkout and product-catalog (always at their 20 MiB limit).
→ Flag only **changes** against each container's own history; never flag observability containers.

**4. "No data" read as "no change".** After a restart Prometheus served no history. → Every comparison reports
`NO BASELINE: changes cannot be judged`. *Learning: missing data must be said out loud.*

### Phase 2: keeping the answer out of the telemetry

**5. Feature-flag wording reached the model.** Calls to the flag service in traces; product-catalog's error text
"Product Catalog Fail **Feature Flag** Enabled" (my first regex missed the space); later ad's background
`flagd…EventStream` in per-operation latency. → Exclude flag services, drop calls to them by target, redact any
spelling of "feature flag" with a neutral marker, filter metric series, and the guardrail rejects any diagnosis that
mentions flags. *Learning: an answer can leak through any field; check every path into the prompt.*

**6. The flag filter broke the trace tree.** Because frontend's span carried the propagated flag text, it was dropped,
the chain broke and frontend became the "origin". → Decide flag calls by the call **target** only; redact text but
keep the span. Live: product-catalog named as origin in 14 of 14 traces.

**7. The caller was blamed when the callee was down.** (paymentUnreachable) → An unanswered call names the callee as
`suspected`, with `observed_in` = caller.

### Phase 3: one incident at a time

**8. An old incident leaked into the new one.** A 15-minute window still contained the previous fault. → 3-minute
windows; `recovering` changes listed separately; the 30-min baseline catches incidents older than two windows
(`ongoing`).

**9. The LLM layer.** Moved from the Claude API to **LangChain** (provider-agnostic) on the Gemini free tier. Hit the
daily quota, 503 "high demand", 504 timeouts and empty replies (the model's hidden reasoning used all tokens). →
Lighter primary model, one retry, then a fallback model; per-request timeout; separate reasoning budget.

### Phase 4: the faults the agent could not see (blind tests)

**10. cartFailure: blamed product-catalog.** Three blind spots at once:
- only `EmptyCart` failed: 0.2 % of cart's calls, so cart's error rate rounded to 0 → `_operation_queries` compares every
  operation on its own;
- the calls hung 15 s+, the gateway timed out first and looked like the origin → `_latency_origin`, `_is_timeout`;
- cart logged nothing for the first 2 minutes → `log_overview` with first/last seen.

Result: "cart cannot reach its store", correct.

**11. Trace sampling missed the failures.** 40 sampled frontend traces held none of a route failing 31 % of the time.
→ `search_traces` asks for `error=true` traces first.

**12. A crash loop looked like "recovering".** (recommendationCacheFailure) Between crashes, callers recovered briefly.
→ `resources.analyse` counts restarts (`resets`) and projects memory to the limit: "CRASH LOOP: 4 restarts…, ~13 min to
its limit".

**13. …and then it never stopped saying "crash loop".** Restarts counted over 30 min kept the flag for 30 min after the
flag was off; "restarted recently" lasted ~2 h. → Current only if the last restart was < 10 min ago; older ones are
`restarts_ended`. *Learning: every signal needs an expiry.*

### Phase 5: leftovers after a fault is switched off

**14. Leftover failures looked live.** cartFailure keeps failing for an hour after it is off (the demo queues calls behind
one connection lock); after paymentFailure, one or two stray failed calls still counted as "error rate up". → An error
rise needs ≥ 2 failed calls; `log_overview` dates every message; first a "newest problem" check, later replaced by the
evidence picture (#20).

### Phase 6: resources

**15. CPU incidents were invisible.** (adHighCpu) `container_cpu_utilization_ratio` here is a **lifetime average since
the container started**: ad at 400 % in Docker showed 26 % and crept up 1.4 points a minute. → CPU from the CPU-time
counter: `rate(container_cpu_usage_nanoseconds_total[3m]) / 1e7` (percent of one core). Then: a spike lasting 15 min
lifted its own average and stopped being flagged → also compare with 30-60 min earlier. Result: ad at ~350 % vs a normal
4.9 %, diagnosed correctly. *Learning: verify a metric's meaning against a second source (Docker) before trusting it.*

**16. Memory swings flagged as growth.** product-catalog swings 75-93 % of its 20 MiB; 90 % vs a 77 % average looked like a
rise. → Flag only above the container's **prior 30-min peak**.

### Phase 7: scale and polish (found through the Grafana pages)

**17. Our own Grafana became the top suspect.** I restarted it while setting it up; the agent saw "agent-grafana: CRASH
LOOP". → Our tooling containers are excluded from the inventory and resource flags.

**18. The token budget ran out.** Ten near-identical traces per search (15-20k characters), plus an evidence summary
re-sent in every message, passed 60k tokens in five model calls → "inconclusive". → `_patterns` groups traces with
counts; only the newest evidence summary is kept. 70k → 49k tokens with the same facts.

### Phase 8: detection (step 1)

**19. Alerts too slow, too noisy, or missing.**
- productCatalogFailure fails one product: ~4 % errors, under a 5 % threshold → relative threshold (≥ 2 % and > 3× its
  baseline);
- browser telemetry with a handful of requests swung to 66 % → ≥ 20 requests in the window;
- a fresh Prometheus after a restart had no baseline, so every normal error looked new → no alert without history;
- a fault on since the demo started was learned as "normal" → also compare with the previous 2 minutes;
- 2-min window and 15-s checks: alert in ~1-2 min instead of ~5.

### Phase 9: from patches to one general rule

**20. My fixes were turning into patches.** A review pointed out that separate "send back" rules (stale, newest, ignored
resource flag) could fail on hidden test cases. → Replaced them with the **evidence picture** (`candidates.py`): one
generic ranking by kind of evidence, recency as one input, several active faults allowed. Tested with invented service
names (`svc-a`, `gateway`, `db-x`). Replay on saved real runs: true origin ranked first in 8 of 14; five of the six misses
came from runs collected before the tool fixes. *Learning: a rule that names a service or fault passes the cases you
tested and fails the ones you didn't.*

**21. Small but real.** Two services in one diagnosis field ("payment and email") → guardrail asks for one component
plus "also active" alternatives. A restart proposed for an application error → a restart needs evidence of a
process-level fault. Developer-side: a watcher still running old code after a fix; PowerShell rewriting a UTF-8 file as
ANSI (≥ became mojibake). *Learning: after a fix, restart what runs it, and check encodings on Windows.*

---

## 6. Issues I found in the organisers' demo (reported, not changed)

| # | Issue | Effect | How the tools cope |
|---|---|---|---|
| 1 | checkout and product-catalog capped at 20 MiB (`GOMEMLIMIT=16MiB`) | High CPU, failing health checks, rising errors with no fault; worse over hours (checkout once ~300 % CPU) | Changes, not levels; restart those containers before a demo |
| 2 | product-catalog's error text names the injected fault | Hands the answer to any trace reader | Redacted |
| 3 | load-generator calls `agent:8010/prompt`, not running in this mode | ~3 % constant errors | Baseline |
| 4 | OpenSearch at ~920 MiB of 1 GiB | Slow or timed-out log queries | 20 s timeout, `unavailable` status |
| 5 | Prometheus loses history after a restart ("overlapping blocks", ~90 min gap once) | No baseline | `NO BASELINE`, detector waits |
| 6 | Flag UI rewrites `demo.flagd.json` without its final newline | Spurious repo diff | `git checkout --` |
| 7 | payment once missing from container metrics (uncertain) | Missing evidence | Reported as missing |
| 8 | ~~failedReadinessProbe has no observable effect~~ **Corrected:** cart logs `ERROR "Health check … completed … with message"` | Visible only in logs | `log_overview` |
| 9 | cartFailure keeps failing ~1 h after it is off; recommendationCacheFailure keeps its leaked memory | Leftovers look live | Signals dated; only `docker restart` clears them (ask the organisers) |
| 10 | `container_cpu_utilization_ratio` behaves as a lifetime average (possibly a collector setting) | CPU spikes invisible | CPU-time counter |

Flags with no effect in this setup: `kafkaConsumerDead`, `kafkaQueueProblems` (Kafka not running), `aiRunawayAgent`,
`aiSlowResponse` (agent service not running), `emitRawPii`, `cartMetricCardinalityOverflow` (not failures).

---

## 7. Results

| Fault | Agent's answer | Verdict |
|---|---|---|
| paymentFailure | payment rejects Charge, "Invalid token" | correct |
| productCatalogFailure | product-catalog gRPC INTERNAL errors, high confidence | correct |
| cartFailure | cart cannot reach its store | correct after fixes 10-11 |
| adHighCpu | ad at ~350 % CPU | correct after fix 15 |
| ad errors (watcher) | auto-detected at 7.5 %, diagnosed ad | correct |
| no fault | no alert; manual run says false alarm | correct |
| two faults at once | found both; now one component + "also active" | found both |
| recommendationCacheFailure | tools found the crash loop; model picked a leftover | not re-run after fixes |

128 automated tests (fake backends, fake model, real data shapes).

---

## 8. What I learned and improved on

- **Check the evidence before blaming the model.** Most wrong answers came from tools that hid or distorted evidence.
  Building `trace_evidence.py` to see exactly what the model saw changed how fast I could fix things.
- **Changes, not levels; and date every signal.** New, ongoing, recovering, stopped. That is what separates a live fault
  from the baseline and from leftovers.
- **Know your metric.** One "obvious" CPU metric was a lifetime average. Cross-check with a second source.
- **Follow causality in traces.** The deepest failing span, where the time went, who did not answer; the service that
  alerts is often only the caller.
- **Summarise, don't truncate.** Group repeated data, keep every fact, stay within the token budget.
- **Generalise.** Fixes that name a service or fault are patches; tests with invented names keep the logic honest for
  hidden cases.
- **Respect the rules in the design.** No flag reading and no demo edits forced better tools: redaction, change
  detection, our own Grafana.
- **Be honest about limits.** "No baseline", "unexplained", "inconclusive" are correct answers, and a corrected claim
  (failedReadinessProbe) is better than a wrong one.
