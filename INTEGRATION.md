# Incident agent: how the pieces fit

See README.md for the start-to-finish run guide. All commands run from `incident-agent/` (Windows: `.\.venv\Scripts\python.exe`).

```
detector.py (every 15 s, 2 consecutive checks)
  -> cli.watch: ALERT annotation + INVESTIGATING row in Grafana
  -> orchestrator.investigate: reasoning.LLMReasoner (LangChain; Gemini by default, fallback model on 503/429)
       <-> guardrails.validate_tool_request -> read-only tools (telemetry.py, resources.py, container_state via Action API)
       -> candidates.py evidence picture -> guardrails.validate_diagnosis (citations, grounding, no flag wording)
  -> response.respond (only with --respond; watch has it on):
       remediation.RuleBasedPlanner (restart only for a process fault, else escalate)
       -> safety.evaluate (catalog allowlist, target, arguments, evidence)
       -> verify.collect baseline -> kill switch -> approval (Action API page or terminal code, digest-bound, expires)
       -> dry-run validation | live: Action API executes once (journal first) -> verify.judge after settle_s
  -> report_io.write_report (JSON + Markdown, team schema) -> Grafana annotation + pages
```

The LLM only ever gets read-only telemetry tools. Planning, policy, approval, the kill switch, dispatch and
verification are deterministic code. Restarts can only be done by the Action API process (`action_server.py` +
`action_executor.py`), which issues a one-time approval code on its own console; the agent cannot approve itself.

## Action API
    cli.py action-api init-token                    # once: ACTION_API_TOKEN / ACTION_API_URL in .env
    cli.py action-api serve                         # separate terminal; --executor docker for real restarts
    cli.py action-api health | operations | services | actions | show <action_id>
    cli.py kill-switch on|off|status                # blocks every state-changing action
    cli.py audit [--server] [--incident-id ID]

## Run modes
    cli.py watch --diagnose-only                    # steps 1-4 only, no Action API needed
    cli.py watch                                    # full chain, dry-run: approval asked, request validated, not sent
    cli.py watch --execute                          # full chain, live: approved request is sent, recovery measured
    cli.py investigate --incident-id X --symptom "..." --respond --action-api [--execute]   # one manual incident

Outcomes: `VERIFIED_RECOVERY` (status RECOVERED), `PARTIAL_RECOVERY`, `NOT_RECOVERED`, `UNVERIFIED`, `ACTION_FAILED`,
`ESCALATED` (status ESCALATED or REJECTED, e.g. approval expired), `DRY_RUN` (status ACTION_PROPOSED).

## Grafana
Team Grafana (`grafana/compose.yaml`, http://localhost:3001): "Incident Agent" (annotations on the error/latency
graphs) and "Incident Agent - Incidents" (one row per incident, updated at every stage). `cli.py grafana sync`
additionally publishes per-incident root-cause and verification dashboards built by `grafana_dash.py`.

## Known limitations
- After a Docker restart or laptop sleep, Prometheus has a history gap: alerts need 30 min of normal traffic again
  (`cli.py doctor` says how long).
- Free-tier LLM models get overloaded (503); the agent switches to LLM_FALLBACK_MODEL at once, then retries with growing waits (8, 16, 32, 64 s), alternating the two models, while the time budget allows.
- Verification needs `--settle-s` >= 120 so the 2-minute rate windows only cover time after the action.
