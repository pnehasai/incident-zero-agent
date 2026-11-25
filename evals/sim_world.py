"""Simulated telemetry backends for end-to-end evaluation (no demo, no Docker).

A Scenario describes a small world: per-service span metrics (now / previous window / 30-min baseline), container
resources, log messages with first/last seen times, and traces as call paths. SimWorld answers the agent's REAL
HTTP requests (the fixed PromQL templates, Jaeger v3 API and OpenSearch PPL queries in telemetry.py, resources.py
and detector.py) from that description, through an httpx MockTransport. Everything above the HTTP layer runs
unchanged: detector, tools, evidence capture, candidates, LLM loop, guardrails, runbooks, report.

The agent never sees the scenario's expected answer; the runner keeps it apart.
"""
from __future__ import annotations

import copy
import json
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

# ---------------------------------------------------------------- default world (shape of the shop under load)
# service: (requests/s, error ratio, p95 ms) in normal operation
BASE_SERVICES: dict[str, tuple[float, float, float]] = {
    "frontend-proxy": (6.0, 0.004, 160), "frontend": (5.0, 0.004, 130), "checkout": (0.3, 0.0, 320),
    "payment": (0.3, 0.0, 22), "cart": (2.0, 0.0, 14), "product-catalog": (6.0, 0.0, 6), "currency": (4.0, 0.0, 3),
    "shipping": (0.6, 0.0, 30), "quote": (0.3, 0.0, 8), "email": (0.3, 0.0, 45), "recommendation": (1.5, 0.0, 25),
    "ad": (1.5, 0.0, 7), "image-provider": (1.0, 0.0, 3), "accounting": (0.3, 0.0, 12),
    "fraud-detection": (0.3, 0.0, 15), "load-generator": (5.0, 0.03, 220),
}
# container: (cpu % of one core, memory % of limit, MB used); every container up for an hour by default
BASE_CONTAINERS: dict[str, tuple[float, float, float]] = {
    **{s: (6.0, 40.0, 80.0) for s in BASE_SERVICES},
    "checkout": (55.0, 88.0, 18.0), "product-catalog": (60.0, 84.0, 17.0),  # the demo runs these near their limits
    "valkey-cart": (2.0, 10.0, 12.0), "postgresql": (3.0, 20.0, 60.0), "otel-collector": (15.0, 50.0, 120.0),
    "opensearch": (30.0, 90.0, 920.0), "prometheus": (10.0, 40.0, 300.0), "jaeger": (8.0, 30.0, 200.0),
}
# normal call paths (healthy traces are sampled from these)
BASE_PATHS = [
    ["frontend-proxy", "frontend", "product-catalog"], ["frontend-proxy", "frontend", "cart"],
    ["frontend-proxy", "frontend", "checkout", "payment"], ["frontend-proxy", "frontend", "checkout", "shipping", "quote"],
    ["frontend-proxy", "frontend", "checkout", "email"], ["frontend-proxy", "frontend", "recommendation", "product-catalog"],
    ["frontend-proxy", "frontend", "ad"], ["frontend-proxy", "frontend", "currency"],
]
# baseline log noise: present all along (the demo's load generator calls a component that is not running)
BASE_LOGS = [{"service": "load-generator", "severity": "WARN", "body": "request to agent failed: connection refused",
              "count_now": 9, "first_min_ago": 33, "last_min_ago": 0}]

PHASES = ("now", "before", "baseline")


@dataclass
class Scenario:
    id: str
    description: str
    expected: dict[str, Any]
    # {service: {"now": {"rate"|"err"|"p95": v}, "before": {...}, "baseline": {...}}} overrides of BASE_SERVICES
    metrics: dict[str, dict[str, dict[str, float]]] = field(default_factory=dict)
    # extra per-operation series: [{"service", "op", "now": {...}, "before": {...}, "baseline": {...}}]
    operations: list[dict[str, Any]] = field(default_factory=list)
    # {container: {"cpu", "cpu_prior", "mem", "mem_prior", "mem_prior_max", "mem_growth", "uptime", "restarts",
    #              "mem_mb", "mem_slope_mb_min"}} overrides
    containers: dict[str, dict[str, float]] = field(default_factory=dict)
    logs: list[dict[str, Any]] = field(default_factory=list)  # like BASE_LOGS
    # fault traces: [{"path": [...], "count": n, "error_at": svc, "message": str, "slow_at": svc, "slow_ms": n,
    #                 "unanswered": bool (last service in path records nothing), "timeout": bool}]
    traces: list[dict[str, Any]] = field(default_factory=list)
    manual_symptom: str = ""  # used when the detector (correctly) raises no alert


