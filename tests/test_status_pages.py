"""Tests for the Statuspage gatherer (Step 2)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from incident_agent.gather.status_pages import (
    INCIDENT_LOOKBACK_DAYS,
    _filter_recent,
    fetch_statuspage_source,
    gather_status_pages,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "status_pages"
FIXED_NOW = datetime(2026, 5, 13, 9, 0, tzinfo=UTC)


def _load(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


# ── _filter_recent ───────────────────────────────────────────────────────────

def test_filter_recent_drops_old_and_keeps_recent():
    incidents = _load("example_incidents.json")["incidents"]
    kept = _filter_recent(incidents, INCIDENT_LOOKBACK_DAYS, now=FIXED_NOW)
    kept_ids = {inc["id"] for inc in kept}
    assert "inc-recent-1" in kept_ids
    assert "inc-recent-2" in kept_ids
    assert "inc-old-1" not in kept_ids


def test_filter_recent_skips_undated_entries():
    kept = _filter_recent(
        [{"id": "no-date", "name": "??"}],
        INCIDENT_LOOKBACK_DAYS,
        now=FIXED_NOW,
    )
    assert kept == []


# ── fetch_statuspage_source ──────────────────────────────────────────────────

@respx.mock
def test_fetch_statuspage_happy_path():
    base = "https://status.example.com"
    respx.get(f"{base}/api/v2/status.json").mock(
        return_value=httpx.Response(200, json=_load("example_status_all_good.json"))
    )
    respx.get(f"{base}/api/v2/incidents.json").mock(
        return_value=httpx.Response(200, json=_load("example_incidents.json"))
    )

    snap = fetch_statuspage_source(
        {"name": "example", "base_url": base},
        now=FIXED_NOW,
    )

    assert snap.error is None
    assert snap.source == "example"
    assert snap.current_status == "All Systems Operational"
    assert snap.current_indicator == "none"
    assert len(snap.recent_incidents) == 2
    assert snap.recent_incidents[0].id == "inc-recent-1"
    assert snap.recent_incidents[0].incident_updates[0].body.startswith(
        "We are monitoring"
    )


@respx.mock
def test_fetch_statuspage_degraded_indicator():
    base = "https://status.example.com"
    respx.get(f"{base}/api/v2/status.json").mock(
        return_value=httpx.Response(200, json=_load("example_status_degraded.json"))
    )
    respx.get(f"{base}/api/v2/incidents.json").mock(
        return_value=httpx.Response(200, json={"incidents": []})
    )

    snap = fetch_statuspage_source(
        {"name": "example", "base_url": base},
        now=FIXED_NOW,
    )

    assert snap.error is None
    assert snap.current_indicator == "major"
    assert snap.current_status == "Partial System Outage"
    assert snap.recent_incidents == []


@respx.mock
def test_fetch_statuspage_http_failure_is_captured_not_raised():
    base = "https://status.broken.example"
    respx.get(f"{base}/api/v2/status.json").mock(return_value=httpx.Response(503))

    snap = fetch_statuspage_source(
        {"name": "broken", "base_url": base},
        now=FIXED_NOW,
    )

    assert snap.error is not None
    assert "503" in snap.error or "Server" in snap.error
    assert snap.current_status == "unknown"
    assert snap.current_indicator == "unknown"
    assert snap.recent_incidents == []


@respx.mock
def test_fetch_statuspage_network_error_is_captured():
    base = "https://status.timeout.example"
    respx.get(f"{base}/api/v2/status.json").mock(
        side_effect=httpx.ConnectError("name resolution failed")
    )

    snap = fetch_statuspage_source(
        {"name": "timeout", "base_url": base},
        now=FIXED_NOW,
    )

    assert snap.error is not None
    assert snap.current_status == "unknown"


@respx.mock
def test_fetch_statuspage_trims_trailing_slash_on_base_url():
    base = "https://status.example.com"
    respx.get(f"{base}/api/v2/status.json").mock(
        return_value=httpx.Response(200, json=_load("example_status_all_good.json"))
    )
    respx.get(f"{base}/api/v2/incidents.json").mock(
        return_value=httpx.Response(200, json={"incidents": []})
    )

    snap = fetch_statuspage_source(
        {"name": "example", "base_url": f"{base}/"},
        now=FIXED_NOW,
    )
    assert snap.error is None


# ── gather_status_pages (config-driven) ──────────────────────────────────────

@respx.mock
def test_gather_status_pages_reads_config(monkeypatch):
    """Smoke test: gather_status_pages hits each source from config/status_sources.yaml."""
    # Mock both real configured sources (smartcar + enode).
    for base in ("https://status.smartcar.com", "https://status.enode.com"):
        respx.get(f"{base}/api/v2/status.json").mock(
            return_value=httpx.Response(200, json=_load("example_status_all_good.json"))
        )
        respx.get(f"{base}/api/v2/incidents.json").mock(
            return_value=httpx.Response(200, json={"incidents": []})
        )

    result = gather_status_pages(now=FIXED_NOW)

    assert set(result.keys()) == {"smartcar", "enode"}
    for snap in result.values():
        assert snap.error is None
        assert snap.current_indicator == "none"


# ── Schema-tolerance / pydantic parsing ─────────────────────────────────────

@respx.mock
def test_unparseable_incident_is_skipped_not_fatal():
    """If Statuspage adds new required-shaped fields and we get garbage on one
    incident, the others should still come through."""
    base = "https://status.example.com"
    good = _load("example_incidents.json")["incidents"][0]
    # Inside lookback window so it survives _filter_recent, but missing the
    # required `name`/`status`/`impact`/`created_at` fields — pydantic should
    # reject it and the gatherer should log + drop it without raising.
    bad = {"id": "bad", "updated_at": "2026-05-12T22:30:00Z"}
    respx.get(f"{base}/api/v2/status.json").mock(
        return_value=httpx.Response(200, json=_load("example_status_all_good.json"))
    )
    respx.get(f"{base}/api/v2/incidents.json").mock(
        return_value=httpx.Response(
            200,
            json={"incidents": [good, bad]},
        )
    )

    snap = fetch_statuspage_source(
        {"name": "example", "base_url": base},
        now=FIXED_NOW,
    )

    assert snap.error is None
    assert len(snap.recent_incidents) == 1
    assert snap.recent_incidents[0].id == "inc-recent-1"


# Used by the gather smoke test above — fail fast if respx isn't pinned.
def test_respx_is_installed():
    assert pytest is not None
    assert respx is not None
