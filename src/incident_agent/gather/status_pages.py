"""Statuspage JSON gatherer for Smartcar and Enode.

Both providers expose unauthenticated v2 endpoints:
    {base_url}/api/v2/status.json       — overall indicator + description
    {base_url}/api/v2/incidents.json    — recent incidents w/ updates

Failures (network, non-2xx, parse) are captured per-source on the returned
snapshot rather than raised — the agent should be able to reason with partial
context.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field

from incident_agent.config import CONFIG_DIR, REPO_ROOT

LOG = logging.getLogger(__name__)

STATUSPAGE_TIMEOUT_SECONDS = 15.0
INCIDENT_LOOKBACK_DAYS = 7


class IncidentUpdate(BaseModel):
    model_config = ConfigDict(extra="ignore")
    body: str
    status: str
    created_at: str


class StatusPageIncident(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    name: str
    status: str
    impact: str
    created_at: str
    updated_at: str
    resolved_at: str | None = None
    shortlink: str | None = None
    incident_updates: list[IncidentUpdate] = Field(default_factory=list)


class StatusPageSnapshot(BaseModel):
    source: str
    base_url: str
    current_status: str          # e.g. "All Systems Operational"
    current_indicator: str       # none | minor | major | critical | unknown
    recent_incidents: list[StatusPageIncident]
    error: str | None = None


def _load_sources() -> list[dict[str, Any]]:
    cfg_path = CONFIG_DIR / "status_sources.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    return list(cfg.get("external_status_pages", []) or [])


def _fetch_json(client: httpx.Client, url: str) -> Any:
    resp = client.get(url, timeout=STATUSPAGE_TIMEOUT_SECONDS)
    resp.raise_for_status()
    return resp.json()


def _filter_recent(
    incidents: list[dict[str, Any]],
    lookback_days: int,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=lookback_days)
    out: list[dict[str, Any]] = []
    for inc in incidents:
        ts = inc.get("updated_at") or inc.get("created_at")
        if not ts:
            continue
        try:
            parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed >= cutoff:
            out.append(inc)
    return out


def fetch_statuspage_source(
    source: dict[str, Any],
    client: httpx.Client | None = None,
    now: datetime | None = None,
) -> StatusPageSnapshot:
    """Fetch one source. Never raises — failures land in `snapshot.error`."""
    name = source["name"]
    base_url = source["base_url"].rstrip("/")

    owns_client = client is None
    if owns_client:
        client = httpx.Client()

    try:
        try:
            status_data = _fetch_json(client, f"{base_url}/api/v2/status.json")
            incidents_data = _fetch_json(client, f"{base_url}/api/v2/incidents.json")
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            LOG.warning("statuspage %s: fetch failed: %s", name, exc)
            return StatusPageSnapshot(
                source=name,
                base_url=base_url,
                current_status="unknown",
                current_indicator="unknown",
                recent_incidents=[],
                error=f"{type(exc).__name__}: {exc}",
            )
    finally:
        if owns_client:
            client.close()

    status_obj = status_data.get("status", {}) or {}
    raw_incidents = incidents_data.get("incidents", []) or []
    recent_raw = _filter_recent(raw_incidents, INCIDENT_LOOKBACK_DAYS, now=now)

    parsed_incidents: list[StatusPageIncident] = []
    for inc in recent_raw:
        try:
            parsed_incidents.append(StatusPageIncident.model_validate(inc))
        except Exception as exc:  # noqa: BLE001 — keep partial data on schema drift
            LOG.warning("statuspage %s: skipping unparseable incident %s: %s",
                        name, inc.get("id"), exc)

    return StatusPageSnapshot(
        source=name,
        base_url=base_url,
        current_status=status_obj.get("description", "unknown"),
        current_indicator=status_obj.get("indicator", "unknown"),
        recent_incidents=parsed_incidents,
    )


def gather_status_pages(now: datetime | None = None) -> dict[str, StatusPageSnapshot]:
    """Fetch every source listed in config/status_sources.yaml."""
    sources = _load_sources()
    with httpx.Client() as client:
        return {
            s["name"]: fetch_statuspage_source(s, client=client, now=now)
            for s in sources
        }


# ── Local smoke / fixture-capture CLI ────────────────────────────────────────

def _cli() -> int:
    parser = argparse.ArgumentParser(description="Print status-page gather output.")
    parser.add_argument(
        "--save-fixtures",
        action="store_true",
        help="Also save raw JSON under tests/fixtures/status_pages/.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    snapshots = gather_status_pages()
    print(json.dumps(
        {name: snap.model_dump() for name, snap in snapshots.items()},
        indent=2,
        default=str,
    ))

    if args.save_fixtures:
        out_dir: Path = REPO_ROOT / "tests" / "fixtures" / "status_pages"
        out_dir.mkdir(parents=True, exist_ok=True)
        with httpx.Client() as client:
            for source in _load_sources():
                name = source["name"]
                base = source["base_url"].rstrip("/")
                try:
                    s = _fetch_json(client, f"{base}/api/v2/status.json")
                    i = _fetch_json(client, f"{base}/api/v2/incidents.json")
                except Exception as exc:  # noqa: BLE001
                    print(f"WARN: could not capture {name}: {exc}", file=sys.stderr)
                    continue
                (out_dir / f"{name}_status.json").write_text(json.dumps(s, indent=2), encoding="utf-8")
                (out_dir / f"{name}_incidents.json").write_text(json.dumps(i, indent=2), encoding="utf-8")
                print(f"saved fixtures for {name}", file=sys.stderr)

    return 0 if all(s.error is None for s in snapshots.values()) else 1


if __name__ == "__main__":
    raise SystemExit(_cli())
