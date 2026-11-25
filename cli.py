"""Incident agent CLI: alert -> bounded LLM tool loop (LangChain) -> evidence-backed diagnosis -> (optional)
human-approved remediation through the Action API -> measured recovery verification -> report + Grafana.

    python cli.py doctor                       # check every prerequisite (backends, history, LLM, Grafana)
    python cli.py check-llm                    # one tiny request: is the key/model in .env working?
    python cli.py action-api init-token        # once: create the shared Action API token in .env
    python cli.py action-api serve [--executor docker|simulate]    # the only process that can restart containers
    python cli.py watch [--execute] [--diagnose-only]              # detect -> investigate -> propose -> approve -> act -> verify
    python cli.py investigate --incident-id INC-001 --symptom "..." [--service checkout]
    python cli.py investigate ... --respond --action-api [--execute]   # same chain for one manual incident
    python cli.py kill-switch on|off|status    # blocks every state-changing action
    python cli.py audit [--server] [--incident-id ID] | report <incident-id> | reset-dashboard | endpoints
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import queue
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")
os.environ.setdefault("TELEMETRY_TIMEOUT_S", "5")  # must be set before telemetry is imported

from actions import CatalogError, HttpActionClient, load_catalog  # noqa: E402
from contracts import ModelError  # noqa: E402
from integration import EvidenceRegistry, make_telemetry_runner  # noqa: E402
from models import Incident, IncidentStatus, TimelineEvent, TimeWindow  # noqa: E402
from orchestrator import (  # noqa: E402
    RECOVERY_NOT_PROVIDED, Dependencies, IncidentInputError, InvestigationConfig, investigate,
)
from reasoning import DEFAULT_PROVIDER, LLMReasoner, _text as reply_text, make_chat_model  # noqa: E402
from report_io import write_report  # noqa: E402
from runbooks import action_payload  # noqa: E402
from grafana_publish import publish as publish_to_grafana, reload_dashboards as reload_grafana_dashboards  # noqa: E402
from grafana_dashboards import build as build_grafana_dashboards  # noqa: E402

KILL_SWITCH_FILE = Path(os.getenv("KILL_SWITCH_FILE", HERE / "KILL_SWITCH"))
DEFAULT_ACTION_API_URL = "http://127.0.0.1:8787"
ACTION_API_STATE_DIR = Path(os.getenv("ACTION_API_STATE_DIR", HERE / "runs" / "action_api"))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="incident-agent")
    sub = p.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("investigate", help="investigate one live incident; optionally plan and gate a remediation")
    i.add_argument("--incident-id", required=True)
    i.add_argument("--symptom", required=True)
    i.add_argument("--service", help="affected service, if known")
    i.add_argument("--source", default="manual-cli")
    _incident_args(i)
    w = sub.add_parser("watch", help="watch the demo's metrics; on an alert run the whole chain (investigate, "
                                     "propose, approval page, Action API, verification, Grafana)")
    w.add_argument("--interval-s", type=float, default=15, help="seconds between detector checks")
    w.add_argument("--once", action="store_true", help="one check: print what is over threshold now, then exit")
    w.add_argument("--diagnose-only", action="store_true", help="stop after the diagnosis (no Action API needed)")
    w.add_argument("--baseline-min", type=int, default=None,
                   help="detector baseline in minutes (default 30; until that much history exists a 10-min one is used)")
    _incident_args(w)
    w.set_defaults(respond=True, action_api=True, approval_channel="page", approval_ttl_s=600)
    dr = sub.add_parser("doctor", help="check backends, metric history, LLM and Grafana before a demo")
    _llm_args(dr)
    c = sub.add_parser("check-llm", help="send one tiny request to the configured LLM (verifies key and model)")
    _llm_args(c)
    r = sub.add_parser("reset-dashboard", help="start the Grafana demo clean: archive old runs (not deleted), "
                                               "remove the agent's old annotations, rebuild empty pages")
    r.add_argument("--out", default="runs")
    _rest_of_parser(sub)
    return p


def _llm_args(x: argparse.ArgumentParser) -> None:
    x.add_argument("--provider", default=os.getenv("LLM_PROVIDER", DEFAULT_PROVIDER),
                   help="LangChain provider: google_genai (default), groq, openai, ollama, anthropic")
    x.add_argument("--model", default=os.getenv("LLM_MODEL", ""))


def _incident_args(i: argparse.ArgumentParser) -> None:
    i.add_argument("--window-minutes", type=int, default=3,
                   help="incident observation window ending now (short: incidents may follow each other minutes apart)")
    i.add_argument("--recovery-condition", default=RECOVERY_NOT_PROVIDED)
    _llm_args(i)
    i.add_argument("--out", default="runs")
    i.add_argument("--deadline-s", type=float, default=240)
    i.add_argument("--max-tool-calls", type=int, default=16)
    i.add_argument("--max-model-calls", type=int, default=8)
    i.add_argument("--request-timeout-s", type=float, default=20,
                   help="per LLM request; a stuck request fails fast and goes to retry/fallback")
    i.add_argument("--max-total-tokens", type=int, default=150000)
    r = i.add_argument_group("remediation (investigate: off unless --respond; watch: on unless --diagnose-only)")
    r.add_argument("--respond", action="store_true", help="plan a remediation, ask for approval, then dry-run or execute")
    r.add_argument("--execute", action="store_true", help="LIVE: send the approved request (default is dry-run)")
    r.add_argument("--action-catalog", default=os.getenv("ACTION_API_CATALOG", ""),
                   help="Action API catalog JSON (only without --action-api)")
    r.add_argument("--action-api", action="store_true",
                   help="use the team Action API (ACTION_API_URL, ACTION_API_TOKEN): catalog, validation, server-issued approval")
    r.add_argument("--approver", default=os.getenv("INCIDENT_AGENT_APPROVER", ""), help="name recorded with the approval")
    r.add_argument("--approval-ttl-s", type=float, default=300, help="seconds the human has to approve (max 900)")
    r.add_argument("--approval-channel", choices=["terminal", "page"], default="terminal",
                   help="terminal: type approve CODE here; page: approve on the Action API page (needs --action-api)")
    # 2-min windows: query_metrics needs >= 2 min (rate() samples), and a 1-min window measured nothing live (5 Nov:
    # last-seen and error rate both "unmeasurable", recovery UNVERIFIED although the restart worked).
    r.add_argument("--settle-s", type=float, default=120,
                   help="wait after dispatch before the first verification sample (>= the verify window)")
    r.add_argument("--interval-s-verify", dest="verify_interval_s", type=float, default=30)
    r.add_argument("--verify-window-min", type=int, default=2, choices=range(2, 11), metavar="2-10",
                   help="rate window of each verification sample")
    r.add_argument("--samples", type=int, default=2)
    i.add_argument("--grafana", action="store_true", help="also publish the API-built root-cause/verification dashboards")


def _rest_of_parser(sub) -> None:
    e = sub.add_parser("endpoints", help="show each backend, the tools that use it, reachability and browser URLs")
    e.add_argument("--write-env", action="store_true", help="discover published ports with `docker port` and update .env")
    k = sub.add_parser("kill-switch", help="operator kill switch for all state-changing actions")
    k.add_argument("state", choices=["on", "off", "status"])
    c = sub.add_parser("catalog", help="validate and show the Action API catalog")
    c.add_argument("--action-catalog", default=os.getenv("ACTION_API_CATALOG", ""))
    a = sub.add_parser("action-api", help="run or query the team Action API")
    asub = a.add_subparsers(dest="api_cmd", required=True)
    s = asub.add_parser("serve", help="start the Action API (the only process that can restart containers)")
    s.add_argument("--host", default=os.getenv("ACTION_API_HOST", "127.0.0.1"))
    s.add_argument("--port", type=int, default=int(os.getenv("ACTION_API_PORT", "8787")))
    s.add_argument("--executor", choices=["simulate", "docker"], default=os.getenv("ACTION_API_EXECUTOR", "simulate"))
    s.add_argument("--state-dir", default=str(ACTION_API_STATE_DIR))
    s.add_argument("--bind-all", action="store_true", help="allow a non-loopback host (not recommended)")
    asub.add_parser("init-token", help="add ACTION_API_TOKEN/ACTION_API_URL to .env if absent (never overwrites)")
    asub.add_parser("health")
    asub.add_parser("operations")
    asub.add_parser("actions")
    asub.add_parser("services", help="read-only system state (container state/health, load generator)")
    sh = asub.add_parser("show")
    sh.add_argument("action_id")
    asub.add_parser("export-schemas", help="regenerate schemas/action_api/*.json and docs/action_api.openapi.json")
    au = sub.add_parser("audit", help="show the agent audit journal (or the Action API audit with --server)")
    au.add_argument("--server", action="store_true")
    au.add_argument("--incident-id")
    au.add_argument("--tail", type=int, default=50)
    au.add_argument("--out", default="runs")
    rp = sub.add_parser("report", help="print an incident report")
    rp.add_argument("incident_id")
    rp.add_argument("--json", action="store_true")
    rp.add_argument("--out", default="runs")
    g = sub.add_parser("grafana", help="root-cause and verification dashboards for every report (GRAFANA_URL)")
    gsub = g.add_subparsers(dest="grafana_cmd", required=True)
    gs = gsub.add_parser("sync", help="publish the index and each incident's dashboards to Grafana")
    gs.add_argument("--out", default="runs")
    ge = gsub.add_parser("export", help="write the dashboard JSON files instead of publishing")
    ge.add_argument("dir")
    ge.add_argument("--out", default="runs")


def _team_client():
    from action_api_client import TeamActionApiClient

    return TeamActionApiClient(os.getenv("ACTION_API_URL", DEFAULT_ACTION_API_URL), os.getenv("ACTION_API_TOKEN", ""))


def _state_client():
    """Team Action API client for the read-only container_state tool, if the API is configured and reachable."""
    if not (os.getenv("ACTION_API_URL") and os.getenv("ACTION_API_TOKEN")):
        return None
    try:
        client = _team_client()
        client.health()
        return client
    except Exception:  # noqa: BLE001 - the tool is optional; investigation continues without it
        return None


def cmd_action_api(args) -> int:
    import json

    import httpx

    from action_api_client import ActionApiError

    if args.api_cmd == "init-token":
        import secrets

        import endpoints
        values = {}
        if not os.getenv("ACTION_API_TOKEN"):
            values["ACTION_API_TOKEN"] = secrets.token_urlsafe(32)
        if not os.getenv("ACTION_API_URL"):
            values["ACTION_API_URL"] = DEFAULT_ACTION_API_URL
        if not values:
            print("ACTION_API_TOKEN and ACTION_API_URL already set; nothing changed")
            return 0
        endpoints.write_env(HERE / ".env", values)
        print(f"added {', '.join(values)} to {HERE / '.env'} (token not printed)")
        return 0
    if args.api_cmd == "export-schemas":
        import action_api_models
        from report_io import export_report_schema
        for path in action_api_models.export(HERE) + [export_report_schema(HERE)]:
            print(path.relative_to(HERE))
        return 0
    if args.api_cmd == "serve":
        return _serve(args)
    try:
        client = _team_client()
        if args.api_cmd == "health":
            data = client.health()
        elif args.api_cmd == "operations":
            data = client.operations().model_dump(mode="json")
        elif args.api_cmd == "actions":
            data = client.list_actions()
        elif args.api_cmd == "services":
            data = client.system_state()
        else:
            data = client._call("GET", f"/v1/actions/{args.action_id}")[1]
    except (ActionApiError, httpx.HTTPError) as exc:
        print(f"error: Action API: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(data, indent=2, default=str))
    return 0


def _serve(args) -> int:
    from action_executor import STATE_SERVICES, ContainerInspector, DockerExecutor, SimulatedExecutor, allowed_targets
    from action_server import ActionApiService, build_server
    from safety import KillSwitch

    if args.host not in ("127.0.0.1", "localhost", "::1") and not args.bind_all:
        print("error: refusing to bind a non-loopback address without --bind-all", file=sys.stderr)
        return 2
    token = os.getenv("ACTION_API_TOKEN", "")
    if len(token) < 24:
        print("error: ACTION_API_TOKEN missing or shorter than 24 characters (run: python cli.py action-api init-token)", file=sys.stderr)
        return 2
    try:
        targets = allowed_targets(os.getenv("ACTION_API_EXTRA_TARGETS", ""))
        executor = (DockerExecutor(os.getenv("ACTION_API_COMPOSE_PROJECT", "opentelemetry-demo-team_6"))
                    if args.executor == "docker" else SimulatedExecutor())
    except (ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    project = os.getenv("ACTION_API_COMPOSE_PROJECT", "opentelemetry-demo-team_6")
    inspector = ContainerInspector(project)
    service = ActionApiService(executor=executor, targets=targets, state_dir=Path(args.state_dir),
                               kill_switch=KillSwitch(KILL_SWITCH_FILE).active,
                               inspector=inspector if inspector.docker else None, state_services=STATE_SERVICES,
                               loadgen_url=os.getenv("ACTION_API_LOADGEN_URL", "http://localhost:8080/loadgen"),
                               ui_base=f"http://127.0.0.1:{args.port}", grafana_url=os.getenv("GRAFANA_URL", ""))
    server = build_server(service, token, args.host, args.port)
    print(f"Team Action API {args.host}:{args.port}  executor={executor.mode}  targets={', '.join(targets)}\n"
          f"state/audit: {args.state_dir}  kill switch: {KILL_SWITCH_FILE}\n"
          f"Approval page: http://127.0.0.1:{args.port}/ui\n"
          "Approval codes for this API are printed ONLY in this terminal. Ctrl+C to stop.", file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def cmd_audit(args) -> int:
    import json

    path = ACTION_API_STATE_DIR / "audit.jsonl" if args.server else Path(args.out).resolve() / "audit.jsonl"
    if not path.exists():
        print(f"no audit log at {path}")
        return 0
    lines = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    if args.incident_id:
        lines = [l for l in lines if l.get("incident_id") == args.incident_id]
    for rec in lines[-args.tail:]:
        extra = {k: v for k, v in rec.items() if k not in ("time", "event")}
        print(f"{rec.get('time', '')[:19]}  {rec.get('event', ''):28} {json.dumps(extra, default=str)[:300]}")
    return 0


def cmd_report(args) -> int:
    base = Path(args.out).resolve() / args.incident_id / f"{args.incident_id}_report.{'json' if args.json else 'md'}"
    if not base.exists():
        print(f"no report at {base}", file=sys.stderr)
        return 1
    print(base.read_text(encoding="utf-8"))
    return 0


def cmd_grafana(args, incident_id: str | None = None) -> int:
    import grafana_dash

    runs = Path(args.out).resolve()
    if getattr(args, "grafana_cmd", None) == "export":
        paths = grafana_dash.export(runs, Path(args.dir))
        print(f"wrote {len(paths)} dashboards to {Path(args.dir).resolve()}")
        return 0
    try:
        res = grafana_dash.sync(runs, grafana_dash.GrafanaPublisher())
    except grafana_dash.GrafanaError as exc:
        print(f"grafana: {exc} (set GRAFANA_URL, e.g. python cli.py endpoints --write-env)", file=sys.stderr)
        return 1
    print(f"Grafana index:   {res['index']}")
    for iid, (rc, ver) in res["incidents"].items():
        if incident_id in (None, iid):
            print(f"{iid}\n  root cause:    {rc}\n  verification:  {ver}")
    for s in res["skipped"]:
        print(f"skipped (not a valid report): {s}")
    return 0


def cmd_endpoints(args) -> int:
    import endpoints

    if args.write_env:
        found = endpoints.discover()
        if not found:
            print("no published backend ports found (is the observability stack running?)", file=sys.stderr)
            return 1
        changed = endpoints.write_env(HERE / ".env", found)
        for k, v in found.items():
            os.environ[k] = v
        print(f"updated {HERE / '.env'}: {', '.join(changed) or 'no changes'}")
    rows = endpoints.check() + [endpoints.action_api_status(os.getenv("ACTION_API_CATALOG", ""))]
    for row in rows:
        print(f"{row['backend']:11} {row.get('status', ''):28} {row.get('url', '-')}")
        if row.get("tools"):
            print(f"{'':11} tools:   {row['tools']}")
        if row.get("browser", "-") != "-":
            print(f"{'':11} browser: {row['browser']}")
    return 0


def cmd_kill_switch(args) -> int:
    from safety import KillSwitch

    ks = KillSwitch(KILL_SWITCH_FILE)
    if args.state != "status":
        ks.set(args.state == "on")
    print(f"kill switch: {'ACTIVE - all state-changing actions blocked' if ks.active() else 'off'} ({KILL_SWITCH_FILE})")
    return 0


def cmd_catalog(args) -> int:
    try:
        cat = load_catalog(args.action_catalog)
    except CatalogError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"provenance: {cat.provenance}  base URL ({cat.base_url_env}): {cat.base_url or 'unset'}")
    if not cat.operations:
        print("no operations: remediation will always escalate (official Action API contract not configured)")
    for op in cat.operations:
        print(f"- {op.name}: {op.method} {op.path} target={op.target_kind} {op.allowed_targets} params={list(op.parameters)}")
    return 0


def reset_dashboard(out: str) -> int:
    import shutil

    from grafana_publish import clear_annotations

    runs = Path(out).resolve()
    archive = runs / "_archive" / datetime.now().strftime("%Y%m%d-%H%M%S")
    moved = 0
    for d in sorted(runs.glob("*")) if runs.is_dir() else []:
        if d.is_dir() and d.name not in ("_archive", "action_api"):
            archive.mkdir(parents=True, exist_ok=True)
            shutil.move(str(d), str(archive / d.name))
            moved += 1
    print(f"Archived {moved} run folder(s) to {archive}" if moved else "No runs to archive")
    print(clear_annotations())
    print(build_grafana_dashboards(runs))
    print(reload_grafana_dashboards())
    return 0


def check_llm(provider: str, model: str) -> int:
    try:
        reply = make_chat_model(provider, model, 50, 30).invoke("Reply with the single word OK.")
    except Exception as exc:  # ModelError (no key/package) or provider error (bad model, quota)
        print(f"LLM check FAILED ({provider} / {model}): {type(exc).__name__}: {str(exc)[:300]}", file=sys.stderr)
        return 1
    usage = reply.usage_metadata or {}
    print(f"LLM check OK: {provider} / {model} replied {reply_text(reply).strip()[:40]!r} "
          f"(tokens in/out: {usage.get('input_tokens')}/{usage.get('output_tokens')})")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    simple = {"endpoints": cmd_endpoints, "kill-switch": cmd_kill_switch, "catalog": cmd_catalog,
              "action-api": cmd_action_api, "audit": cmd_audit, "report": cmd_report, "grafana": cmd_grafana}
    if args.cmd in simple:
        return simple[args.cmd](args)
    if args.cmd == "reset-dashboard":
        return reset_dashboard(args.out)
    if args.cmd == "doctor":
        import preflight
        return preflight.doctor(args.provider, args.model, check_llm)
    if not args.model:
        print("error: model identifier missing (use --model or set LLM_MODEL in .env)", file=sys.stderr)
        return 2
    if args.cmd == "check-llm":
        return check_llm(args.provider, args.model)
    import preflight
    print(preflight.resolve_opensearch())
    if args.cmd == "watch":
        if args.baseline_min:
            import detector
            detector.configure(args.baseline_min)
        if args.diagnose_only or args.once:
            args.respond = args.action_api = False
            args.approval_channel = "terminal"
        ready = (None, None) if args.once else _preflight(args)
        if isinstance(ready, int):
            return ready
        return watch(args, *ready)
    ready = _preflight(args)
    if isinstance(ready, int):
        return ready
    return run_incident(args, args.incident_id, args.symptom, args.service, args.source, *ready)


def _preflight(args):
    """Action API checks shared by investigate and watch: (catalog, client) or an exit code."""
    from action_api_models import MAX_APPROVAL_TTL_S
    if args.approval_ttl_s > MAX_APPROVAL_TTL_S:
        print(f"note: --approval-ttl-s {args.approval_ttl_s:.0f} is above the maximum; using {MAX_APPROVAL_TTL_S} s")
        args.approval_ttl_s = MAX_APPROVAL_TTL_S
    catalog, client = None, None
    if args.respond and args.action_api:
        import httpx

        from action_api_client import ActionApiError
        try:
            client = _team_client()
            catalog = client.catalog()
        except (ActionApiError, httpx.HTTPError) as exc:
            print(f"error: team Action API unavailable ({exc}). Start it in another terminal with:\n"
                  "  python cli.py action-api serve            (add --executor docker for real restarts)\n"
                  "or run without remediation: watch --diagnose-only", file=sys.stderr)
            return 2
    elif args.respond:
        try:
            catalog = load_catalog(args.action_catalog)
            client = HttpActionClient(catalog) if args.execute else None
        except CatalogError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    elif args.execute or args.action_api:
        print("error: --execute and --action-api require --respond", file=sys.stderr)
        return 2
    if args.approval_channel == "page" and not (args.respond and args.action_api):
        print("error: --approval-channel page needs --respond --action-api", file=sys.stderr)
        return 2
    return catalog, client


def _refresh_dashboards(out: Path) -> None:
    try:
        build_grafana_dashboards(out)
        reload_grafana_dashboards()
    except OSError as exc:  # dashboards are a view; the report files stay the record
        print(f"Grafana dashboards not rebuilt: {exc}")


def run_incident(args, incident_id: str, symptom: str, service, source: str, catalog, client, alerts=None) -> int:
    now = datetime.now(timezone.utc)
    out = Path(args.out).resolve()
    run_dir = out / incident_id
    try:
        incident = Incident(
            id=incident_id, source=source, symptom=symptom, affected_service=service,
            detection_time=now, observation_window=TimeWindow(start_time=now - timedelta(minutes=args.window_minutes), end_time=now),
            recovery_condition=args.recovery_condition,
        )
        run_tool, policy, _ = make_telemetry_runner(run_dir / "evidence", _state_client())
        if not policy.approved_services:
            print("error: no services visible in telemetry backends; run `python cli.py doctor` first", file=sys.stderr)
            return 2
        registry = EvidenceRegistry()
        reasoner = LLMReasoner(args.model, provider=args.provider, request_timeout_s=args.request_timeout_s,
                               transcript_path=run_dir / "llm_transcript.json")
        deps = Dependencies(reasoner=reasoner, run_tool=run_tool, policy=policy, registry=registry)
        config = InvestigationConfig(deadline_s=args.deadline_s, max_tool_calls=args.max_tool_calls,
                                     max_model_calls=args.max_model_calls, request_timeout_s=args.request_timeout_s,
                                     max_total_tokens=args.max_total_tokens)
        report = investigate(incident, config, deps)
        if alerts:
            report = report.model_copy(update={"timeline": [TimelineEvent(
                timestamp=now, status=IncidentStatus.RECEIVED, description=f"Alert received from the watcher: {symptom}",
                metadata={"alerts": alerts})] + list(report.timeline)})
        for p in report.proposals:  # what the investigation itself would propose (kept even without --respond)
            payload = action_payload(p, report.diagnosis.supporting_evidence_ids if report.diagnosis else [])
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "action_payload.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
        if args.respond:
            report = report.model_copy(update={"proposals": []})  # the remediation step builds the real proposal
            report = _respond(args, report, catalog, client, run_tool, registry, policy.approved_services, out)
    except (IncidentInputError, ModelError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    report, art = write_report(report, run_dir)
    meta = next((t.metadata for t in reversed(report.timeline) if "model_calls" in t.metadata), {})
    print(f"Incident: {report.incident_id}   Status: {report.status.value}")
    if report.diagnosis:
        d = report.diagnosis
        print(f"Diagnosis: {d.suspected_component}: {d.suspected_cause}\n"
              f"Confidence label: {d.confidence_label.value} (in where/how it fails, not in why)\n"
              f"Supporting evidence: {', '.join(d.supporting_evidence_ids)}\nUncertainty (still unknown): {d.uncertainty}")
    else:
        print("No diagnosis accepted (escalate to a human).")
    if report.remediation_outcome:
        print(f"Remediation: {report.remediation_outcome} (mode {report.execution_mode})"
              + (f" - {report.escalation_reason}" if report.escalation_reason else ""))
        for p in report.proposals:
            print(f"Proposal: {p.action} on {p.target_service} {p.parameters} | verify: {p.verification_plan}")
        if report.verification:
            print(f"Verification: {report.verification.result.value}: {report.verification.details}")
    print(f"Model calls: {meta.get('model_calls')}  Tool calls: {meta.get('tool_calls')}  Elapsed: {meta.get('elapsed_s')}s")
    print(f"Report JSON: {art.json_path}\nReport MD:   {art.markdown_path}")
    print(f"Report schema (schemas/incident_report.schema.json): {art.team_schema_status}")
    for e in art.team_schema_errors + art.internal_validation_errors + art.export_errors:
        print(f"VALIDATION/EXPORT ERROR: {e}")
    for lim in report.limitations:
        print(f"limitation: {lim}")
    print(publish_to_grafana(report))
    _refresh_dashboards(out)
    if args.grafana and art.json_path:
        try:
            cmd_grafana(args, report.incident_id)
        except Exception as exc:  # the report is already written; a dashboard problem must not fail the incident
            print(f"grafana: dashboards not published ({type(exc).__name__}: {exc})", file=sys.stderr)
    return 0 if not art.export_errors else 1


def timed_input(ttl_s: float):
    """input() that gives up after the approval TTL; an unanswered prompt becomes a rejection, never a hang.
    A reader thread instead of select(): select() on stdin does not work on Windows."""
    def read(prompt: str) -> str:
        print(prompt, end="", flush=True)
        box: queue.Queue = queue.Queue(maxsize=1)

        def reader() -> None:
            try:
                box.put(sys.stdin.readline())
            except (OSError, ValueError):
                box.put("")
        threading.Thread(target=reader, daemon=True).start()
        try:
            return box.get(timeout=ttl_s)
        except queue.Empty:
            return ""
    return read


def _respond(args, report, catalog, client, run_tool, registry, inventory, out: Path):
    from remediation import RuleBasedPlanner
    from response import ResponseConfig, ResponseDeps, respond
    from safety import AuditJournal, KillSwitch

    cfg = ResponseConfig(mode="live" if args.execute else "dry_run", approval_ttl_s=args.approval_ttl_s,
                         approval_channel=args.approval_channel if args.execute else "terminal",
                         settle_s=max(args.settle_s, 60 * args.verify_window_min), interval_s=args.verify_interval_s,
                         samples=max(1, args.samples), window_minutes=args.verify_window_min)

    def on_stage(name: str, interim) -> None:
        write_report(interim, out / interim.incident_id)
        _refresh_dashboards(out)  # the Incidents table shows AWAITING_APPROVAL, EXECUTING, VERIFYING as they happen
        if args.grafana:
            cmd_grafana(args, interim.incident_id)

    deps = ResponseDeps(planner=RuleBasedPlanner(), catalog=catalog, run_tool=run_tool, registry=registry,
                        inventory=inventory, kill_switch=KillSwitch(KILL_SWITCH_FILE), journal=AuditJournal(out / "audit.jsonl"),
                        lock_path=out / ".dispatch.lock", approver=args.approver or getpass.getuser(),
                        interactive=sys.stdin.isatty(), input_fn=timed_input(args.approval_ttl_s), client=client,
                        on_stage=on_stage)
    return respond(report, cfg, deps)


def watch(args, catalog, client) -> int:
    """Detector loop (detector.py): an alert that holds for 2 checks runs the whole chain for it."""
    import detector
    from grafana_dashboards import write_alert
    from grafana_publish import post_alert

    if not args.once:
        if args.respond:
            mode = "LIVE: approved restarts run" if args.execute else "dry-run: approved actions are only validated"
            print(f"Remediation on ({mode}). Approve on {os.getenv('ACTION_API_URL', DEFAULT_ACTION_API_URL)}/ui "
                  "with the code printed in the Action API terminal.")
        else:
            print("Diagnosis only (no remediation).")

    def alert(incident_id: str, text: str) -> str:
        # Show the incident on both dashboards at once; the report replaces it as the chain progresses.
        status = post_alert(incident_id, text)
        try:
            write_alert(incident_id, text, Path(args.out))
            _refresh_dashboards(Path(args.out).resolve())
        except OSError as exc:
            status += f"; incidents table not updated ({exc})"
        return status

    def run(incident_id: str, symptom: str, breaches: list) -> None:
        try:
            run_incident(args, incident_id, symptom, None, "threshold-alert", catalog, client,
                         alerts=[b.as_dict() for b in breaches])
        except Exception as exc:  # noqa: BLE001 - one failed incident must not stop the watcher
            print(f"[watch] incident {incident_id} failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)

    return detector.watch(run, interval_s=args.interval_s, once=args.once, post_alert=alert)


if __name__ == "__main__":
    sys.exit(main())
