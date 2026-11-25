# `telemetry.py` in brief: the AI on-call agent's eyes

Context for whoever prepares the presentation: Team 6 built an AI on-call engineer for the Bosch "Incident Zero"
hackathon. When the OpenTelemetry Demo shop breaks, the agent investigates live telemetry, names the failing service
with evidence, and proposes a safe fix. `telemetry.py` (owner: Neha, Member 2) is the set of **read-only tools** the
LLM calls to see metrics, traces and logs. Everything the agent knows comes through this file.

## What it connects to

| Signal | Backend | API used |
|---|---|---|
| Metrics (request rate, errors, latency, container CPU/memory) | Prometheus | HTTP query API with PromQL |
| Traces (a request's path through services) | Jaeger | v3 API, OTLP JSON |
| Logs | OpenSearch | PPL query language |

## Core design decisions

1. **Read-only and bounded.** No writes, no shell, no Docker, no arbitrary URLs. The model never writes queries: only
   fixed templates. Service names are validated against the live inventory; time windows are limited (1-60 min);
   every request has a timeout and one retry.
2. **Every result is evidence.** Each call returns a `ToolResult` with a unique `evidence_id` (e.g.
   `ev-search_traces-1a2b3c4d`), the exact query, the time window, status and value, and is saved to a file. The LLM
   must cite these IDs in its diagnosis; invented IDs are rejected downstream.
3. **"No data" is never "healthy".** Four statuses: `ok`, `empty` (backend answered, nothing in the window),
   `unavailable` (backend unreachable), `error`.
4. **No feature-flag leaks.** Reading flag settings is disqualifying under the hackathon rules. The file excludes flag
   services, drops calls to them, and replaces any flag wording with a neutral marker (the demo's own error text says
   "Product Catalog Fail Feature Flag Enabled", which would hand over the answer).
5. **Changes, not levels.** The demo is never fully healthy (some services always error a little). So tools compare
   *now* (last 3 min) with the *previous* 3 min and the *30 min before*, and report what changed, what is ongoing, and
   what is recovering.
6. **Complete output, no truncation.** Size is controlled by summarising (e.g. trace patterns), never by cutting JSON.

## The tools the LLM can call

| Tool | What it does |
|---|---|
| `overview` | **First look at every service.** Request rate, error rate and p95 latency now vs previous 3 min vs 30-min baseline, for each service **and each incoming operation** (a failing operation that is 0.2 % of a service's calls would vanish in the service total). Returns `changed_since_previous_window`, `operations_changed`, `recovering_since_previous_window`, or `NO BASELINE` when history is missing (e.g. after a restart). |
| `log_overview` | **Every service's warnings and errors at once**, grouped by message, with **when each first and last appeared** in the last ~33 min: `started_recently` (≤ 15 min, strong lead), baseline (always there), or `stopped_earlier` (an earlier incident that ended). |
| `search_traces` | **Where failures start and where time goes.** Asks Jaeger explicitly for error traces (a random sample missed a 31 %-failing route), then finds the origin of each failure, the slow step in slow traces, calls that got no answer, caller→callee edges, and groups traces that fail the same way into patterns. |
| `query_metrics` | One fixed metric for one service: request rate, error rate, errors by operation, p95, p95 by operation, container CPU, memory, memory trend, uptime. |
| `query_logs` | One service's WARN+ log lines with time, severity, trace ID and log ID. |
| `service_health` | One-call summary of a service: traffic, errors, latency, CPU, memory, memory trend, uptime, recently restarted. |

## Important implementations

### Metrics
- **Span metrics:** the collector turns every traced call into `traces_span_metrics_calls_total` (counter) and a latency
  histogram. Error rate = `rate(errors[3m]) / rate(all calls[3m])`; p95 = `histogram_quantile(0.95, …)`; the previous
  window uses `offset 3m`.
- **`_changes`** turns three numbers into plain verdicts such as `error_rate up 0.000 -> 0.300`,
  `p95 up 200 -> 950 ms`, `recovering: error_rate down …`, `elevated … (ongoing)`, `traffic down …`. Noise guards:
  an error rise needs ≥ 2 failed calls; latency must at least double (+100 ms), because demo latency drifts ~1.5× on
  its own.
- **Real CPU:** `rate(container_cpu_usage_nanoseconds_total[3m]) / 1e7` (percent of one core). The obvious metric
  (`container_cpu_utilization_ratio`) turned out to be a lifetime average: a container at 400 % showed 26 %.

### Traces
- **`_parse_span`** decodes each span (service, operation, kind, duration, error, error message, safe attributes) and
  marks calls to the flag service by their **target** only.
- **`_error_origin`:** the deepest failing span whose children did not fail is where the failure starts. If that span
  is an outgoing call and the called service left no span (e.g. it is down), the **called** service is named as a
  `suspected` origin, with the caller as `observed_in`.
- **`_latency_origin`:** in a slow trace, follow the longest child while it covers ≥ 50 % of its parent; the end of
  that path is where the time went. If finished child spans explain < 50 % of the time, it says `unexplained`
  (a downstream call still hanging has no span yet).
- **`_is_timeout`:** an HTTP 504, gRPC deadline or "timeout" message means the span gave up waiting, so it is marked
  `timed_out_waiting`, a symptom of the slow service, not the cause.
- **`_patterns`:** groups traces that fail the same way (with counts and example trace IDs) and shows only the 3 newest
  in full. This cut the LLM's token use from ~70k to ~49k per investigation with the same facts.

### Logs
- PPL queries against `otel-logs-*`; nested severity fields handled; a fallback query if field names differ.
- `log_overview` runs two aggregate queries (now, and the last 33 min with `min`/`max` timestamps) to date every
  message.

### Safety and plumbing
- **`service_inventory` / `validate_service`:** the live list of services from Prometheus labels, container names and
  Jaeger; flag services and the team's own Grafana container excluded; unknown names rejected.
- **`_redact`, `_FLAG_PATTERN`, `_FLAG_SERVICE`:** flag wording in any spelling is replaced by
  `[message text withheld by policy]`; calls to flagd/OpenFeature are dropped.
- **`_request`:** timeout plus one retry. **`_capture`:** saves every result to `evidence/<evidence_id>.json`.
  **`_failure`:** connection problems become `unavailable`, other problems `error`.
- **`TOOLS`, `tool_specs`, `run_tool`:** the registry. Each tool has a Pydantic argument model; `tool_specs` gives the
  LLM OpenAI-style function definitions; `run_tool` validates arguments before running.
- **CLI:** `python telemetry.py check | overview | traces <svc> | logs <svc> | log-overview | health <svc>` for manual
  checks (CP0: all three backends answer HTTP 200).

## Problems found and fixed while building it (good for the "learnings" part)

| Problem seen live | Fix in this file |
|---|---|
| A failing operation (0.2 % of calls) was invisible in the service-wide error rate | Per-operation comparison in `overview` |
| Timeouts made the gateway look like the origin | `_latency_origin`, `_is_timeout` |
| A random trace sample missed a 31 %-failing route | Ask Jaeger for error traces explicitly |
| An earlier incident was blamed for a new one | 3-min windows; recovering / ongoing labels; log first/last seen |
| Flag text leaked the answer; the flag filter broke the trace chain | Redact text, decide by call target, keep spans |
| CPU incidents invisible (lifetime-average metric) | CPU from the CPU-time counter |
| Token budget ran out on repeated traces | Trace patterns |
| Cut-off output broke JSON | Never truncate; summarise instead |
| No history after a restart read as "no change" | `NO BASELINE` reported explicitly |

## One-line summary for the speech

"`telemetry.py` gives the AI read-only eyes on metrics, traces and logs: it compares now with before instead of
trusting absolute levels, follows each failure to the service where it starts, dates every log message, hides anything
that would leak the injected fault, and turns every answer into a citable piece of evidence."
