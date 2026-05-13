"""Smoke tests to keep the scaffold honest until real implementations land."""

from __future__ import annotations

from pathlib import Path

import incident_agent

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_package_imports():
    assert incident_agent.__version__


def test_repo_layout_is_present():
    expected = [
        "src/incident_agent/main.py",
        "src/incident_agent/config.py",
        "src/incident_agent/gather/slack.py",
        "src/incident_agent/gather/status_pages.py",
        "src/incident_agent/gather/instatus.py",
        "src/incident_agent/gather/rubric.py",
        "src/incident_agent/reason/claude.py",
        "src/incident_agent/reason/schema.py",
        "src/incident_agent/act/post_to_slack.py",
        "src/incident_agent/fixtures.py",
        "prompts/system.md",
        "prompts/user_template.md",
        "config/channels.yaml",
        "config/status_sources.yaml",
        "docs/rubric-starter.md",
        "docs/runbook.md",
        ".github/workflows/daily-sweep.yml",
    ]
    missing = [p for p in expected if not (REPO_ROOT / p).is_file()]
    assert not missing, f"Missing scaffold files: {missing}"
