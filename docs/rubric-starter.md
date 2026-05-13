# Decision Rubric — starter

This file bootstraps the Notion rubric. Akhil should review, edit, and copy
into Notion as the source of truth. The bot reads from Notion at runtime; this
file is the bootstrap and the fallback when Notion is unreachable.

## Severity tiers

### Investigating (yellow)
- A provider has publicly posted a degradation, OR
- ≥3 independent internal reports in the last 4 hours about the same issue, OR
- Single-provider error rate jump >2× baseline for >30 min, affecting <10% of operations.

### Identified (orange)
- Confirmed sustained upstream provider issue (status page "identified" or "major").
- Internal issue affecting >10% of operations for >30 min.
- A specific brand/region pair is non-functional.

### Major outage (red)
- A provider is fully down.
- ev.energy cannot fulfill charges across a brand or region.
- Smart charging dispatch is broken end-to-end for a utility program.

## Do NOT flag
- Single-user reports without correlating signal.
- Sub-30-min transients that have already resolved.
- Internal-only annoyances (UI papercuts, slow dashboards, CS ticket backlogs).
- Already-active incidents on InStatus (set `duplicates_existing_incident`).
- Known-flaky integrations during their known-flaky periods (note these as
  `watching` to track frequency, but don't escalate every blip).

## Examples of past decisions

This section gets populated over time with feedback from real runs. Each entry:
- **Date**
- **What the bot saw**
- **What it recommended (or didn't)**
- **What was the right call**
- **Why**

_(Empty — populated during the calibration period.)_
