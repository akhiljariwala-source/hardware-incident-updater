"""Block Kit message posting for incident proposals.

Builds Block Kit messages from a SweepResult and posts them to the configured
review channel. Threading model (build-time decision):
- Each candidate is its own top-level post — so triage reactions (✅/❌/🤔)
  are per-incident and easy to scan.
- The "watching" summary is one top-level post.
- The "all clear" message is one top-level post on quiet days, so Akhil knows
  the bot ran even when there's nothing to flag.

Posting failures are logged and captured on the returned PostedMessage list;
one bad post should never kill the rest of the sweep.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

from incident_agent.reason.schema import IncidentCandidate, SweepResult, WatchingItem

LOG = logging.getLogger(__name__)

SEVERITY_EMOJI = {
    "investigating": "🟡",
    "identified": "🟠",
    "major_outage": "🔴",
}

TRIAGE_FOOTER_LINES = [
    "_React to triage:_",
    "✅ approve — open the incident   ❌ dismiss — not incident-worthy   🤔 discuss",
]


@dataclass
class PostedMessage:
    """One Slack post attempt, captured for the fixtures audit trail."""

    kind: str               # "candidate" | "watching" | "all_clear"
    blocks: list[dict[str, Any]]
    fallback_text: str
    channel: str
    ts: str | None = None
    permalink: str | None = None
    error: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


# ── Block-Kit builders ──────────────────────────────────────────────────────

def _signal_bullet(idx: int, signal) -> str:
    excerpt = (signal.excerpt or "").strip().replace("\n", " ")
    if len(excerpt) > 240:
        excerpt = excerpt[:237] + "…"
    pieces = [f"• {excerpt}"]
    if signal.source:
        pieces.append(f"_({signal.source})_")
    if signal.permalink:
        pieces.append(f"<{signal.permalink}|link>")
    return " ".join(pieces) + f"  `ts:{signal.timestamp}`"


def build_candidate_blocks(cand: IncidentCandidate) -> tuple[list[dict[str, Any]], str]:
    """Returns (blocks, fallback_text)."""
    emoji = SEVERITY_EMOJI.get(cand.severity, "⚪")
    fallback = f"{emoji} Incident recommendation — {cand.proposed_external_title}"

    components_text = ", ".join(cand.affected_components) or "_none cited_"
    signals = (
        "\n".join(_signal_bullet(i, s) for i, s in enumerate(cand.supporting_signals))
        if cand.supporting_signals
        else "_no signals cited_"
    )

    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"{emoji} Incident recommendation — {cand.severity}"},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*{cand.proposed_external_title}*\n{cand.proposed_external_description}"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Affected components:*\n{components_text}"},
                {"type": "mrkdwn", "text": f"*Affected scope:*\n{cand.affected_scope}"},
                {"type": "mrkdwn", "text": f"*Confidence:*\n{cand.confidence:.2f}"},
                {
                    "type": "mrkdwn",
                    "text": f"*Duplicates existing incident:*\n{cand.duplicates_existing_incident or '_no_'}",
                },
            ],
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Supporting signals:*\n{signals}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Why I'm flagging this:*\n{cand.reasoning}"}},
        {"type": "divider"},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": "\n".join(TRIAGE_FOOTER_LINES)}]},
    ]
    return blocks, fallback


def build_watching_blocks(watching: list[WatchingItem]) -> tuple[list[dict[str, Any]], str]:
    bullets: list[str] = []
    for w in watching:
        line = f"• *{w.topic}* — {w.why_not_yet}"
        if w.signals_to_watch_for:
            line += "\n   _Looking for:_ " + "; ".join(w.signals_to_watch_for)
        bullets.append(line)
    body = "\n".join(bullets) if bullets else "_none_"
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "👀 Also watching but not flagging yet"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": body}},
    ]
    fallback = f"👀 Watching {len(watching)} item(s) — see thread for details."
    return blocks, fallback


def build_all_clear_blocks(
    sweep: SweepResult,
    run_metadata: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], str]:
    summary = sweep.summary or "Nothing to flag."
    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"✅ *Daily sweep complete — nothing to flag.*\n{summary}"}},
    ]
    if run_metadata:
        meta_bits = []
        run_id = run_metadata.get("github_run_id")
        run_url = run_metadata.get("artifact_url") or run_metadata.get("run_url")
        if run_id:
            meta_bits.append(f"Run ID: `{run_id}`")
        if run_url:
            meta_bits.append(f"<{run_url}|run details>")
        if meta_bits:
            blocks.append(
                {"type": "context", "elements": [{"type": "mrkdwn", "text": " · ".join(meta_bits)}]}
            )
    return blocks, f"✅ Daily sweep complete — nothing to flag. {summary}"


# ── Posting ────────────────────────────────────────────────────────────────

def _api_data(resp: Any) -> dict[str, Any]:
    if isinstance(resp, dict):
        return resp
    return getattr(resp, "data", None) or {}


def _post_one(
    client: Any,
    channel: str,
    blocks: list[dict[str, Any]],
    fallback: str,
    kind: str,
    meta: dict[str, Any] | None = None,
) -> PostedMessage:
    msg = PostedMessage(
        kind=kind,
        blocks=blocks,
        fallback_text=fallback,
        channel=channel,
        meta=meta or {},
    )
    try:
        resp = client.chat_postMessage(channel=channel, blocks=blocks, text=fallback)
    except Exception as exc:  # noqa: BLE001 — per-post failures must not kill the sweep
        LOG.warning("slack: chat.postMessage failed for %s: %s", kind, exc)
        msg.error = f"{type(exc).__name__}: {exc}"
        return msg

    data = _api_data(resp)
    msg.ts = data.get("ts")

    try:
        permalink_resp = client.chat_getPermalink(channel=channel, message_ts=msg.ts) if msg.ts else None
        if permalink_resp is not None:
            msg.permalink = _api_data(permalink_resp).get("permalink")
    except Exception as exc:  # noqa: BLE001 — permalink is decorative; tolerate failure
        LOG.warning("slack: chat.getPermalink failed for %s: %s", kind, exc)
    return msg


def post_sweep_result(
    sweep: SweepResult,
    channel: str | None = None,
    client: Any | None = None,
    run_metadata: dict[str, Any] | None = None,
) -> list[PostedMessage]:
    """Post each candidate as a top-level message, then a watching summary
    (if any), then the all-clear (if nothing fired).
    """
    channel = (channel or os.environ.get("POST_TO_CHANNEL_ID", "")).strip()
    if not channel:
        raise ValueError("post_sweep_result: no channel — set POST_TO_CHANNEL_ID or pass `channel=`")

    if client is None:
        token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
        if not token:
            raise ValueError("post_sweep_result: SLACK_BOT_TOKEN unset and no client passed in")
        from slack_sdk import WebClient
        client = WebClient(token=token)

    out: list[PostedMessage] = []

    # Quiet days: one all-clear post.
    if not sweep.candidates and not sweep.watching:
        blocks, fallback = build_all_clear_blocks(sweep, run_metadata)
        out.append(_post_one(client, channel, blocks, fallback, kind="all_clear",
                             meta={"summary": sweep.summary}))
        return out

    for cand in sweep.candidates:
        blocks, fallback = build_candidate_blocks(cand)
        out.append(_post_one(client, channel, blocks, fallback, kind="candidate",
                             meta={"title": cand.title, "severity": cand.severity}))

    if sweep.watching:
        blocks, fallback = build_watching_blocks(sweep.watching)
        out.append(_post_one(client, channel, blocks, fallback, kind="watching",
                             meta={"count": len(sweep.watching)}))

    return out
