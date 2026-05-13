"""InStatus API gatherer.

Fetches active (open) incidents and the full component list from ev.energy's
own InStatus page so the reasoning step can:
  1. Detect duplicates of existing incidents.
  2. Pick `affected_components` from a closed list of real component names.

InStatus sits behind Cloudflare and rejects Python's default User-Agent with
403/cf-1010, so we set a browser-like UA preemptively.

Failures are captured on the returned snapshot rather than raised — partial
context is better than no context.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field

from incident_agent.config import CONFIG_DIR, REPO_ROOT

LOG = logging.getLogger(__name__)

INSTATUS_TIMEOUT_SECONDS = 20.0

# Cloudflare in front of api.instatus.com 403s the default Python UA.
INSTATUS_USER_AGENT = "ev-energy-incident-agent/0.1 (+gather/instatus.py)"

# Per the plan: only "open" incident statuses are interesting.
OPEN_INCIDENT_STATUSES = ("INVESTIGATING", "IDENTIFIED", "MONITORING")


class InStatusComponent(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    name: str
    status: str | None = None
    description: str | None = None


class InStatusIncidentUpdate(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str | None = None
    message: str | None = None
    status: str | None = None
    started: str | None = None
    notify: bool | None = None


class InStatusIncident(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    name: str
    status: str
    started: str | None = None
    resolved: str | None = None
    url: str | None = None
    components: list[InStatusComponent] = Field(default_factory=list)
    updates: list[InStatusIncidentUpdate] = Field(default_factory=list)


class InStatusSnapshot(BaseModel):
    page_id: str
    base_url: str
    active_incidents: list[InStatusIncident]
    components: list[InStatusComponent]
    error: str | None = None


def _load_instatus_config() -> dict:
    cfg_path = CONFIG_DIR / "status_sources.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    return cfg.get("instatus", {}) or {}


def _auth_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "User-Agent": INSTATUS_USER_AGENT,
        "Accept": "application/json",
    }


def _fetch_json(client: httpx.Client, url: str, api_key: str) -> object:
    resp = client.get(
        url,
        headers=_auth_headers(api_key),
        timeout=INSTATUS_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_instatus_state(
    page_id: str,
    api_key: str,
    base_url: str = "https://api.instatus.com",
    client: httpx.Client | None = None,
) -> InStatusSnapshot:
    """Fetch open incidents + components. Never raises — see `snapshot.error`."""
    base = base_url.rstrip("/")
    owns_client = client is None
    if owns_client:
        client = httpx.Client()

    try:
        try:
            status_filter = ",".join(OPEN_INCIDENT_STATUSES)
            incidents_raw = _fetch_json(
                client,
                f"{base}/v1/{page_id}/incidents?status={status_filter}",
                api_key,
            )
            components_raw = _fetch_json(
                client,
                f"{base}/v1/{page_id}/components",
                api_key,
            )
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            LOG.warning("instatus: fetch failed: %s", exc)
            return InStatusSnapshot(
                page_id=page_id,
                base_url=base,
                active_incidents=[],
                components=[],
                error=f"{type(exc).__name__}: {exc}",
            )
    finally:
        if owns_client:
            client.close()

    incidents_list = incidents_raw if isinstance(incidents_raw, list) else []
    components_list = components_raw if isinstance(components_raw, list) else []

    # Belt-and-braces: filter again client-side in case the API returns
    # resolved incidents (some Statuspage-likes ignore the status filter).
    parsed_incidents: list[InStatusIncident] = []
    for inc in incidents_list:
        if (inc.get("status") or "").upper() not in OPEN_INCIDENT_STATUSES:
            continue
        try:
            parsed_incidents.append(InStatusIncident.model_validate(inc))
        except Exception as exc:  # noqa: BLE001 — schema drift tolerance
            LOG.warning("instatus: skipping unparseable incident %s: %s",
                        inc.get("id"), exc)

    parsed_components: list[InStatusComponent] = []
    for comp in components_list:
        try:
            parsed_components.append(InStatusComponent.model_validate(comp))
        except Exception as exc:  # noqa: BLE001
            LOG.warning("instatus: skipping unparseable component %s: %s",
                        comp.get("id"), exc)

    return InStatusSnapshot(
        page_id=page_id,
        base_url=base,
        active_incidents=parsed_incidents,
        components=parsed_components,
    )


def gather_instatus_state() -> InStatusSnapshot:
    """Config + env entry point used by main.py."""
    cfg = _load_instatus_config()
    page_id_env = cfg.get("page_id_env", "INSTATUS_PAGE_ID")
    base_url = cfg.get("base_url", "https://api.instatus.com")

    page_id = os.environ.get(page_id_env, "").strip()
    api_key = os.environ.get("INSTATUS_API_KEY", "").strip()

    if not page_id or not api_key:
        missing = [n for n, v in (("INSTATUS_API_KEY", api_key),
                                   (page_id_env, page_id)) if not v]
        return InStatusSnapshot(
            page_id=page_id or "(unset)",
            base_url=base_url,
            active_incidents=[],
            components=[],
            error=f"missing env vars: {', '.join(missing)}",
        )

    return fetch_instatus_state(page_id=page_id, api_key=api_key, base_url=base_url)


# ── Local smoke / fixture-capture CLI ────────────────────────────────────────

def _cli() -> int:
    parser = argparse.ArgumentParser(description="Print InStatus gather output.")
    parser.add_argument(
        "--save-fixtures",
        action="store_true",
        help="Also save raw JSON under tests/fixtures/instatus/.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    snapshot = gather_instatus_state()
    print(json.dumps(snapshot.model_dump(), indent=2, default=str))

    if args.save_fixtures and snapshot.error is None:
        out_dir: Path = REPO_ROOT / "tests" / "fixtures" / "instatus"
        out_dir.mkdir(parents=True, exist_ok=True)

        cfg = _load_instatus_config()
        page_id = os.environ.get(cfg.get("page_id_env", "INSTATUS_PAGE_ID"), "").strip()
        api_key = os.environ.get("INSTATUS_API_KEY", "").strip()
        base = cfg.get("base_url", "https://api.instatus.com").rstrip("/")
        status_filter = ",".join(OPEN_INCIDENT_STATUSES)

        with httpx.Client() as client:
            try:
                inc_raw = _fetch_json(
                    client,
                    f"{base}/v1/{page_id}/incidents?status={status_filter}",
                    api_key,
                )
                comp_raw = _fetch_json(client, f"{base}/v1/{page_id}/components", api_key)
            except Exception as exc:  # noqa: BLE001
                print(f"WARN: fixture capture failed: {exc}", file=sys.stderr)
                return 1

        (out_dir / "incidents_open.json").write_text(json.dumps(inc_raw, indent=2), encoding="utf-8")
        (out_dir / "components.json").write_text(json.dumps(comp_raw, indent=2), encoding="utf-8")
        print(f"saved fixtures to {out_dir}", file=sys.stderr)

    return 0 if snapshot.error is None else 1


if __name__ == "__main__":
    raise SystemExit(_cli())
