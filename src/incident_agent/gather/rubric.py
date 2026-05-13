"""Notion rubric gatherer.

Fetches the decision rubric from Notion at every run (so Akhil can edit
it without a code commit) and converts the page blocks to markdown. If
Notion is unreachable, the API key is missing, or the page can't be read,
falls back to `docs/rubric-starter.md` in the repo so the bot still has
something to reason against.

The rubric is the most important non-signal input — without it Claude is
guessing about ev.energy's bar for "incident-worthy."
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass

from incident_agent.config import DOCS_DIR

LOG = logging.getLogger(__name__)

FALLBACK_RUBRIC_PATH = DOCS_DIR / "rubric-starter.md"


@dataclass(frozen=True)
class RubricResult:
    markdown: str
    source: str           # "notion" | "file" | "missing"
    page_id: str | None
    error: str | None = None


def _read_fallback() -> str:
    try:
        return FALLBACK_RUBRIC_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        LOG.error("rubric: fallback file unreadable: %s", exc)
        return ""


def _blocks_to_markdown(client, page_id: str) -> str:
    """Convert a Notion page's blocks into a markdown string.

    Handles the block types the rubric uses: headings, paragraphs, bulleted
    lists, numbered lists, dividers, quotes, code, and inline annotations.
    Unknown block types render as their plain text (or are skipped silently
    if they have none).
    """
    parts: list[str] = []
    _walk_blocks(client, page_id, parts, indent=0)
    return "\n\n".join(p for p in parts if p.strip())


def _walk_blocks(client, parent_id: str, out: list[str], indent: int) -> None:
    cursor: str | None = None
    while True:
        kwargs = {"block_id": parent_id, "page_size": 100}
        if cursor:
            kwargs["start_cursor"] = cursor
        resp = client.blocks.children.list(**kwargs)
        for block in resp.get("results", []):
            rendered = _render_block(block, indent)
            if rendered is not None:
                out.append(rendered)
            if block.get("has_children"):
                _walk_blocks(client, block["id"], out, indent + 1)
        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")


def _render_block(block: dict, indent: int) -> str | None:
    btype = block.get("type")
    if btype is None:
        return None
    body = block.get(btype, {}) or {}
    text = _rich_text_to_md(body.get("rich_text", []))
    prefix = "  " * indent

    if btype == "heading_1":
        return f"# {text}"
    if btype == "heading_2":
        return f"## {text}"
    if btype == "heading_3":
        return f"### {text}"
    if btype == "paragraph":
        return f"{prefix}{text}" if text else None
    if btype == "bulleted_list_item":
        return f"{prefix}- {text}"
    if btype == "numbered_list_item":
        return f"{prefix}1. {text}"
    if btype == "to_do":
        checked = "x" if body.get("checked") else " "
        return f"{prefix}- [{checked}] {text}"
    if btype == "quote":
        return f"> {text}"
    if btype == "callout":
        return f"> {text}"
    if btype == "divider":
        return "---"
    if btype == "code":
        lang = body.get("language", "")
        return f"```{lang}\n{text}\n```"
    if btype == "toggle":
        return f"{prefix}- {text}" if text else None
    # Fall back to whatever rich_text we have, if any.
    return f"{prefix}{text}" if text else None


def _rich_text_to_md(rich: list[dict]) -> str:
    out: list[str] = []
    for chunk in rich:
        plain = chunk.get("plain_text", "")
        if not plain:
            continue
        ann = chunk.get("annotations", {}) or {}
        if ann.get("code"):
            plain = f"`{plain}`"
        if ann.get("bold"):
            plain = f"**{plain}**"
        if ann.get("italic"):
            plain = f"_{plain}_"
        href = chunk.get("href")
        if href:
            plain = f"[{plain}]({href})"
        out.append(plain)
    return "".join(out)


def fetch_rubric_from_notion(api_key: str, page_id: str) -> str:
    """Fetch and convert. Raises on any error; caller handles the fallback."""
    # Imported lazily so unit tests can monkeypatch without notion-client installed.
    from notion_client import Client

    client = Client(auth=api_key)
    # Strip dashes so dashed or undashed page IDs both work.
    return _blocks_to_markdown(client, page_id.replace("-", ""))


def gather_rubric() -> RubricResult:
    """Top-level entry. Tries Notion, falls back to the local file."""
    api_key = os.environ.get("NOTION_API_KEY", "").strip()
    page_id = os.environ.get("NOTION_RUBRIC_PAGE_ID", "").strip()

    if not api_key or not page_id:
        missing = [n for n, v in (("NOTION_API_KEY", api_key),
                                   ("NOTION_RUBRIC_PAGE_ID", page_id)) if not v]
        LOG.info("rubric: env vars %s unset, using file fallback",
                 ", ".join(missing))
        return RubricResult(
            markdown=_read_fallback(),
            source="file",
            page_id=None,
            error=f"env vars unset: {', '.join(missing)}",
        )

    try:
        md = fetch_rubric_from_notion(api_key, page_id)
        if not md.strip():
            raise RuntimeError("notion returned empty rubric")
        return RubricResult(markdown=md, source="notion", page_id=page_id)
    except Exception as exc:  # noqa: BLE001 — fallback covers anything
        LOG.warning("rubric: notion fetch failed, using file fallback: %s", exc)
        return RubricResult(
            markdown=_read_fallback(),
            source="file",
            page_id=page_id,
            error=f"{type(exc).__name__}: {exc}",
        )


# ── Local smoke CLI ──────────────────────────────────────────────────────────

def _cli() -> int:
    parser = argparse.ArgumentParser(
        description="Print the rubric the bot would use this run."
    )
    parser.add_argument(
        "--force-file",
        action="store_true",
        help="Skip Notion and print the file fallback instead.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.force_file:
        text = _read_fallback()
        print(f"-- source: docs/rubric-starter.md ({len(text)} chars) --", file=sys.stderr)
        print(text)
        return 0

    result = gather_rubric()
    print(f"-- source: {result.source} "
          f"(page_id={result.page_id}, {len(result.markdown)} chars) --",
          file=sys.stderr)
    if result.error:
        print(f"-- note: {result.error} --", file=sys.stderr)
    print(result.markdown)
    return 0 if result.source == "notion" else 1


if __name__ == "__main__":
    raise SystemExit(_cli())
