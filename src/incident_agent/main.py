"""Entry point: orchestrates gather → reason → act, dumps fixtures per run.

Run with `python -m incident_agent.main`. GitHub Actions invokes the same
command on the daily cron.

Failure-tolerant by design: each phase captures its own errors on the
returned snapshot rather than raising, so one bad data source can't kill
the run. The exit code reflects whether the overall pipeline produced a
post on Slack — non-zero only if posting fully failed.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path

from incident_agent import __version__
from incident_agent.act.post_to_slack import post_sweep_result
from incident_agent.fixtures import make_run_dir, write_run_fixtures
from incident_agent.gather.instatus import gather_instatus_state
from incident_agent.gather.rubric import gather_rubric
from incident_agent.gather.slack import gather_slack_messages
from incident_agent.gather.status_pages import gather_status_pages
from incident_agent.reason.claude import ReasoningInputs, reason_over_signals
from incident_agent.reason.schema import SweepResult

LOG = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


def _ghactions_run_url() -> str | None:
    repo = os.environ.get("GITHUB_REPOSITORY")
    run_id = os.environ.get("GITHUB_RUN_ID")
    if repo and run_id:
        return f"https://github.com/{repo}/actions/runs/{run_id}"
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Daily integration-incident sweep.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run gather → reason but skip the Slack post. Still writes fixtures.",
    )
    parser.add_argument(
        "--skip-post",
        dest="dry_run",
        action="store_true",
        help="Alias for --dry-run.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    start = time.monotonic()
    started_at = _now()
    run_dir: Path = make_run_dir(started_at)
    LOG.info("incident_agent v%s starting; run_dir=%s", __version__, run_dir)

    errors: list[dict] = []
    phase_timings: dict[str, float] = {}

    # ── PHASE: gather ─────────────────────────────────────────────────────
    t0 = time.monotonic()
    LOG.info("gather: status pages")
    try:
        status_pages = gather_status_pages()
    except Exception as exc:  # noqa: BLE001 — never let a gatherer kill the run
        status_pages = {}
        errors.append({"phase": "gather/status_pages", "error": repr(exc),
                       "traceback": traceback.format_exc()})
        LOG.exception("gather/status_pages failed")
    phase_timings["status_pages"] = time.monotonic() - t0

    t0 = time.monotonic()
    LOG.info("gather: instatus")
    try:
        instatus = gather_instatus_state()
    except Exception as exc:  # noqa: BLE001
        from incident_agent.gather.instatus import InStatusSnapshot
        instatus = InStatusSnapshot(
            page_id="(error)", base_url="https://api.instatus.com",
            active_incidents=[], components=[],
            error=f"{type(exc).__name__}: {exc}",
        )
        errors.append({"phase": "gather/instatus", "error": repr(exc),
                       "traceback": traceback.format_exc()})
        LOG.exception("gather/instatus failed")
    phase_timings["instatus"] = time.monotonic() - t0

    t0 = time.monotonic()
    LOG.info("gather: rubric")
    try:
        rubric = gather_rubric()
    except Exception as exc:  # noqa: BLE001
        from incident_agent.gather.rubric import RubricResult
        rubric = RubricResult(markdown="", source="missing", page_id=None,
                              error=f"{type(exc).__name__}: {exc}")
        errors.append({"phase": "gather/rubric", "error": repr(exc),
                       "traceback": traceback.format_exc()})
        LOG.exception("gather/rubric failed")
    phase_timings["rubric"] = time.monotonic() - t0

    t0 = time.monotonic()
    LOG.info("gather: slack")
    try:
        slack = gather_slack_messages()
    except Exception as exc:  # noqa: BLE001
        from incident_agent.gather.slack import SlackSnapshot
        slack = SlackSnapshot(channels=[], error=f"{type(exc).__name__}: {exc}")
        errors.append({"phase": "gather/slack", "error": repr(exc),
                       "traceback": traceback.format_exc()})
        LOG.exception("gather/slack failed")
    phase_timings["slack"] = time.monotonic() - t0

    # Dump gather output even if reason or act blows up.
    write_run_fixtures(
        run_dir=run_dir,
        slack=slack,
        status_pages=status_pages,
        instatus=instatus,
        rubric=rubric,
    )

    # ── PHASE: reason ─────────────────────────────────────────────────────
    inputs = ReasoningInputs(
        rubric=rubric,
        instatus=instatus,
        status_pages=status_pages,
        slack=slack,
        today_iso=started_at.date().isoformat(),
    )

    t0 = time.monotonic()
    sweep: SweepResult | None = None
    system_prompt = user_prompt = None
    try:
        sweep, system_prompt, user_prompt = reason_over_signals(inputs)
        LOG.info(
            "reason: %d candidate(s), %d watching, %d-char summary",
            len(sweep.candidates), len(sweep.watching), len(sweep.summary or ""),
        )
    except Exception as exc:  # noqa: BLE001
        errors.append({"phase": "reason", "error": repr(exc),
                       "traceback": traceback.format_exc()})
        LOG.exception("reason failed")
    phase_timings["reason"] = time.monotonic() - t0

    write_run_fixtures(
        run_dir=run_dir,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        sweep_result=sweep,
    )

    # ── PHASE: act ────────────────────────────────────────────────────────
    posted = None
    if sweep is None:
        LOG.warning("act: skipping — reason produced no SweepResult")
    elif args.dry_run:
        LOG.info("act: skipping — --dry-run set")
    else:
        t0 = time.monotonic()
        try:
            posted = post_sweep_result(
                sweep,
                run_metadata={
                    "github_run_id": os.environ.get("GITHUB_RUN_ID"),
                    "artifact_url": _ghactions_run_url(),
                },
            )
            for m in posted:
                if m.error:
                    errors.append({"phase": "act/post", "error": m.error, "kind": m.kind})
        except Exception as exc:  # noqa: BLE001
            errors.append({"phase": "act/post", "error": repr(exc),
                           "traceback": traceback.format_exc()})
            LOG.exception("act/post failed")
        phase_timings["act"] = time.monotonic() - t0

    # ── PHASE: fixtures (final flush w/ metadata) ────────────────────────
    finished_at = _now()
    run_metadata = {
        "agent_version": __version__,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_seconds": time.monotonic() - start,
        "phase_timings_seconds": phase_timings,
        "github": {
            "run_id": os.environ.get("GITHUB_RUN_ID"),
            "repository": os.environ.get("GITHUB_REPOSITORY"),
            "ref": os.environ.get("GITHUB_REF"),
            "sha": os.environ.get("GITHUB_SHA"),
            "run_url": _ghactions_run_url(),
        },
        "dry_run": args.dry_run,
        "errors": errors,
    }

    write_run_fixtures(
        run_dir=run_dir,
        posted_messages=posted,
        run_metadata=run_metadata,
    )

    # Exit code: 0 if we got at least to a post (or successfully skipped on
    # dry-run), 1 if the pipeline failed before that. Per-post Slack errors
    # don't fail the run — they're captured for human review.
    if args.dry_run:
        return 0 if sweep is not None else 1
    if posted is None:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
