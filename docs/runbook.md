# Runbook

## Daily operations

1. The cron job runs at 09:00 UTC. Output lands in `#hardware-incident-alert-test`.
2. Review each candidate. React ✅ / ❌ / 🤔 to triage (v2 will wire these to InStatus drafts).
3. When the bot is wrong, add an entry to the "Examples of past decisions"
   section of the Notion rubric describing what the right call would have been.

## When things break

### No daily post showed up
- Check the latest workflow run in GitHub Actions.
- If it failed: download the `fixture-{run_id}` artifact for the raw inputs +
  any partial outputs.
- Re-run via `workflow_dispatch` after the underlying issue is fixed.

### Notion fetch failed
- Bot falls back to `docs/rubric-starter.md` and logs a warning. No human
  action needed unless Notion is down for >24h.

### Smartcar / Enode status page unreachable
- Bot logs a warning and proceeds without that source. Recommendations may
  miss provider-side signals that day; lean on Slack signal.

### InStatus API unreachable
- Bot proceeds without dedup. Watch for duplicate candidates against
  already-open InStatus incidents.

### Claude API error
- Bot retries once with backoff, then posts a failure message to Slack with
  the run ID. Inspect the fixture artifact for the exact prompt.

## Tuning prompts

1. Download a fixture from a problematic run.
2. Drop it into `tests/fixtures/{date}/`.
3. Edit `prompts/system.md` or `prompts/user_template.md`.
4. Run reason-only against the fixture (replay test, Step 6 of the dev plan).
5. Iterate until output is sane, then commit prompt + a regression test.

## Cost

Estimated $3–$8/run with Opus 4 (~$90–240/month). Track via the Anthropic
console; if a run spikes well above the band, inspect channel pre-filter
configuration in `config/channels.yaml` — likely culprit is an un-filtered
high-volume channel.
