"""Print every InStatus page your API key has access to, including the cuid.

The InStatus dashboard doesn't surface the canonical page ID (a cuid like
`clk1234abcd5678efgh`) anywhere obvious, but the API does. Run this once,
grab the cuid for `ev.energy Integration Status`, and stash it as the
`INSTATUS_PAGE_ID` repo secret.

Usage (PowerShell):
    $env:INSTATUS_API_KEY = "<paste-key>"
    python scripts/find_instatus_page_id.py

Usage (bash/zsh):
    export INSTATUS_API_KEY=<paste-key>
    python scripts/find_instatus_page_id.py
"""

from __future__ import annotations

import os
import sys

import httpx

API_BASE = "https://api.instatus.com"
# Use a real User-Agent — Cloudflare in front of InStatus rejects the default
# Python-urllib UA (returns 403 with cf error 1010).
USER_AGENT = "ev-energy-incident-agent/0.1 (+find_instatus_page_id)"


def main() -> int:
    api_key = os.environ.get("INSTATUS_API_KEY")
    if not api_key:
        print("error: set INSTATUS_API_KEY in your env first", file=sys.stderr)
        return 1

    try:
        resp = httpx.get(
            f"{API_BASE}/v1/pages",
            headers={
                "Authorization": f"Bearer {api_key}",
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            },
            timeout=20.0,
        )
    except httpx.HTTPError as exc:
        print(f"error: network failure: {exc}", file=sys.stderr)
        return 1

    if resp.status_code != 200:
        print(f"error: HTTP {resp.status_code}", file=sys.stderr)
        print(resp.text[:500], file=sys.stderr)
        return 1

    pages = resp.json()
    if not pages:
        print("(no pages returned — does this API key have any page access?)")
        return 1

    print(f"{'PAGE ID (cuid)':<32}  {'SUBDOMAIN':<40}  NAME")
    print(f"{'-' * 32}  {'-' * 40}  {'-' * 40}")
    for page in pages:
        page_id = page.get("id", "?")
        subdomain = page.get("subdomain", "?")
        name = page.get("name", "?")
        print(f"{page_id:<32}  {subdomain:<40}  {name}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
