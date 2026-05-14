"""Tests for the main orchestration (Step 8)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from incident_agent import main as main_mod
from incident_agent.act.post_to_slack import PostedMessage
from incident_agent.gather.instatus import InStatusSnapshot
from incident_agent.gather.rubric import RubricResult
from incident_agent.gather.slack import SlackSnapshot
from incident_agent.gather.status_pages import StatusPageSnapshot
from incident_agent.reason.schema import SweepResult


@pytest.fixture
def patched_pipeline(monkeypatch, tmp_path: Path):
    """Wire up the four gatherers + reason + post + fixtures dir for fast tests."""
    calls: dict = {"posted": False, "reasoned": False}

    monkeypatch.setattr(
        main_mod, "make_run_dir", lambda *a, **k: tmp_path / "run"
    )

    monkeypatch.setattr(main_mod, "gather_status_pages", lambda: {
        "smartcar": StatusPageSnapshot(
            source="smartcar",
            base_url="https://status.smartcar.com",
            current_status="All Systems Operational",
            current_indicator="none",
            recent_incidents=[],
        ),
    })
    monkeypatch.setattr(main_mod, "gather_instatus_state", lambda: InStatusSnapshot(
        page_id="p", base_url="https://api.instatus.com",
        active_incidents=[], components=[],
    ))
    monkeypatch.setattr(main_mod, "gather_rubric", lambda: RubricResult(
        markdown="# Rubric", source="file", page_id=None,
    ))
    monkeypatch.setattr(main_mod, "gather_slack_messages", lambda: SlackSnapshot(channels=[]))

    def _reason(inputs):
        calls["reasoned"] = True
        return (
            SweepResult(candidates=[], watching=[], summary="Nothing to flag."),
            "SYS",
            "USR",
        )

    monkeypatch.setattr(main_mod, "reason_over_signals", _reason)

    def _post(sweep, run_metadata=None):
        calls["posted"] = True
        return [
            PostedMessage(
                kind="all_clear",
                blocks=[],
                fallback_text="all clear",
                channel="C_TEST",
                ts="1.0",
            ),
        ]

    monkeypatch.setattr(main_mod, "post_sweep_result", _post)

    return calls, tmp_path / "run"


def test_main_happy_path_writes_all_fixtures_and_posts(patched_pipeline):
    calls, run_dir = patched_pipeline

    rc = main_mod.main([])

    assert rc == 0
    assert calls["reasoned"] is True
    assert calls["posted"] is True

    # All the expected fixture files should be present.
    files = {p.name for p in run_dir.iterdir()}
    assert "slack.json" in files
    assert "instatus.json" in files
    assert "status_pages.json" in files
    assert "rubric.md" in files
    assert "prompt.txt" in files
    assert "system_prompt.txt" in files
    assert "response.json" in files
    assert "posted_messages.json" in files
    assert "run_metadata.json" in files

    metadata = json.loads((run_dir / "run_metadata.json").read_text(encoding="utf-8"))
    assert metadata["dry_run"] is False
    assert metadata["errors"] == []
    assert "phase_timings_seconds" in metadata


def test_main_dry_run_skips_post(patched_pipeline):
    calls, run_dir = patched_pipeline

    rc = main_mod.main(["--dry-run"])

    assert rc == 0
    assert calls["reasoned"] is True
    assert calls["posted"] is False
    metadata = json.loads((run_dir / "run_metadata.json").read_text(encoding="utf-8"))
    assert metadata["dry_run"] is True


def test_main_captures_gatherer_exception_and_continues(monkeypatch, patched_pipeline):
    calls, run_dir = patched_pipeline

    def _boom():
        raise RuntimeError("slack token rejected")

    monkeypatch.setattr(main_mod, "gather_slack_messages", _boom)

    rc = main_mod.main([])

    assert rc == 0  # the rest of the pipeline still ran
    assert calls["reasoned"] is True
    metadata = json.loads((run_dir / "run_metadata.json").read_text(encoding="utf-8"))
    phases = [e["phase"] for e in metadata["errors"]]
    assert "gather/slack" in phases


def test_main_returns_nonzero_when_reason_fails(monkeypatch, patched_pipeline):
    _, run_dir = patched_pipeline

    def _reason_boom(inputs):
        raise RuntimeError("Claude API down")

    monkeypatch.setattr(main_mod, "reason_over_signals", _reason_boom)

    rc = main_mod.main([])

    assert rc == 1
    metadata = json.loads((run_dir / "run_metadata.json").read_text(encoding="utf-8"))
    assert any(e["phase"] == "reason" for e in metadata["errors"])
