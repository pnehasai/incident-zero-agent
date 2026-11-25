"""Operator-side setup checks, run by the CLI before an investigation (never by the model).

- OpenSearch gets a new random host port every time the demo restarts, so a fixed OPENSEARCH_URL in .env goes
  stale and the logs backend silently disappears. resolve_opensearch() keeps a working URL, otherwise asks
  Docker for the current port (`docker port opensearch 9200`) and points telemetry at it.
- doctor() checks every prerequisite of `cli.py watch` / `investigate` and says how to fix what is missing.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess

import httpx

import telemetry


def _reachable(url: str) -> bool:
    try:
        return httpx.get(f"{url}/_cluster/health", timeout=3).status_code == 200
    except httpx.HTTPError:
        return False


def _docker_port(container: str, port: int) -> str | None:
    if not shutil.which("docker"):
        return None
    try:
        out = subprocess.run(["docker", "port", container, str(port)], capture_output=True, text=True, timeout=8).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r":(\d+)\s*$", out.strip().splitlines()[0]) if out.strip() else None
    return m.group(1) if m else None


def resolve_opensearch() -> str:
    """Make telemetry.OPENSEARCH_URL point at the running OpenSearch. Returns a one-line status."""
    current = telemetry.OPENSEARCH_URL
    if current and _reachable(current):
        return f"OpenSearch: {current}"
    port = _docker_port("opensearch", 9200)
    if port:
        url = f"http://localhost:{port}"
        if _reachable(url):
            telemetry.OPENSEARCH_URL = url
            os.environ["OPENSEARCH_URL"] = url
            was = f" (was {current or 'unset'}; update .env to skip this lookup)" if url != current else ""
            return f"OpenSearch: {url}{was}"
    return (f"OpenSearch: NOT REACHABLE at {current or '(unset)'}; log tools will report 'unavailable'. "
            "Find the port with: docker port opensearch 9200")


def _history_minutes() -> float | None:
    """How far back Prometheus has span metrics (the detector and overview need ~30 min of normal traffic)."""
    try:
        for minutes in (30, 25, 20, 15, 10, 5, 2, 1):
            rows, _ = telemetry._prom(f"count(traces_span_metrics_calls_total offset {minutes}m)")
            if rows and rows[0]["value"] > 0:
                return float(minutes)
        return 0.0
    except Exception:  # noqa: BLE001 - doctor reports, never raises
        return None


def doctor(provider: str, model: str, check_llm) -> int:
    """Print PASS/WARN/FAIL per prerequisite. Returns 0 if the agent can run (warnings allowed)."""
    fails, warming = 0, None

    def line(level: str, what: str, detail: str) -> None:
        nonlocal fails
        fails += level == "FAIL"
        print(f"[{level:4}] {what:22} {detail}")

    backends = telemetry.check_backends()
    for name in ("prometheus", "jaeger"):
        ok = backends[name].endswith("OK")
        line("PASS" if ok else "FAIL", name, backends[name] + ("" if ok else "  -> is the demo running? docker compose ps"))
    os_status = resolve_opensearch()
    line("WARN" if "NOT REACHABLE" in os_status else "PASS", "opensearch (logs)", os_status.split(": ", 1)[1])
    inv = telemetry.service_inventory(refresh=True)
    line("PASS" if len(inv) >= 5 else "FAIL", "services in telemetry",
         f"{len(inv)} seen" + ("" if len(inv) >= 5 else "  -> wait for the load generator, then retry"))
    import detector
    need = min(detector.BASELINE_MIN, detector.WARMUP_BASELINE_MIN)  # alerts start on the 10-min warm-up baseline
    hist = _history_minutes()
    if hist is not None and hist < need:
        warming = need - hist
    if hist is None:
        line("WARN", "metric history", "could not be measured")
    else:
        line("PASS" if hist >= need else "WARN", "metric history",
             f">= {hist:.0f} min of span metrics" + ("" if hist >= need else
             f"  -> the detector needs {need} min of normal traffic; no alerts until then"))
    if not model:
        line("FAIL", "LLM", "LLM_MODEL not set (.env or --model)")
    else:
        rc = check_llm(provider, model)
        fallback = os.getenv("LLM_FALLBACK_MODEL", "")
        if rc == 0:
            line("PASS", "LLM", f"{provider} / {model}")
        elif fallback and fallback != model and check_llm(provider, fallback) == 0:
            line("WARN", "LLM", f"{model} failed (see above); fallback {fallback} works, the agent switches to it")
        else:
            line("FAIL", "LLM", f"{provider} / {model}" + (f" and fallback {fallback}" if fallback else "")
                 + " failed: see the error above (503 = provider overloaded, retry in a few minutes)")
    url = os.getenv("GRAFANA_URL", "http://localhost:3001").rstrip("/")
    if not url:
        line("PASS", "agent Grafana", "disabled (GRAFANA_URL empty)")
    else:
        try:
            ok = httpx.get(f"{url}/api/health", timeout=3).status_code == 200
        except httpx.HTTPError:
            ok = False
        line("PASS" if ok else "WARN", "agent Grafana", url + ("" if ok else
             "  -> optional; start with: docker compose -f grafana/compose.yaml up -d"))
    if fails:
        print(f"{fails} blocking problem(s): fix them before running the agent.")
    elif warming is not None:
        print(f"Warming up: manual 'investigate' works now; 'watch' cannot raise alerts for about {warming:.0f} more "
              f"min (it needs {need} min of normal traffic as a baseline).")
    else:
        print("Ready: 'watch' and 'investigate' can both run.")
    return 1 if fails else 0
