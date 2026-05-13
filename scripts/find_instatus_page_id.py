"""Print every InStatus page your API key has access to, including the cuid.

The InStatus dashboard doesn't surface the canonical page ID (a cuid like
`clk1234abcd5678efgh`) anywhere obvious, but the API does. Run this once,
grab the cuid for `ev.energy Integration Status`, and stash it as the
`INSTATUS_PAGE_ID` repo secret.

Usage:
    export INSTATUS_API_KEY=...   # from instatus.com → API keys
    python scripts/find_instatus_page_id.py
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

API_BASE = "https://api.instatus.com"


def main() -> int:
    api_key = os.environ.get("INSTATUS_API_KEY")
    if not api_key:
        print("error: set INSTATUS_API_KEY in your env first", file=sys.stderr)
        return 1

    req = urllib.request.Request(
        f"{API_BASE}/v1/pages",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            pages = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"error: {e.code} {e.reason}\n{e.read().decode(errors='replace')}", file=sys.stderr)
        return 1

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
