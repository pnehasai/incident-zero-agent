"""Step 1: detect an incident from threshold alerts on the demo's metrics and start an investigation automatically.

    python cli.py watch                 # poll every 15 s until Ctrl+C
    python cli.py watch --once          # one check (prints what it sees)

Generic thresholds per service, each against that service's own 30-min baseline (no service or fault names):
  error rate   >= 2%, > 3x baseline + 1 point, and >= 3 failed calls in the 2-min window
  p95 latency  >= 1000 ms, >= 3x baseline and, below 100 requests in the window, >= 2x its usual peaks (90th
               percentile of its 2-min p95s over the baseline): there p95 is set by the 2nd-slowest request and spikes
  both need >= 20 requests in the window (rates from a handful of requests are noise)
A service needs traffic a full 30 min ago (no alerts during the demo's warm-up after a restart).
Only services that SERVE requests (server or consumer spans) can alert: pure traffic sources (a load generator, a
browser) only make calls, so their errors are the symptoms of others or of their own test setup.
The "jumped vs previous window" rule also needs >= JUMP_MIN_CALLS failed calls (it compares two short windows).
A breach must hold on 2 consecutive checks (filters one-sample blips). Every service breaching at the same time
goes into one alert; a service that starts breaching later raises a new alert (several faults can overlap).
After an alert, its services are quiet for COOLDOWN_S so one incident is not investigated twice.

Three more rules, added after the live suite of 22 Nov missed faults that requests alone do not show
(evals/results/suite-20261122-fix; thresholds checked by replaying that run with evals/replay_detector.py):
  operation  one served operation fails >= 20% of its calls in OP_WINDOW_MIN (>= 3 failed) while it was clean
             before: a rare call (one every 30 s) failing barely moves its service's error rate
  resources  container CPU >= 80% of a core and >= 4x its own 30-min average + 20 points, or memory >= 85% of its
             limit (2-min average), >= 10 points over its own average and still rising; a container up < 10 min is skipped
  traffic    a served service gets >= 3x its baseline request rate and >= 5 req/s more (a flood), from a baseline of
             >= 1 req/s and >= 2x its usual peaks: a service coming back after an outage returns to its old level
             (its average is pulled down by the outage, its peaks are not) and is not a flood
Fixed after the live run of 24 Nov (evals/results/suite-20261123): the usual-peaks condition (false alarm on
email, 1.1 s vs 0.2 s with no fault) and the baseline floor for floods (product-catalog recovering looked like one).
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import telemetry

WINDOW_MIN, BASELINE_MIN = 2, int(os.getenv("WATCH_BASELINE_MIN", "30"))  # span metrics arrive every 15 s; WATCH_BASELINE_MIN=10 shortens the warm-up for demos
ERR_MIN, ERR_FACTOR, ERR_POINTS, ERR_MIN_CALLS = 0.02, 3.0, 0.01, 3  # partial faults (one product, 10% of calls) stay in single digits
P95_MIN_MS, P95_FACTOR, P95_PEAK_FACTOR = 1000.0, 3.0, 2.0
# Above this many requests per window a p95 is stable; the usual-peaks check would only delay alerts there (replay
# 22 Nov: peaks inflated by the previous fault delayed busy services by a check)
P95_PEAK_MAX_REQUESTS = 100
JUMP_MIN_CALLS = 10  # the 2-min vs 2-min comparison swings on a few calls (seen live: 1.5% -> 5.7% from noise)
MIN_REQUESTS = 20  # per window: below this, error rates and p95 swing on a few requests
CONSECUTIVE = 2
COOLDOWN_S = 300
# While the full baseline is not there yet (just after a start or a laptop sleep) a 10-min one is used instead.
# Replayed on 2 h of live history (evals/replay_detector.py, 5 Nov): 30 min alone missed a fault during warm-up,
# 10 min alone missed a fault that started right after another one (its short memory already counted the errors
# as normal); 30 min with a 10-min warm-up fallback catches both. Neither raised a false alarm.
WARMUP_BASELINE_MIN = 10
OP_WINDOW_MIN, OP_ERR_MIN, OP_MIN_FAILED = 10, 0.20, 3
CPU_MIN_PCT, CPU_FACTOR, CPU_POINTS = 80.0, 4.0, 20.0
MEM_MIN_PCT, MEM_POINTS, MEM_RISE_POINTS = 85.0, 10.0, 2.0
MIN_UPTIME_S = 600
SURGE_FACTOR, SURGE_MIN_RPS, SURGE_MIN_BASE_RPS, SURGE_PEAK_FACTOR = 3.0, 5.0, 1.0, 2.0
# Not the shop: the observability stack never raises resource alerts.
NOT_SHOP_CONTAINERS = {"opensearch", "prometheus", "grafana", "jaeger", "otel-collector", "opamp-server", "telemetry-docs"}

_C, _D = "traces_span_metrics_calls_total", "traces_span_metrics_duration_milliseconds_bucket"


def _queries(BASELINE_MIN: int) -> dict[str, str]:
    return {
        "rate": f"sum by (service_name) (rate({_C}[{WINDOW_MIN}m]))",
        "rate_base": f"sum by (service_name) (rate({_C}[{BASELINE_MIN}m] offset {WINDOW_MIN}m))",
        # Traffic a full baseline ago: without it the "30-min baseline" is really the demo's warm-up (seen live: frontend
        # p95 13.8 s against a 0.3 s "baseline" 15 min after a restart), so no alert until the history is complete.
        "history": f"sum by (service_name) (rate({_C}[5m] offset {BASELINE_MIN}m))",
        # Serves requests (server or consumer spans) in the last 30 min: traffic sources never get this row.
        "served": f'sum by (service_name) (rate({_C}{{span_kind=~"SPAN_KIND_SERVER|SPAN_KIND_CONSUMER"}}[{BASELINE_MIN}m]))',
        # The window just before this one: a fault switched on reacts here at once, even when the 30-min baseline
        # still contains an earlier spell of the same fault.
        "err_prev": (f'sum by (service_name) (rate({_C}{{status_code="STATUS_CODE_ERROR"}}[{WINDOW_MIN}m] offset {WINDOW_MIN}m))'
                     f" / sum by (service_name) (rate({_C}[{WINDOW_MIN}m] offset {WINDOW_MIN}m))"),
        "err": (f'sum by (service_name) (rate({_C}{{status_code="STATUS_CODE_ERROR"}}[{WINDOW_MIN}m]))'
                f" / sum by (service_name) (rate({_C}[{WINDOW_MIN}m]))"),
        "err_base": (f'sum by (service_name) (rate({_C}{{status_code="STATUS_CODE_ERROR"}}[{BASELINE_MIN}m] offset {WINDOW_MIN}m))'
                     f" / sum by (service_name) (rate({_C}[{BASELINE_MIN}m] offset {WINDOW_MIN}m))"),
        "p95": f"histogram_quantile(0.95, sum by (le, service_name) (rate({_D}[{WINDOW_MIN}m])))",
        "p95_base": f"histogram_quantile(0.95, sum by (le, service_name) (rate({_D}[{BASELINE_MIN}m] offset {WINDOW_MIN}m)))",
        "p95_peak": (f"quantile_over_time(0.9, (histogram_quantile(0.95, sum by (le, service_name) "
                     f"(rate({_D}[{WINDOW_MIN}m]))))[{BASELINE_MIN}m:1m] offset {WINDOW_MIN}m)"),
        "rate_peak": (f"quantile_over_time(0.9, (sum by (service_name) (rate({_C}[{WINDOW_MIN}m])))"
                      f"[{BASELINE_MIN}m:1m] offset {WINDOW_MIN}m)"),
    }


_BASE_KEYS = ("rate_base", "history", "err_base", "p95_base", "p95_peak", "rate_peak")


def _all_queries(baseline: int) -> dict[str, str]:
    q = _queries(baseline)
    if WARMUP_BASELINE_MIN < baseline:
        short = _queries(WARMUP_BASELINE_MIN)
        q.update({f"{k}_warmup": short[k] for k in _BASE_KEYS})
    return q


QUERIES = _all_queries(BASELINE_MIN)

_SERVED = 'span_kind=~"SPAN_KIND_SERVER|SPAN_KIND_CONSUMER"'
_ERR = 'status_code="STATUS_CODE_ERROR"'
_CPU = "rate(container_cpu_usage_nanoseconds_total[3m]) / 1e7"
# Per served operation, over a longer window (rare calls), against the same operation's own earlier 30 min.
OP_QUERIES = {
    "calls": f"sum by (service_name, span_name) (increase({_C}{{{_SERVED}}}[{OP_WINDOW_MIN}m]))",
    "failed": f"sum by (service_name, span_name) (increase({_C}{{{_SERVED},{_ERR}}}[{OP_WINDOW_MIN}m]))",
    # Still failing now: the 10-min window otherwise keeps alerting up to 10 min after a fault ended (replay, 22 Nov)
    "failed_recent": f"sum by (service_name, span_name) (increase({_C}{{{_SERVED},{_ERR}}}[3m]))",
    "calls_base": f"sum by (service_name, span_name) (increase({_C}{{{_SERVED}}}[30m] offset {OP_WINDOW_MIN}m))",
    "failed_base": f"sum by (service_name, span_name) (increase({_C}{{{_SERVED},{_ERR}}}[30m] offset {OP_WINDOW_MIN}m))",
}
# Per container, against its own earlier 30 min (the offset keeps a fault that just started out of the average).
RES_QUERIES = {
    "cpu": _CPU,
    "cpu_base": f"avg_over_time(({_CPU})[30m:1m] offset 5m)",
    # 2-min averages: single 10-s spikes (quote, astronomy-db: 70% -> 88% -> 70%) are normal churn, not a leak
    "mem": "avg_over_time(container_memory_percent_ratio[2m])",
    "mem_base": "avg_over_time(container_memory_percent_ratio[30m] offset 10m)",
    "mem_5m_ago": "avg_over_time(container_memory_percent_ratio[2m] offset 5m)",
    "uptime": "container_uptime_seconds",
}


def configure(baseline_min: int) -> None:
    """Baseline length in minutes (default 30). The 10-min warm-up fallback stays on whenever it is shorter."""
    global BASELINE_MIN, QUERIES
    BASELINE_MIN = max(5, int(baseline_min))
    QUERIES = _all_queries(BASELINE_MIN)


@dataclass
class Breach:
    service: str
    text: str
    signal: str = ""
    promql: str = ""  # the query behind the alert, for a Grafana/Prometheus link

    def as_dict(self) -> dict:
        return {"service": self.service, "signal": self.signal, "detail": self.text, "promql": self.promql}


def _promql(signal: str, svc: str) -> str:
    sel = f'service_name="{svc}"'
    if signal == "error_rate":
        return (f'sum(rate({_C}{{{sel},status_code="STATUS_CODE_ERROR"}}[{WINDOW_MIN}m])) / sum(rate({_C}{{{sel}}}[{WINDOW_MIN}m]))')
    return f"histogram_quantile(0.95, sum by (le) (rate({_D}{{{sel}}}[{WINDOW_MIN}m])))"


def _keep(name: str | None) -> bool:
    return bool(name) and name not in telemetry.EXCLUDED_SERVICES and name not in telemetry.OWN_TOOLING


def measure() -> dict[str, dict]:
    """{service: {query key: value, "ops": {operation: {...}}, "res": {container values}}}"""
    table: dict[str, dict] = {}
    for key, q in QUERIES.items():
        rows, _ = telemetry._prom(q)
        for r in rows:
            svc = r["labels"].get("service_name")
            if _keep(svc):
                table.setdefault(svc, {})[key] = r["value"]
    for key, q in OP_QUERIES.items():
        rows, _ = telemetry._prom(q)
        for r in rows:
            svc, op = r["labels"].get("service_name"), r["labels"].get("span_name")
            if _keep(svc) and op:
                table.setdefault(svc, {}).setdefault("ops", {}).setdefault(op, {})[key] = r["value"]
    for key, q in RES_QUERIES.items():
        rows, _ = telemetry._prom(q)
        for r in rows:
            name = r["labels"].get("container_name")
            if _keep(name) and name not in NOT_SHOP_CONTAINERS:
                table.setdefault(name, {}).setdefault("res", {})[key] = r["value"]
    return table


def _resource_breaches(name: str, r: dict) -> list[Breach]:
    if (r.get("uptime") or 0) < MIN_UPTIME_S:
        return []  # just started: start-up CPU/memory is not an incident, and its own average is not known yet
    out = []
    cpu, cpu_b = r.get("cpu"), r.get("cpu_base")
    if cpu is not None and cpu_b is not None and cpu >= CPU_MIN_PCT and cpu >= CPU_FACTOR * cpu_b + CPU_POINTS:
        out.append(Breach(name, f"CPU of {name} is {cpu:.0f}% of a core (its 30-min average {cpu_b:.0f}%)", "container_cpu",
                          f'rate(container_cpu_usage_nanoseconds_total{{container_name="{name}"}}[3m]) / 1e7'))
    mem, mem_b, mem_p = r.get("mem"), r.get("mem_base"), r.get("mem_5m_ago")
    if None not in (mem, mem_b, mem_p) and mem >= MEM_MIN_PCT and mem >= mem_b + MEM_POINTS \
            and mem >= mem_p + MEM_RISE_POINTS:
        out.append(Breach(name, f"memory of {name} is {mem:.0f}% of its limit and rising (its 30-min average {mem_b:.0f}%)",
                          "container_memory", f'container_memory_percent_ratio{{container_name="{name}"}}'))
    return out


def _op_breaches(svc: str, ops: dict) -> list[Breach]:
    for op, o in sorted(ops.items()):
        calls, failed = o.get("calls") or 0.0, o.get("failed") or 0.0
        calls_b, failed_b = o.get("calls_base") or 0.0, o.get("failed_base") or 0.0
        if calls <= 0 or calls_b <= 0 or failed < OP_MIN_FAILED or not o.get("failed_recent"):
            continue  # no history, too few failures to tell from noise, or no failure in the last 3 min (over)
        err, base = failed / calls, failed_b / calls_b
        if err >= OP_ERR_MIN and err >= ERR_FACTOR * base + 0.05:
            sel = f'service_name="{svc}",span_name="{op}"'
            return [Breach(svc, f"operation {op} of {svc} fails {err:.0%} of calls ({failed:.0f} of {calls:.0f} in "
                                f"{OP_WINDOW_MIN} min; before {base:.0%})", "operation_errors",
                           f'sum(rate({_C}{{{sel},{_ERR}}}[{OP_WINDOW_MIN}m])) / sum(rate({_C}{{{sel}}}[{OP_WINDOW_MIN}m]))')]
    return []


def breaches(table: dict[str, dict]) -> list[Breach]:
    out = []
    for svc, m in sorted(table.items()):
        out += _resource_breaches(svc, m.get("res") or {})
        out += _request_breaches(svc, m)
    return out


def _request_breaches(svc: str, m: dict) -> list[Breach]:
    out: list[Breach] = []
    base_min = BASELINE_MIN
    # "history" is the traffic a baseline ago: 0 means the service was down then (24 Nov: product-catalog's warm-up
    # baseline was its own outage, so coming back looked like a flood)
    if "rate_base" not in m or not m.get("history"):
        if "rate_base_warmup" not in m or not m.get("history_warmup"):
            return out  # not even the warm-up baseline yet (right after a start): normal level unknown, no alert
        m = {**m, **{k: m.get(f"{k}_warmup") for k in _BASE_KEYS}}
        base_min = WARMUP_BASELINE_MIN
    if not m.get("served"):
        return out  # a pure traffic source (only client spans): not a service of the shop
    err, base, rate = m.get("err"), m.get("err_base") or 0.0, m.get("rate") or 0.0
    prev = m.get("err_prev")
    solid = err is not None and err >= ERR_MIN and err * rate * WINDOW_MIN * 60 >= ERR_MIN_CALLS \
        and rate * WINDOW_MIN * 60 >= MIN_REQUESTS
    if solid and err >= ERR_FACTOR * base + ERR_POINTS:
        out.append(Breach(svc, f"error rate of {svc} is {err:.1%} (its {base_min}-min baseline {base:.1%})",
                          "error_rate", _promql("error_rate", svc)))
    elif solid and prev is not None and err >= ERR_FACTOR * prev + ERR_POINTS \
            and err * rate * WINDOW_MIN * 60 >= JUMP_MIN_CALLS:
        out.append(Breach(svc, f"error rate of {svc} jumped to {err:.1%} (previous {WINDOW_MIN} min: {prev:.1%})",
                          "error_rate", _promql("error_rate", svc)))
    p95, p95b, peak = m.get("p95"), m.get("p95_base"), m.get("p95_peak")
    enough = rate * WINDOW_MIN * 60 >= MIN_REQUESTS  # a rate or p95 from a handful of requests is noise
    if p95 is not None and p95b and enough and p95 >= P95_MIN_MS and p95 >= P95_FACTOR * p95b \
            and (peak is None or rate * WINDOW_MIN * 60 >= P95_PEAK_MAX_REQUESTS or p95 >= P95_PEAK_FACTOR * peak):
        out.append(Breach(svc, f"p95 latency of {svc} is {p95 / 1000:.1f} s (its {base_min}-min baseline {p95b / 1000:.1f} s)",
                          "p95_latency", _promql("p95_latency", svc)))
    rate_b, rate_pk = m.get("rate_base") or 0.0, m.get("rate_peak")
    if rate_b >= SURGE_MIN_BASE_RPS and rate >= SURGE_FACTOR * rate_b and rate - rate_b >= SURGE_MIN_RPS \
            and (rate_pk is None or rate >= SURGE_PEAK_FACTOR * rate_pk):
        out.append(Breach(svc, f"traffic to {svc} is {rate:.1f} req/s (its {base_min}-min baseline {rate_b:.1f} req/s)",
                          "traffic_surge", f'sum(rate({_C}{{service_name="{svc}"}}[{WINDOW_MIN}m]))'))
    if not any(b.signal == "error_rate" for b in out):  # the service-wide error rule already covers its operations
        out += _op_breaches(svc, m.get("ops") or {})
    return out


class Detector:
    """Stateful: consecutive-check filter and per-service cooldown."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.streak: dict[str, int] = {}
        self.quiet_until: dict[str, float] = {}

    def check(self, found: list[Breach]) -> list[Breach]:
        """Breaches that should raise an alert now."""
        now, seen = self.clock(), {b.service for b in found}
        self.streak = {s: self.streak.get(s, 0) + 1 for s in seen}
        ready = [b for b in found if self.streak[b.service] >= CONSECUTIVE and self.quiet_until.get(b.service, 0) <= now]
        for b in ready:
            self.quiet_until[b.service] = now + COOLDOWN_S
        return ready


