"""Tests for the per-run fixture dump (Step 8)."""

from __future__ import annotations

import json
from pathlib import Path

from incident_agent.act.post_to_slack import PostedMessage
from incident_agent.fixtures import write_run_fixtures
from incident_agent.gather.instatus import InStatusComponent, InStatusSnapshot
from incident_agent.gather.rubric import RubricResult
from incident_agent.gather.slack import SlackSnapshot
from incident_agent.gather.status_pages import StatusPageSnapshot
from incident_agent.reason.schema import SweepResult


def test_write_run_fixtures_writes_every_file(tmp_path: Path):
    run_dir = tmp_path / "2026-05-13T090000Z"

    write_run_fixtures(
        run_dir=run_dir,
        slack=SlackSnapshot(channels=[]),
        status_pages={
            "smartcar": StatusPageSnapshot(
                source="smartcar",
                base_url="https://status.smartcar.com",
                current_status="All Systems Operational",
                current_indicator="none",
                recent_incidents=[],
            ),
        },
        instatus=InStatusSnapshot(
            page_id="cmtest123",
            base_url="https://api.instatus.com",
            active_incidents=[],
            components=[InStatusComponent(id="c1", name="BMW", status="OPERATIONAL")],
        ),
        rubric=RubricResult(
            markdown="# Rubric\nFlag major outages.\n",
            source="notion",
            page_id="35f4...",
        ),
        system_prompt="You are an integration incident analyst.",
        user_prompt="Today's date: 2026-05-13",
        sweep_result=SweepResult(candidates=[], watching=[], summary="Nothing to flag."),
        posted_messages=[
            PostedMessage(
                kind="all_clear",
                blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": "all clear"}}],
                fallback_text="all clear",
                channel="C_TEST",
                ts="1.000000",
                permalink="https://example.slack.com/...",
            ),
        ],
        run_metadata={"agent_version": "0.1.0", "errors": []},
    )

    files = {p.name for p in run_dir.iterdir()}
    assert files == {
        "slack.json",
        "status_pages.json",
        "instatus.json",
        "rubric.md",
        "rubric_source.txt",
        "system_prompt.txt",
        "prompt.txt",
        "response.json",
        "posted_messages.json",
        "run_metadata.json",
    }

    rubric_md = (run_dir / "rubric.md").read_text(encoding="utf-8")
    assert "Flag major outages." in rubric_md

    rubric_source = (run_dir / "rubric_source.txt").read_text(encoding="utf-8")
    assert "source: notion" in rubric_source

    response = json.loads((run_dir / "response.json").read_text(encoding="utf-8"))
    assert response["summary"] == "Nothing to flag."

    posted = json.loads((run_dir / "posted_messages.json").read_text(encoding="utf-8"))
    assert posted[0]["ts"] == "1.000000"
    assert posted[0]["kind"] == "all_clear"


def test_write_run_fixtures_is_safe_with_partial_inputs(tmp_path: Path):
    """The main loop calls write_run_fixtures multiple times as data lands."""
    run_dir = tmp_path / "partial"

    # First flush: just gather output.
    write_run_fixtures(
        run_dir=run_dir,
        slack=SlackSnapshot(channels=[]),
        rubric=RubricResult(markdown="rubric", source="file", page_id=None),
    )
    assert (run_dir / "slack.json").is_file()
    assert (run_dir / "rubric.md").is_file()
    assert not (run_dir / "response.json").is_file()

    # Second flush: add the reasoning output. Earlier files must remain.
    write_run_fixtures(
        run_dir=run_dir,
        system_prompt="sys",
        user_prompt="usr",
        sweep_result=SweepResult(candidates=[], watching=[], summary="ok"),
    )
    assert (run_dir / "slack.json").is_file()  # not lost
    assert (run_dir / "response.json").is_file()
