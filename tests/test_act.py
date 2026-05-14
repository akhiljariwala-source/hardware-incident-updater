"""Tests for the Slack posting layer (Step 7)."""

from __future__ import annotations

import json

from incident_agent.act.post_to_slack import (
    build_all_clear_blocks,
    build_candidate_blocks,
    build_watching_blocks,
    post_sweep_result,
)
from incident_agent.reason.schema import IncidentCandidate, Signal, SweepResult, WatchingItem


def _candidate(**overrides) -> IncidentCandidate:
    defaults = dict(
        title="Mercedes elevated error rate",
        severity="investigating",
        affected_components=["Vehicles"],
        affected_scope="Mercedes vehicles via Enode, ~5% of fleet",
        proposed_external_title="Connectivity issues affecting Mercedes vehicles",
        proposed_external_description="We are investigating elevated error rates on Mercedes vehicle connectivity.",
        supporting_signals=[
            Signal(
                source="enode status page",
                excerpt="Mercedes: Elevated error rate",
                timestamp="2026-05-12T18:34:12Z",
                permalink="https://status.enode.com/incidents/abc",
            ),
        ],
        confidence=0.7,
        reasoning="Enode publicly posted a degradation matching the rubric's Investigating tier.",
    )
    defaults.update(overrides)
    return IncidentCandidate(**defaults)


# ── Test doubles ────────────────────────────────────────────────────────────

class _FakeClient:
    def __init__(self, raise_on_kinds: set[str] | None = None):
        self.posts: list[dict] = []
        self.permalink_calls: list[dict] = []
        self._raise_on_kinds = raise_on_kinds or set()
        self._counter = 0

    def chat_postMessage(self, *, channel, blocks, text):
        # Use the first block's text to identify the kind we're posting.
        header_text = ""
        if blocks and blocks[0].get("type") == "header":
            header_text = blocks[0].get("text", {}).get("text", "")
        elif blocks:
            header_text = blocks[0].get("text", {}).get("text", "")
        for kind_marker in self._raise_on_kinds:
            if kind_marker in header_text:
                raise RuntimeError(f"channel_not_found: {kind_marker}")
        self._counter += 1
        ts = f"{self._counter}.000000"
        self.posts.append({"channel": channel, "blocks": blocks, "text": text, "ts": ts})
        return {"ok": True, "ts": ts, "channel": channel}

    def chat_getPermalink(self, *, channel, message_ts):
        self.permalink_calls.append({"channel": channel, "message_ts": message_ts})
        return {"ok": True, "permalink": f"https://example.slack.com/archives/{channel}/p{message_ts.replace('.', '')}"}


# ── Block builders ──────────────────────────────────────────────────────────

def test_build_candidate_blocks_has_all_required_sections():
    cand = _candidate()
    blocks, fallback = build_candidate_blocks(cand)

    types = [b["type"] for b in blocks]
    assert "header" in types
    assert "section" in types
    assert "divider" in types
    assert "context" in types
    assert "Connectivity issues" in fallback

    # The body should include the title, description, components, scope, confidence.
    rendered = json.dumps(blocks, ensure_ascii=False)
    assert "Connectivity issues affecting Mercedes vehicles" in rendered
    assert "elevated error rates on Mercedes" in rendered
    assert "Vehicles" in rendered
    assert "0.70" in rendered  # confidence formatted to 2dp
    assert "investigating" in rendered
    assert "Enode publicly posted" in rendered
    # Signals come through with source, link, and ts marker.
    assert "Mercedes: Elevated error rate" in rendered
    assert "https://status.enode.com/incidents/abc" in rendered
    assert "ts:2026-05-12T18:34:12Z" in rendered


def test_build_candidate_blocks_handles_long_excerpts_and_missing_data():
    long = "a" * 500
    cand = _candidate(
        affected_components=[],
        duplicates_existing_incident=None,
        supporting_signals=[Signal(source="", excerpt=long, timestamp="0", permalink=None)],
    )
    blocks, _ = build_candidate_blocks(cand)
    rendered = json.dumps(blocks, ensure_ascii=False)
    # Long excerpt is truncated with ellipsis.
    assert "aaaa…" in rendered or "…" in rendered
    # No components → placeholder rather than empty list.
    assert "_none cited_" in rendered
    assert "_no_" in rendered  # duplicates_existing_incident