def alert_text(ready: list[Breach]) -> str:
    return "Threshold alert: " + "; ".join(b.text for b in ready)


def new_incident_id() -> str:
    return datetime.now(timezone.utc).strftime("INC-%Y%m%d-%H%M%S")


def watch(investigate, interval_s: float = 15, once: bool = False, post_alert=None) -> int:
    """investigate(incident_id, symptom, breaches) runs one incident; post_alert(incident_id, text) shows the alert."""
    det = Detector()
    print(f"Watching the demo's metrics every {interval_s:.0f} s (Ctrl+C to stop). Thresholds: error rate >= "
          f"{ERR_MIN:.0%} and > {ERR_FACTOR:.0f}x baseline; p95 >= {P95_MIN_MS / 1000:.0f} s and >= {P95_FACTOR:.0f}x baseline.")
    try:
        while True:
            try:
                found = breaches(measure())
            except Exception as exc:  # Prometheus briefly unreachable: keep watching
                print(f"[{datetime.now():%H:%M:%S}] metrics unavailable: {type(exc).__name__}")
                found = []
            ready = det.check(found)
            stamp = f"[{datetime.now():%H:%M:%S}]"
            if once:
                print(f"{stamp} over threshold now: {[b.text for b in found] or 'nothing'}")
                return 0
            if ready:
                inc_id, text = new_incident_id(), alert_text(ready)
                print(f"{stamp} ALERT {inc_id}: {text}\n{stamp} starting investigation automatically...")
                if post_alert:
                    print(post_alert(inc_id, text))
                investigate(inc_id, text, ready)
                print(f"[{datetime.now():%H:%M:%S}] investigation {inc_id} finished; watching again.")
            elif found:
                print(f"{stamp} over threshold (waiting for a 2nd check or in cooldown): {[b.service for b in found]}")
            time.sleep(interval_s)
    except KeyboardInterrupt:
        print("stopped watching")
        return 0
