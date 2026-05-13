"""Tests for the InStatus gatherer (Step 3)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from incident_agent.gather.instatus import (
    OPEN_INCIDENT_STATUSES,
    fetch_instatus_state,
    gather_instatus_state,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "instatus"

PAGE_ID = "cmtest123"
API_KEY = "test-api-key"
BASE = "https://api.instatus.com"
INCIDENTS_URL = f"{BASE}/v1/{PAGE_ID}/incidents?status={','.join(OPEN_INCIDENT_STATUSES)}"
COMPONENTS_URL = f"{BASE}/v1/{PAGE_ID}/components"


def _load(name: str):
    return json.loads((FIXTURE_DIR / name).read_text())


# ── Happy path ───────────────────────────────────────────────────────────────

@respx.mock
def test_fetch_instatus_happy_path():
    respx.get(INCIDENTS_URL).mock(
        return_value=httpx.Response(200, json=_load("incidents_open_example.json"))
    )
    respx.get(COMPONENTS_URL).mock(
        return_value=httpx.Response(200, json=_load("components_example.json"))
    )

    snap = fetch_instatus_state(page_id=PAGE_ID, api_key=API_KEY, base_url=BASE)

    assert snap.error is None
    assert snap.page_id == PAGE_ID
    assert len(snap.active_incidents) == 1
    assert snap.active_incidents[0].id == "inc_open_1"
    assert snap.active_incidents[0].status == "MONITORING"
    assert len(snap.active_incidents[0].updates) == 2
    assert len(snap.components) == 5
    assert {c.name for c in snap.components} == {
        "Smartcar — API",
        "Smartcar — Webhooks",
        "Enode — API",
        "Enode — Webhooks",
        "Smart charging dispatch",
    }


@respx.mock
def test_fetch_instatus_no_open_incidents():
    respx.get(INCIDENTS_URL).mock(
        return_value=httpx.Response(200, json=_load("incidents_open_empty.json"))
    )
    respx.get(COMPONENTS_URL).mock(
        return_value=httpx.Response(200, json=_load("components_example.json"))
    )

    snap = fetch_instatus_state(page_id=PAGE_ID, api_key=API_KEY, base_url=BASE)

    assert snap.error is None
    assert snap.active_incidents == []
    assert len(snap.components) == 5


# ── Auth header is set correctly ─────────────────────────────────────────────

@respx.mock
def test_fetch_instatus_sends_bearer_token():
    captured = respx.get(INCIDENTS_URL).mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.get(COMPONENTS_URL).mock(return_value=httpx.Response(200, json=[]))

    fetch_instatus_state(page_id=PAGE_ID, api_key=API_KEY, base_url=BASE)

    request = captured.calls[0].request
    assert request.headers["Authorization"] == f"Bearer {API_KEY}"
    # Cloudflare bypass: don't use Python's default UA.
    assert "ev-energy-incident-agent" in request.headers["User-Agent"]


# ── Failure capture ──────────────────────────────────────────────────────────

@respx.mock
def test_fetch_instatus_cloudflare_403_is_captured():
    respx.get(INCIDENTS_URL).mock(return_value=httpx.Response(403, text="error code: 1010"))

    snap = fetch_instatus_state(page_id=PAGE_ID, api_key=API_KEY, base_url=BASE)

    assert snap.error is not None
    assert "403" in snap.error or "Forbidden" in snap.error
    assert snap.active_incidents == []
    assert snap.components == []


@respx.mock
def test_fetch_instatus_network_error_is_captured():
    respx.get(INCIDENTS_URL).mock(side_effect=httpx.ConnectError("dns failure"))

    snap = fetch_instatus_state(page_id=PAGE_ID, api_key=API_KEY, base_url=BASE)

    assert snap.error is not None
    assert snap.active_incidents == []


# ── Resilient parsing ────────────────────────────────────────────────────────

@respx.mock
def test_fetch_instatus_filters_resolved_incidents_returned_by_api():
    """Some APIs ignore the status filter and return resolved too; we must drop them."""
    incidents = [
        {
            "id": "open_1",
            "name": "Real open",
            "status": "INVESTIGATING",
        },
        {
            "id": "resolved_1",
            "name": "Already done",
            "status": "RESOLVED",
        },
    ]
    respx.get(INCIDENTS_URL).mock(return_value=httpx.Response(200, json=incidents))
    respx.get(COMPONENTS_URL).mock(return_value=httpx.Response(200, json=[]))

    snap = fetch_instatus_state(page_id=PAGE_ID, api_key=API_KEY, base_url=BASE)

    assert [i.id for i in snap.active_incidents] == ["open_1"]


@respx.mock
def test_unparseable_incident_is_skipped_not_fatal():
    incidents = [
        # Missing required `name`; pydantic rejects this one.
        {"id": "bad", "status": "INVESTIGATING"},
        # Well-formed.
        {"id": "good", "name": "Good", "status": "INVESTIGATING"},
    ]
    respx.get(INCIDENTS_URL).mock(return_value=httpx.Response(200, json=incidents))
    respx.get(COMPONENTS_URL).mock(return_value=httpx.Response(200, json=[]))

    snap = fetch_instatus_state(page_id=PAGE_ID, api_key=API_KEY, base_url=BASE)

    assert snap.error is None
    assert [i.id for i in snap.active_incidents] == ["good"]


@respx.mock
def test_unparseable_component_is_skipped_not_fatal():
    respx.get(INCIDENTS_URL).mock(return_value=httpx.Response(200, json=[]))
    respx.get(COMPONENTS_URL).mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": "bad"},  # missing `name`
                {"id": "good", "name": "Good"},
            ],
        )
    )

    snap = fetch_instatus_state(page_id=PAGE_ID, api_key=API_KEY, base_url=BASE)

    assert snap.error is None
    assert [c.id for c in snap.components] == ["good"]


# ── env-driven entry point ───────────────────────────────────────────────────

def test_gather_instatus_state_reports_missing_env(monkeypatch):
    monkeypatch.delenv("INSTATUS_API_KEY", raising=False)
    monkeypatch.delenv("INSTATUS_PAGE_ID", raising=False)

    snap = gather_instatus_state()

    assert snap.error is not None
    assert "INSTATUS_API_KEY" in snap.error
    assert "INSTATUS_PAGE_ID" in snap.error


@respx.mock
def test_gather_instatus_state_reads_env(monkeypatch):
    monkeypatch.setenv("INSTATUS_API_KEY", API_KEY)
    monkeypatch.setenv("INSTATUS_PAGE_ID", PAGE_ID)

    respx.get(INCIDENTS_URL).mock(return_value=httpx.Response(200, json=[]))
    respx.get(COMPONENTS_URL).mock(
        return_value=httpx.Response(200, json=_load("components_example.json"))
    )

    snap = gather_instatus_state()

    assert snap.error is None
    assert snap.page_id == PAGE_ID
    assert len(snap.components) == 5


# Used to fail fast if respx isn't installed.
def test_respx_is_present():
    assert pytest is not None
    assert respx is not None