def _hex(n: int) -> str:
    return secrets.token_hex(n)


class SimWorld:
    def __init__(self, sc: Scenario, rename: dict[str, str] | None = None):
        self.sc = sc
        self.now = datetime.now(timezone.utc)
        self.rename = rename or {}
        self.services = {s: {p: dict(zip(("rate", "err", "p95"), v)) for p in PHASES} for s, v in BASE_SERVICES.items()}
        for svc, phases in sc.metrics.items():
            row = self.services.setdefault(svc, {p: {"rate": 0.0, "err": 0.0, "p95": 0.0} for p in PHASES})
            for p, vals in phases.items():
                row[p].update(vals)
        self.containers: dict[str, dict[str, float]] = {}
        for c, (cpu, mem, mb) in BASE_CONTAINERS.items():
            self.containers[c] = {"cpu": cpu, "cpu_prior": cpu, "mem": mem, "mem_prior": mem - 1,
                                  "mem_prior_max": mem + (6 if mem > 80 else 2), "mem_growth": 0.0, "uptime": 3600.0,
                                  "restarts": 0.0, "mem_mb": mb, "mem_slope_mb_min": 0.0}
        for c, vals in sc.containers.items():
            self.containers.setdefault(c, dict(next(iter(self.containers.values())))).update(vals)
        self.logs = [{**m, "body": self.text(m["body"])} for m in BASE_LOGS + sc.logs]
        self.traces = self._build_traces()

    def text(self, s: str) -> str:
        """Rename service names inside free text too (e.g. 'lookup payment: no such host')."""
        for old in sorted(self.rename, key=len, reverse=True):
            s = re.sub(rf"(?<![\w-]){re.escape(old)}(?![\w-])", self.rename[old], s, flags=re.IGNORECASE)
        return s

    # ------------------------------------------------------------------ names (optional consistent renaming)
    def n(self, name: str) -> str:
        return self.rename.get(name, name)

    def _un(self, name: str) -> str:
        inv = {v: k for k, v in self.rename.items()}
        return inv.get(name, name)

    # ------------------------------------------------------------------ Prometheus
    def _phase(self, q: str) -> str:
        if "[30m" in q:
            return "baseline"
        return "before" if "offset" in q else "now"

    def _ops(self, phase: str) -> list[tuple[str, str, dict]]:
        out = []
        extra = {(o["service"]) for o in self.sc.operations}
        for svc, row in self.services.items():
            if svc not in extra:
                out.append((svc, f"{svc}.Handle", row[phase]))
        for o in self.sc.operations:
            vals = dict(self.services[o["service"]][phase])
            vals.update(o.get(phase) or {})
            out.append((o["service"], o["op"], vals))
        return out

    def _span_metric(self, q: str) -> list[tuple[dict, float]]:
        phase = self._phase(q)
        single = re.search(r'service_name="([^"]+)"', q)
        svc_filter = self._un(single.group(1)) if single else None
        by_op = "span_name" in q
        if "histogram_quantile" in q:
            kind = "p95"
        elif "STATUS_CODE_ERROR" in q and "/" in q:
            kind = "err"
        elif "STATUS_CODE_ERROR" in q:
            kind = "errors_per_s"
        else:
            kind = "rate"
        rows: list[tuple[str, str | None, dict]] = (
            [(s, op, v) for s, op, v in self._ops(phase)] if by_op else
            [(s, None, r[phase]) for s, r in self.services.items()])
        out = []
        for svc, op, v in rows:
            if svc_filter and svc != svc_filter:
                continue
            if v["rate"] <= 0:
                continue  # no traffic: no series
            val = {"p95": v["p95"], "err": v["err"], "errors_per_s": v["err"] * v["rate"], "rate": v["rate"]}[kind]
            if kind == "errors_per_s" and val <= 0:
                continue
            labels: dict[str, str] = {}
            if by_op:
                labels["span_name"] = op
                if re.search(r"by \((le, )?service_name, span_name\)", q):
                    labels["service_name"] = self.n(svc)
            elif "by (service_name)" in q or "by (le, service_name)" in q:
                labels["service_name"] = self.n(svc)
            out.append((labels, val))
        return out

    def _container_metric(self, q: str) -> list[tuple[dict, float]]:
        single = re.search(r'container_name="([^"]+)"', q)
        only = self._un(single.group(1)) if single else None
        if "timestamp(" in q:
            key = "last_seen_age"  # seconds since the container's last sample (healthy: a few seconds)
        elif "resets(" in q:
            key = "restarts"
        elif "max_over_time" in q:
            key = "mem_prior_max"
        elif "avg_over_time" in q:
            key = "cpu_prior" if "cpu" in q else "mem_prior"
        elif "deriv(" in q and "*" in q:
            key = "mem_growth"
        elif "deriv(" in q:
            key = "mem_slope"
        elif "container_cpu_usage_nanoseconds_total" in q:
            key = "cpu"
        elif "container_memory_percent_ratio" in q:
            key = "mem"
        elif "__name__=~\"container_memory" in q:
            key = "mem_series"
        elif "container_memory_usage_total_bytes" in q:
            key = "mem_bytes"
        elif "container_uptime" in q:
            key = "uptime"
        else:
            return []
        out = []
        for c, v in self.containers.items():
            if only and c != only:
                continue
            labels = {"container_name": self.n(c)}
            if key == "mem_series":
                out.append(({**labels, "__name__": "container_memory_usage_total_bytes"}, v["mem_mb"] * 1e6))
                out.append(({**labels, "__name__": "container_memory_percent_ratio"}, v["mem"]))
            elif key == "mem_bytes":
                out.append((labels, v["mem_mb"] * 1e6))
            elif key == "last_seen_age":
                out.append((labels, v.get("last_seen_age", 5.0)))
            elif key == "mem_slope":
                out.append((labels, v["mem_slope_mb_min"] * 1e6 / 60))
            else:
                out.append((labels, v[key]))
        return out

    def prom_query(self, q: str) -> dict:
        if "container_" in q:
            rows = self._container_metric(q)
        elif "traces_span_metrics" in q:
            rows = self._span_metric(q)
        else:
            rows = []
        if q.startswith("count("):
            rows = [({}, float(len(self.services)))]
        ts = self.now.timestamp()
        return {"status": "success", "data": {"resultType": "vector", "result": [
            {"metric": labels, "value": [ts, str(v)]} for labels, v in rows]}}

    def label_values(self, label: str) -> list[str]:
        names = self.services if label == "service_name" else self.containers
        return sorted(self.n(s) for s in names)

    # ------------------------------------------------------------------ Jaeger
    def _build_traces(self) -> list[dict]:
        """Each trace: {"trace_id", "spans": [(service, span dict)], "services": set, "error": bool}."""
        specs = [{"path": p, "count": 3} for p in BASE_PATHS] + [dict(t) for t in self.sc.traces]
        traces = []
        for spec in specs:
            for i in range(int(spec.get("count", 1))):
                traces.append(self._trace(spec, age_s=10 + 13 * i + len(traces) % 7))
        return traces

    def _trace(self, spec: dict, age_s: float) -> dict:
        path, err_at, slow_at = spec["path"], spec.get("error_at"), spec.get("slow_at")
        unanswered, timeout = bool(spec.get("unanswered")), bool(spec.get("timeout"))
        recorded = path[:-1] if unanswered else path
        tid = _hex(16)
        end_ns = int((self.now.timestamp() - age_s) * 1e9)
        # durations: each hop 3 ms of own work, the slow service adds slow_ms
        own = {s: 3.0 for s in path}
        if slow_at:
            own[slow_at] = float(spec.get("slow_ms", 3000))
        dur = {}
        for idx in range(len(path) - 1, -1, -1):
            dur[path[idx]] = own[path[idx]] + (dur[path[idx + 1]] + 1 if idx + 1 < len(path) else 0)
        if unanswered:
            dur[path[-1]] = 0
            dur[path[-2]] = own[path[-2]] + float(spec.get("wait_ms", 2000))
        failing = set()
        if err_at in path:
            failing = set(path[: path.index(err_at) + 1])
        if unanswered and spec.get("error_at", path[-2]) == path[-2]:
            failing = set(path[:-1])
        if timeout:
            failing = {path[0]}
        msg = self.text(spec.get("message", ""))
        spans, parent = [], ""
        start_ns = end_ns - int(dur[path[0]] * 1e6)
        for idx, svc in enumerate(recorded):
            sid = _hex(8)
            is_err = svc in failing
            own_msg = msg if svc == (err_at or (path[-2] if unanswered else None)) else (
                "deadline exceeded while waiting for downstream" if timeout and idx == 0 else
                (f"call to {path[idx + 1]} failed" if is_err else ""))
            server = self._span(tid, sid, parent, f"{self.n(svc)}.Handle", 2, start_ns, dur[svc], is_err, own_msg,
                                {"rpc.service": self.n(svc)} if idx else {"http.status_code": 504 if timeout else
                                                                         (500 if is_err else 200)})
            spans.append((svc, server))
            if idx + 1 < len(path):
                cid = _hex(8)
                callee = path[idx + 1]
                child_dur = dur[callee] + 1 if not (unanswered and idx + 1 == len(path) - 1) else float(spec.get("wait_ms", 2000))
                child_err = callee in failing or (unanswered and idx + 1 == len(path) - 1)
                cmsg = (msg if unanswered and idx + 1 == len(path) - 1 else (f"{callee} returned an error" if child_err else ""))
                client = self._span(tid, cid, sid, f"call {self.n(callee)}", 3, start_ns + 1_000_000, child_dur,
                                    child_err, cmsg, {"rpc.service": self.n(callee), "server.address": self.n(callee)})
                spans.append((svc, client))
                parent = cid
                start_ns += 1_000_000
        return {"trace_id": tid, "spans": spans, "services": {s for s, _ in spans},
                "error": any(sp["status"].get("code") == 2 for _, sp in spans)}

    @staticmethod
    def _span(tid, sid, parent, name, kind, start_ns, dur_ms, err, msg, attrs) -> dict:
        return {"traceId": tid, "spanId": sid, "parentSpanId": parent, "name": name, "kind": kind,
                "startTimeUnixNano": str(start_ns), "endTimeUnixNano": str(start_ns + int(dur_ms * 1e6)),
                "status": {"code": 2, "message": msg} if err else {},
                "attributes": [{"key": k, "value": {"intValue" if isinstance(v, int) else "stringValue": v}}
                               for k, v in attrs.items()]}

    def jaeger_traces(self, params: dict[str, str]) -> dict | None:
        svc = self._un(params.get("query.service_name", ""))
        want_err = "error" in params.get("query.attributes", "")
        limit = int(params.get("query.num_traces", 20))
        picked = [t for t in self.traces if svc in t["services"] and (t["error"] or not want_err)][:limit]
        if not picked:
            return None
        by_svc: dict[str, list[dict]] = {}
        for t in picked:
            for s, sp in t["spans"]:
                by_svc.setdefault(s, []).append(sp)
        return {"result": {"resourceSpans": [
            {"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": self.n(s)}}]},
             "scopeSpans": [{"spans": copy.deepcopy(spans)}]} for s, spans in by_svc.items()]}}

    # ------------------------------------------------------------------ OpenSearch PPL
    _SEV = {"DEBUG": 5, "INFO": 9, "WARN": 13, "ERROR": 17, "FATAL": 21}

    def _ts(self, minutes_ago: float) -> str:
        return (self.now - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M:%S.%f")[:23]

    def ppl(self, query: str) -> dict:
        if "stats count() as n" in query:
            hist = "min(`@timestamp`)" in query
            rows = []
            for m in self.logs:
                n = m.get("count_hist", m["count_now"] * 6) if hist else m["count_now"]
                if not hist and (m["count_now"] <= 0 or m["last_min_ago"] > 3):
                    continue
                row = {"n": n, "resource.service.name": self.n(m["service"]), "severity.text": m["severity"],
                       "body": m["body"]}
                if hist:
                    row.update(first=self._ts(m["first_min_ago"]), last=self._ts(m["last_min_ago"]))
                rows.append(row)
            cols = ["n", *(["first", "last"] if hist else []), "resource.service.name", "severity.text", "body"]
            return {"schema": [{"name": c} for c in cols], "datarows": [[r.get(c) for c in cols] for r in rows]}
        m = re.search(r"resource\.service\.name = '([^']+)'", query)
        svc = self._un(m.group(1)) if m else ""
        sev_min = int(re.search(r"`severity.number` >= (\d+)", query).group(1)) if "severity.number" in query else 0
        lines = []
        for msg in self.logs:
            if msg["service"] != svc or msg["count_now"] <= 0 or msg["last_min_ago"] > 3:
                continue
            if self._SEV[msg["severity"]] < sev_min:
                continue
            for i in range(min(int(msg["count_now"]), 10)):
                lines.append((msg["last_min_ago"] + i * 0.2, msg))
        lines.sort(key=lambda t: t[0])
        cols = ["@timestamp", "severity", "body", "traceId", "_id"]
        return {"schema": [{"name": c} for c in cols], "datarows": [
            [self._ts(ago), {"number": self._SEV[m["severity"]], "text": m["severity"]}, m["body"], _hex(16), _hex(10)]
            for ago, m in lines[:40]]}

    # ------------------------------------------------------------------ transport
    def handler(self, request: httpx.Request) -> httpx.Response:
        url = urlparse(str(request.url))
        params = {k: v[0] for k, v in parse_qs(url.query).items()}
        path = url.path
        if path == "/api/v1/query":
            return httpx.Response(200, json=self.prom_query(params.get("query", "")))
        if path.startswith("/api/v1/label/") and path.endswith("/values"):
            return httpx.Response(200, json={"status": "success", "data": self.label_values(path.split("/")[4])})
        if path == "/-/ready":
            return httpx.Response(200, text="ready")
        if path == "/jaeger/ui/api/v3/services":
            return httpx.Response(200, json={"services": sorted({self.n(s) for t in self.traces for s in t["services"]})})
        if path == "/jaeger/ui/api/v3/traces":
            body = self.jaeger_traces(params)
            return httpx.Response(200, json=body) if body else httpx.Response(404, json={"error": "no traces"})
        if path == "/_plugins/_ppl":
            return httpx.Response(200, json=self.ppl(json.loads(request.content)["query"]))
        if path == "/_cluster/health":
            return httpx.Response(200, json={"status": "green"})
        return httpx.Response(404, json={"error": f"sim: no route {path}"})


def install(world: SimWorld, telemetry_mod, resources_mod) -> callable:
    """Route the agent's HTTP traffic to the simulated world. Returns an uninstall function."""
    transport = httpx.MockTransport(world.handler)
    saved = (telemetry_mod._client, resources_mod.httpx.Client, telemetry_mod.OPENSEARCH_URL, telemetry_mod._inventory)
    telemetry_mod._client = httpx.Client(transport=transport)
    real_client = httpx.Client

    def client_factory(*args, **kwargs):
        kwargs.pop("transport", None)
        return real_client(*args, transport=transport, **kwargs)

    class _HttpxShim:  # resources.py calls httpx.Client(...); everything else on httpx stays real
        def __getattr__(self, name):
            return client_factory if name == "Client" else getattr(httpx, name)

    resources_mod.httpx = _HttpxShim()
    telemetry_mod.OPENSEARCH_URL = "http://sim-opensearch:9200"
    telemetry_mod._inventory = (0.0, [])

    def uninstall() -> None:
        telemetry_mod._client = saved[0]
        resources_mod.httpx = httpx
        telemetry_mod.OPENSEARCH_URL = saved[2]
        telemetry_mod._inventory = (0.0, [])

    return uninstall