def test_build_watching_blocks_renders_each_item():
    blocks, fallback = build_watching_blocks([
        WatchingItem(
            topic="Tesla session drop-offs",
            why_not_yet="Below the 30-min threshold.",
            signals_to_watch_for=["sustained drop-off >30 min", "3+ user reports"],
        ),
    ])
    rendered = json.dumps(blocks, ensure_ascii=False)
    assert "👀" in rendered
    assert "Tesla session drop-offs" in rendered
    assert "Below the 30-min threshold." in rendered
    assert "sustained drop-off >30 min" in rendered
    assert "Watching 1 item" in fallback


def test_build_all_clear_blocks_includes_run_metadata():
    sweep = SweepResult(candidates=[], watching=[], summary="No signals today.")
    blocks, fallback = build_all_clear_blocks(
        sweep,
        run_metadata={
            "github_run_id": "12345",
            "artifact_url": "https://github.com/akhiljariwala-source/repo/actions/runs/12345",
        },
    )
    rendered = json.dumps(blocks, ensure_ascii=False)
    assert "Daily sweep complete" in rendered
    assert "No signals today." in rendered
    assert "12345" in rendered
    assert "actions/runs/12345" in rendered
    assert "Daily sweep complete" in fallback


# ── post_sweep_result orchestration ────────────────────────────────────────

def test_post_sweep_result_quiet_day_posts_one_all_clear():
    sweep = SweepResult(candidates=[], watching=[], summary="All good.")
    client = _FakeClient()

    posted = post_sweep_result(sweep, channel="C_TEST", client=client)

    assert len(posted) == 1
    assert posted[0].kind == "all_clear"
    assert posted[0].error is None
    assert posted[0].ts == "1.000000"
    assert posted[0].permalink and posted[0].permalink.startswith("https://example.slack.com/")

    assert len(client.posts) == 1
    assert client.posts[0]["channel"] == "C_TEST"


def test_post_sweep_result_posts_each_candidate_top_level_then_watching():
    sweep = SweepResult(
        candidates=[_candidate(title="cand1"), _candidate(title="cand2")],
        watching=[WatchingItem(topic="t1", why_not_yet="reason", signals_to_watch_for=[])],
        summary="Two candidates, one watching.",
    )
    client = _FakeClient()

    posted = post_sweep_result(sweep, channel="C_TEST", client=client)

    assert [p.kind for p in posted] == ["candidate", "candidate", "watching"]
    assert all(p.error is None for p in posted)
    # Each is a separate top-level message (no thread_ts plumbed).
    for sent in client.posts:
        assert "thread_ts" not in sent
        assert sent["channel"] == "C_TEST"


def test_post_sweep_result_captures_per_post_failure_without_aborting():
    sweep = SweepResult(
        candidates=[_candidate(title="will fail"), _candidate(title="will succeed")],
        watching=[],
        summary="",
    )
    # First post raises; the rest must still go through.
    client = _FakeClient(raise_on_kinds={"investigating"})

    posted = post_sweep_result(sweep, channel="C_TEST", client=client)

    # Both candidates were attempted, but chat_postMessage raises on both
    # (raise_on_kinds matches on severity header text). Let's narrow:
    assert len(posted) == 2
    assert posted[0].error is not None
    assert posted[1].error is not None
    # Even on failure we get a captured message record back.
    for p in posted:
        assert p.kind == "candidate"
        assert p.fallback_text


def test_post_sweep_result_continues_when_only_one_post_fails():
    """A failure on the first candidate must not block the second."""
    sweep = SweepResult(
        candidates=[_candidate(title="cand1"), _candidate(title="cand2", severity="identified")],
        watching=[],
        summary="",
    )
    # Only "investigating" header lines raise — "identified" goes through.
    client = _FakeClient(raise_on_kinds={"investigating"})

    posted = post_sweep_result(sweep, channel="C_TEST", client=client)

    assert posted[0].error is not None and posted[0].ts is None
    assert posted[1].error is None and posted[1].ts == "1.000000"


def test_post_sweep_result_errors_when_channel_missing(monkeypatch):
    monkeypatch.delenv("POST_TO_CHANNEL_ID", raising=False)
    sweep = SweepResult(candidates=[], watching=[], summary="")
    try:
        post_sweep_result(sweep, channel="", client=_FakeClient())
    except ValueError as e:
        assert "POST_TO_CHANNEL_ID" in str(e)
    else:
        raise AssertionError("expected ValueError")
