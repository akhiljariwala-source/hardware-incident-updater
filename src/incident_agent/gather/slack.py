"""Slack history gatherer.

For each channel in config/channels.yaml, pulls the last 24h of messages,
resolves user IDs to display names (cached per run), optionally pre-filters
noisy channels by keyword/triage-reaction, and fetches a few replies for
thread parents so context isn't lost.

Per-channel failures (channel_not_found, missing_scope, etc.) are logged
and captured on the channel snapshot rather than raised — one bad channel
shouldn't kill the sweep.

Permalinks are NOT resolved here. We keep gather cheap and let the act
phase compute permalinks only for the small subset of messages Claude
actually cites in supporting_signals.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from incident_agent.config import CONFIG_DIR, REPO_ROOT

LOG = logging.getLogger(__name__)

LOOKBACK_HOURS = 24
THREAD_REPLY_LIMIT = 10
TRIAGE_REACTIONS = {"rotating_light", "warning", "fire", "siren"}
USER_MENTION_RE = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>")


# ── Pydantic shapes ──────────────────────────────────────────────────────────

class SlackReaction(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str
    count: int


class SlackMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")
    ts: str
    author: str              # display name (resolved); "(unknown user)" if it couldn't resolve
    author_id: str | None = None
    text: str
    reactions: list[SlackReaction] = Field(default_factory=list)
    thread_ts: str | None = None
    is_thread_parent: bool = False
    reply_count: int = 0
    is_bot: bool = False
    replies: list[SlackMessage] = Field(default_factory=list)


class SlackChannelData(BaseModel):
    channel_name: str
    channel_id: str
    private: bool
    pre_filter_applied: bool
    messages: list[SlackMessage]
    raw_message_count: int   # total messages returned by the API before pre-filter
    error: str | None = None


class SlackSnapshot(BaseModel):
    channels: list[SlackChannelData]
    error: str | None = None


# ── Config ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ChannelConfig:
    name: str
    id: str
    private: bool
    pre_filter: bool
    filter_keywords: tuple[str, ...]
    max_messages: int
    notes: str

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ChannelConfig:
        return cls(
            name=d["name"],
            id=d["id"],
            private=bool(d.get("private", False)),
            pre_filter=bool(d.get("pre_filter", False)),
            filter_keywords=tuple(d.get("filter_keywords", []) or []),
            max_messages=int(d.get("max_messages", 50)),
            notes=str(d.get("notes", "") or ""),
        )


def _load_channel_configs() -> list[ChannelConfig]:
    cfg_path = CONFIG_DIR / "channels.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    raw = (cfg.get("slack", {}) or {}).get("channels", []) or []
    out: list[ChannelConfig] = []
    for entry in raw:
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        try:
            out.append(ChannelConfig.from_dict(entry))
        except (KeyError, TypeError, ValueError) as exc:
            LOG.warning("slack: skipping malformed channel entry %r: %s", entry, exc)
    return out


# ── User-name cache ──────────────────────────────────────────────────────────

class UserResolver:
    """Per-run cache of Slack user_id -> display name."""

    def __init__(self, client: Any):
        self._client = client
        self._cache: dict[str, str] = {}

    def resolve(self, user_id: str | None) -> str:
        if not user_id:
            return "(unknown user)"
        if user_id in self._cache:
            return self._cache[user_id]
        name = self._fetch(user_id)
        self._cache[user_id] = name
        return name

    def _fetch(self, user_id: str) -> str:
        try:
            resp = self._client.users_info(user=user_id)
        except Exception as exc:  # noqa: BLE001 — we degrade rather than fail
            LOG.warning("slack: users_info failed for %s: %s", user_id, exc)
            return f"(user:{user_id})"
        user = (resp.get("user") or {}) if isinstance(resp, dict) else (resp.data or {}).get("user", {})
        profile = user.get("profile", {}) or {}
        return (
            profile.get("display_name")
            or profile.get("real_name")
            or user.get("name")
            or f"(user:{user_id})"
        )

    def resolve_mentions(self, text: str) -> str:
        """Rewrite <@Uxxxx> mentions inline with display names for prompt readability."""
        def _sub(match: re.Match[str]) -> str:
            return f"@{self.resolve(match.group(1))}"
        return USER_MENTION_RE.sub(_sub, text)


# ── Core fetch ───────────────────────────────────────────────────────────────

def _oldest_ts(hours: int, now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    return f"{(now - timedelta(hours=hours)).timestamp():.6f}"


def _has_triage_reaction(reactions: list[dict[str, Any]]) -> bool:
    return any((r.get("name") or "").lower() in TRIAGE_REACTIONS for r in reactions)


def _matches_keywords(text: str, keywords: tuple[str, ...]) -> bool:
    if not keywords:
        return False
    lower = text.lower()
    return any(kw.lower() in lower for kw in keywords)


def _api_data(resp: Any) -> dict[str, Any]:
    """slack_sdk responses are SlackResponse objects; tests use plain dicts."""
    if isinstance(resp, dict):
        return resp
    return getattr(resp, "data", None) or {}


def _build_message(
    raw: dict[str, Any],
    resolver: UserResolver,
) -> SlackMessage:
    author_id = raw.get("user") or raw.get("bot_id")
    author = resolver.resolve(raw.get("user"))
    if not raw.get("user") and raw.get("bot_id"):
        # Bot messages: prefer the bot username embedded in the payload.
        author = raw.get("username") or raw.get("bot_profile", {}).get("name") or f"(bot:{author_id})"

    text = resolver.resolve_mentions(raw.get("text") or "")
    reactions_raw = raw.get("reactions", []) or []
    reactions = [
        SlackReaction(name=r.get("name", ""), count=int(r.get("count", 0)))
        for r in reactions_raw
        if r.get("name")
    ]

    ts = raw.get("ts", "")
    thread_ts = raw.get("thread_ts")
    is_thread_parent = bool(thread_ts) and thread_ts == ts and int(raw.get("reply_count") or 0) > 0

    return SlackMessage(
        ts=ts,
        author=author,
        author_id=author_id,
        text=text,
        reactions=reactions,
        thread_ts=thread_ts,
        is_thread_parent=is_thread_parent,
        reply_count=int(raw.get("reply_count") or 0),
        is_bot=bool(raw.get("bot_id")),
    )


def _fetch_thread_replies(
    client: Any,
    channel_id: str,
    parent_ts: str,
    resolver: UserResolver,
    limit: int = THREAD_REPLY_LIMIT,
) -> list[SlackMessage]:
    try:
        resp = client.conversations_replies(channel=channel_id, ts=parent_ts, limit=limit + 1)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("slack: conversations_replies failed for %s/%s: %s",
                    channel_id, parent_ts, exc)
        return []
    data = _api_data(resp)
    raws = data.get("messages") or []
    # First entry is the parent; replies follow.
    replies: list[SlackMessage] = []
    for raw in raws[1:]:
        replies.append(_build_message(raw, resolver))
        if len(replies) >= limit:
            break
    return replies


def _should_keep(msg: SlackMessage, channel: ChannelConfig) -> bool:
    """Pre-filter logic. Always keep when pre_filter is off."""
    if not channel.pre_filter:
        return True
    if _matches_keywords(msg.text, channel.filter_keywords):
        return True
    if _has_triage_reaction([r.model_dump() for r in msg.reactions]):
        return True
    return False


def fetch_channel_messages(
    client: Any,
    channel: ChannelConfig,
    now: datetime | None = None,
    resolver: UserResolver | None = None,
) -> SlackChannelData:
    """Fetch + filter one channel. Never raises."""
    resolver = resolver or UserResolver(client)
    oldest = _oldest_ts(LOOKBACK_HOURS, now=now)

    try:
        resp = client.conversations_history(
            channel=channel.id,
            oldest=oldest,
            limit=min(channel.max_messages, 200),
        )
    except Exception as exc:  # noqa: BLE001 — capture per-channel
        LOG.warning("slack: conversations_history failed for %s (%s): %s",
                    channel.name, channel.id, exc)
        return SlackChannelData(
            channel_name=channel.name,
            channel_id=channel.id,
            private=channel.private,
            pre_filter_applied=channel.pre_filter,
            messages=[],
            raw_message_count=0,
            error=f"{type(exc).__name__}: {exc}",
        )

    data = _api_data(resp)
    raws: list[dict[str, Any]] = data.get("messages") or []

    # Slack returns newest first; reverse for chronological reading order.
    raws = list(reversed(raws[: channel.max_messages]))
    raw_count = len(raws)

    kept: list[SlackMessage] = []
    for raw in raws:
        msg = _build_message(raw, resolver)
        if not _should_keep(msg, channel):
            continue
        if msg.is_thread_parent:
            msg = msg.model_copy(update={
                "replies": _fetch_thread_replies(client, channel.id, msg.ts, resolver),
            })
        kept.append(msg)

    return SlackChannelData(
        channel_name=channel.name,
        channel_id=channel.id,
        private=channel.private,
        pre_filter_applied=channel.pre_filter,
        messages=kept,
        raw_message_count=raw_count,
    )


def gather_slack_messages(
    client: Any | None = None,
    now: datetime | None = None,
) -> SlackSnapshot:
    """Top-level entry. Reads config + token from env and fetches every channel."""
    if client is None:
        token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
        if not token:
            return SlackSnapshot(channels=[], error="SLACK_BOT_TOKEN unset")
        try:
            from slack_sdk import WebClient
        except ImportError as exc:
            return SlackSnapshot(channels=[], error=f"slack_sdk import failed: {exc}")
        client = WebClient(token=token)

    channels_cfg = _load_channel_configs()
    if not channels_cfg:
        return SlackSnapshot(channels=[], error="no channels configured in config/channels.yaml")

    resolver = UserResolver(client)
    out: list[SlackChannelData] = []
    for cfg in channels_cfg:
        out.append(fetch_channel_messages(client, cfg, now=now, resolver=resolver))
        # Cheap politeness — Slack rate-limits at ~1 req/s on tier-2 methods.
        time.sleep(0.05)
    return SlackSnapshot(channels=out)


# ── Local smoke / fixture-capture CLI ────────────────────────────────────────

def _cli() -> int:
    parser = argparse.ArgumentParser(description="Print Slack gather output.")
    parser.add_argument(
        "--save-fixtures",
        action="store_true",
        help="Also save the JSON snapshot under tests/fixtures/slack/.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    snapshot = gather_slack_messages()
    serialized = snapshot.model_dump()
    print(json.dumps(serialized, indent=2, default=str))

    print(
        "\n-- summary --\n"
        + "\n".join(
            f"  {c.channel_name:35} {len(c.messages):3} kept / {c.raw_message_count:3} raw"
            + ("  [filter on]" if c.pre_filter_applied else "")
            + (f"  ERROR: {c.error}" if c.error else "")
            for c in snapshot.channels
        ),
        file=sys.stderr,
    )

    if args.save_fixtures:
        out_dir: Path = REPO_ROOT / "tests" / "fixtures" / "slack"
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H%M%SZ")
        (out_dir / f"snapshot_{stamp}.json").write_text(
            json.dumps(serialized, indent=2, default=str), encoding="utf-8"
        )
        print(f"saved fixture to tests/fixtures/slack/snapshot_{stamp}.json", file=sys.stderr)

    return 0 if snapshot.error is None else 1


if __name__ == "__main__":
    raise SystemExit(_cli())
