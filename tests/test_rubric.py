"""Tests for the Notion rubric gatherer (Step 4)."""

from __future__ import annotations

from incident_agent.gather import rubric


def _rich(text: str, **annotations) -> dict:
    return {"plain_text": text, "annotations": annotations}


def _block(btype: str, text: str = "", **extras) -> dict:
    body = {"rich_text": [_rich(text)] if text else []}
    body.update(extras)
    return {"id": "blk_" + text[:8], "type": btype, btype: body, "has_children": False}


class _FakeBlocksChildren:
    def __init__(self, by_parent: dict[str, list[dict]]):
        self._by_parent = by_parent

    def list(self, block_id: str, page_size: int = 100, start_cursor: str | None = None):
        return {
            "results": self._by_parent.get(block_id, []),
            "has_more": False,
            "next_cursor": None,
        }


class _FakeNotionClient:
    def __init__(self, by_parent):
        self.blocks = type("B", (), {"children": _FakeBlocksChildren(by_parent)})()


# ── block-to-markdown conversion ─────────────────────────────────────────────

def test_blocks_to_markdown_handles_common_types():
    blocks = [
        _block("heading_1", "Decision Rubric"),
        _block("paragraph", "Source of truth for the daily agent."),
        _block("heading_2", "Severity tiers"),
        _block("heading_3", "Investigating"),
        _block("bulleted_list_item", "Provider posted degradation"),
        _block("bulleted_list_item", ">=3 internal reports"),
        _block("divider"),
        _block("quote", "Be conservative."),
        _block("paragraph", ""),  # empty paragraph should drop
    ]
    client = _FakeNotionClient({"page1": blocks})

    md = rubric._blocks_to_markdown(client, "page1")

    assert "# Decision Rubric" in md
    assert "## Severity tiers" in md
    assert "### Investigating" in md
    assert "- Provider posted degradation" in md
    assert "- >=3 internal reports" in md
    assert "> Be conservative." in md
    assert "---" in md
    # Empty paragraph between divider and quote shouldn't introduce extra blank lines.
    assert md.count("\n\n\n") == 0


def test_blocks_to_markdown_renders_inline_annotations():
    blocks = [{
        "id": "blk",
        "type": "paragraph",
        "has_children": False,
        "paragraph": {"rich_text": [
            {"plain_text": "Bold", "annotations": {"bold": True}},
            {"plain_text": " and ", "annotations": {}},
            {"plain_text": "italic", "annotations": {"italic": True}},
            {"plain_text": " with ", "annotations": {}},
            {"plain_text": "code", "annotations": {"code": True}},
            {"plain_text": " and ", "annotations": {}},
            {
                "plain_text": "a link",
                "annotations": {},
                "href": "https://example.com",
            },
        ]},
    }]
    client = _FakeNotionClient({"page1": blocks})
    md = rubric._blocks_to_markdown(client, "page1")
    assert "**Bold**" in md
    assert "_italic_" in md
    assert "`code`" in md
    assert "[a link](https://example.com)" in md


def test_blocks_to_markdown_recurses_into_children():
    nested_child = _block("paragraph", "Nested under toggle")
    toggle = _block("toggle", "Click to expand")
    toggle["has_children"] = True
    client = _FakeNotionClient({
        "page1": [toggle],
        toggle["id"]: [nested_child],
    })
    md = rubric._blocks_to_markdown(client, "page1")
    assert "Click to expand" in md
    assert "Nested under toggle" in md


# ── gather_rubric env-driven entry point ─────────────────────────────────────

def test_gather_rubric_falls_back_to_file_when_envvars_missing(monkeypatch):
    monkeypatch.delenv("NOTION_API_KEY", raising=False)
    monkeypatch.delenv("NOTION_RUBRIC_PAGE_ID", raising=False)

    result = rubric.gather_rubric()

    assert result.source == "file"
    assert result.markdown  # rubric-starter.md is present
    assert "Severity tiers" in result.markdown
    assert "NOTION_API_KEY" in (result.error or "")


def test_gather_rubric_uses_notion_when_envvars_present(monkeypatch):
    monkeypatch.setenv("NOTION_API_KEY", "secret")
    monkeypatch.setenv("NOTION_RUBRIC_PAGE_ID", "page-123")

    def _fake_fetch(api_key, page_id):
        assert api_key == "secret"
        # gather_rubric strips the dashes before calling, but fetch_rubric_from_notion
        # also strips inside. Either way, dashes shouldn't propagate to the caller.
        return "# Fresh from Notion\n\nReady."

    monkeypatch.setattr(rubric, "fetch_rubric_from_notion", _fake_fetch)

    result = rubric.gather_rubric()

    assert result.source == "notion"
    assert result.error is None
    assert "Fresh from Notion" in result.markdown
    assert result.page_id == "page-123"


def test_gather_rubric_falls_back_when_notion_raises(monkeypatch):
    monkeypatch.setenv("NOTION_API_KEY", "secret")
    monkeypatch.setenv("NOTION_RUBRIC_PAGE_ID", "page-123")

    def _boom(api_key, page_id):
        raise RuntimeError("notion API down")

    monkeypatch.setattr(rubric, "fetch_rubric_from_notion", _boom)

    result = rubric.gather_rubric()

    assert result.source == "file"
    assert result.error is not None
    assert "notion API down" in result.error
    # We still got something usable from the file.
    assert "Severity tiers" in result.markdown


def test_gather_rubric_falls_back_when_notion_returns_empty(monkeypatch):
    monkeypatch.setenv("NOTION_API_KEY", "secret")
    monkeypatch.setenv("NOTION_RUBRIC_PAGE_ID", "page-123")
    monkeypatch.setattr(rubric, "fetch_rubric_from_notion", lambda *a, **k: "   \n")

    result = rubric.gather_rubric()

    assert result.source == "file"
    assert "Severity tiers" in result.markdown
