# integration-incident-agent

A semi-autonomous agent for ev.energy that sweeps Slack channels, Smartcar/Enode
status pages, and the team's Notion incident tracker each day, then recommends
whether to open a new incident on the ev.energy InStatus page.

v1 produces recommendations as Slack messages for human approval.

## Architecture

Three phases per run: **gather → reason → act**.

- `src/incident_agent/gather/` — pulls Slack, Statuspage, InStatus, Notion. No LLM.
- `src/incident_agent/reason/` — single Claude call with structured-output tool.
- `src/incident_agent/act/` — posts Block Kit messages to Slack.
- `src/incident_agent/fixtures.py` — dumps everything per run for audit/replay.

See the dev plan for the full design.

## Running locally

```bash
pip install -e ".[dev]"
python -m incident_agent.main
```

Requires the env vars listed in `.github/workflows/daily-sweep.yml`.

## Running in CI

`.github/workflows/daily-sweep.yml` runs daily at 09:00 UTC and is also
triggerable manually via `workflow_dispatch`. Each run uploads `fixtures/` as
an artifact with 30-day retention.

## Status

**Step 1 of 11 — scaffolding only.** Modules are stubs. See the dev plan for
the implementation sequence.
