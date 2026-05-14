"""Per-run fixture dumps for audit + prompt-iteration replay.

Every sweep writes a directory under `fixtures/{YYYY-MM-DD-HHMMSS}/` with:
  slack.json          — raw gather output (model_dump of SlackSnapshot)
  status_pages.json   — same, keyed by source name
  instatus.json       — InStatusSnapshot
  rubric.md           — the markdown the bot reasoned against (and `rubric_source.txt`
                        recording where it came from)
  prompt.txt          — the exact user prompt sent to Claude
  system_prompt.txt   — the system prompt
  response.json       — the SweepResult Claude returned
  posted_messages.json — what got posted to Slack (with ts + permalink + per-post errors)
  run_metadata.json   — timing, errors, version, github run info

The directory is uploaded as a GitHub Actions artifact (retention: 30 days)
and is also the replay corpus — drop one into tests/fixtures/ and rerun
reason-only via `python -m incident_agent.reason.claude --fixture-dir ...`.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from incident_agent.act.post_to_slack import PostedMessage
from incident_agent.config import REPO_ROOT
from incident_agent.gather.instatus import InStatusSnapshot
from incident_agent.gather.rubric import RubricResult
from incident_agent.gather.slack import SlackSnapshot
from incident_agent.gather.status_pages import StatusPageSnapshot
from incident_agent.reason.schema import SweepResult

LOG = logging.getLogger(__name__)

FIXTURES_ROOT = REPO_ROOT / "fixtures"


def _write_json(path: Path, data: Any) -> None:
    path.write_text(
        json.dumps(data, indent=2, default=str, ensure_ascii=False),
        encoding="utf-8",
    )


def _write_text(path: Path, content: str) -> None:
    path.write_text(content or "", encoding="utf-8")


def _model_or_dataclass_dump(obj: Any) -> Any:
    if obj is None:
        return None
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if dataclasses.is_dataclass(obj):
        return asdict(obj)
    return obj


def make_run_dir(now: datetime | None = None) -> Path:
    now = now or datetime.now(UTC)
    stamp = now.strftime("%Y-%m-%dT%H%M%SZ")
    out = FIXTURES_ROOT / stamp
    out.mkdir(parents=True, exist_ok=True)
    return out


def write_run_fixtures(
    *,
    run_dir: Path,
    slack: SlackSnapshot | None = None,
    status_pages: dict[str, StatusPageSnapshot] | None = None,
    instatus: InStatusSnapshot | None = None,
    rubric: RubricResult | None = None,
    system_prompt: str | None = None,
    user_prompt: str | None = None,
    sweep_result: SweepResult | None = None,
    posted_messages: list[PostedMessage] | None = None,
    run_metadata: dict[str, Any] | None = None,
) -> Path:
    """Dump whatever we have so far. Safe to call with partial inputs."""
    run_dir.mkdir(parents=True, exist_ok=True)

    if slack is not None:
        _write_json(run_dir / "slack.json", _model_or_dataclass_dump(slack))
    if status_pages is not None:
        _write_json(
            run_dir / "status_pages.json",
            {name: _model_or_dataclass_dump(snap) for name, snap in status_pages.items()},
        )
    if instatus is not None:
        _write_json(run_dir / "instatus.json", _model_or_dataclass_dump(instatus))
    if rubric is not None:
        _write_text(run_dir / "rubric.md", rubric.markdown)
        _write_text(
            run_dir / "rubric_source.txt",
            f"source: {rubric.source}\npage_id: {rubric.page_id}\nerror: {rubric.error or ''}\n",
        )
    if system_prompt is not None:
        _write_text(run_dir / "system_prompt.txt", system_prompt)
    if user_prompt is not None:
        _write_text(run_dir / "prompt.txt", user_prompt)
    if sweep_result is not None:
        _write_json(run_dir / "response.json", _model_or_dataclass_dump(sweep_result))
    if posted_messages is not None:
        _write_json(
            run_dir / "posted_messages.json",
            [_model_or_dataclass_dump(m) for m in posted_messages],
        )
    if run_metadata is not None:
        _write_json(run_dir / "run_metadata.json", run_metadata)

    return run_dir
