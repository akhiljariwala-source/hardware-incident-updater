"""Tests for the Slack gatherer (Step 5)."""

from __future__ import annotations

from datetime import UTC, datetime

from incident_agent.gather import slack as slack_mod
from incident_agent.gather.slack import (
    ChannelConfig,
    UserResolver,
    fetch_channel_messages,
    gather_slack_messages,
)

FIXED_NOW = datetime(2026, 5, 13, 9, 0, tzinfo=UTC)


# ── Test doubles ─────────────────────────────────────────────────────────────

class _FakeSlackClient:
    """A minimal stand-in for slack_sdk.WebClient.

    Records calls and serves canned responses keyed off (method, channel).
    """

    def __init__(
        self,
        history_by_channel: dict[str, list[dict]] | None = None,
        users: dict[str, dict] | None = None,
        replies_by_parent: dict[str, list[dict]] | None = None,
        raise_history_for: set[str] | None = None,
    ):
        self.history_by_channel = history_by_channel or {}
        self.users = users or {}
        self.replies_by_parent = replies_by_parent or {}
        self.raise_history_for = raise_history_for or set()
        self.history_calls: list[dict] = []
        self.users_info_calls: list[str] = []
        self.replies_calls: list[dict] = []

    def conversations_history(self, *, channel, oldest, limit):
        self.history_calls.append({"channel": channel, "oldest": oldest, "limit": limit})
        if channel in self.raise_history_for:
            raise RuntimeError("missing_scope: channels:history")
        return {"messages": list(self.history_by_channel.get(channel, []))}

    def conversations_replies(self, *, channel, ts, limit):
        self.replies_calls.append({"channel": channel, "ts": ts, "limit": limit})
        return {"messages": list(self.replies_by_parent.get(ts, []))}

    def users_info(self, *, user):
        self.users_info_calls.append(user)
        return {"user": self.users.get(user, {"name": f"raw-{user}", "profile": {}})}


def _msg(ts: str, user: str = "U01AKHIL01", text: str = "hello", **extra) -> dict:
    base = {"ts": ts, "user": user, "text": text, "type": "message"}
    base.update(extra)
    return base


def _channel(name: str, **overrides) -> ChannelConfig:
    return ChannelConfig.from_dict({
        "name": name,
        "id": "C_" + name.upper().replace("-", "_"),
        "private": False,
        "pre_filter": False,
        "max_messages": 50,
        **overrides,
    })


def _users() -> dict[str, dict]:
    return {
        "U01AKHIL01": {"name": "akhil", "profile": {"display_name": "Akhil"}},
        "U01SAM0001":   {"name": "sam",   "profile": {"display_name": "Sam"}},
    }


# ── UserResolver ─────────────────────────────────────────────────────────────

def test_user_resolver_caches_lookups():
    client = _FakeSlackClient(users=_users())
    r = UserResolver(client)

    assert r.resolve("U01AKHIL01") == "Akhil"
    assert r.resolve("U01AKHIL01") == "Akhil"   # served from cache
    assert r.resolve("U01AKHIL01") == "Akhil"

    assert client.users_info_calls == ["U01AKHIL01"]


def test_user_resolver_falls_back_when_users_info_fails():
    class _BrokenClient(_FakeSlackClient):
        def users_info(self, *, user):
            raise RuntimeError("ratelimited")

    r = UserResolver(_BrokenClient())
    assert r.resolve("U_GHOST") == "(user:U_GHOST)"


def test_user_resolver_rewrites_mentions():
    client = _FakeSlackClient(users=_users())
    r = UserResolver(client)

    out = r.resolve_mentions("hey <@U01AKHIL01> ping <@U01SAM0001|sam> end")
    assert out == "hey @Akhil ping @Sam end"


# ── Pre-filter behaviour ─────────────────────────────────────────────────────

def test_pre_filter_keeps_keyword_matches():
    client = _FakeSlackClient(
        users=_users(),
        history_by_channel={"C_ALERTS": [
            _msg("1.0", text="all clear"),
            _msg("2.0", text="webhook latency elevated"),  # 'elevated' keyword
            _msg("3.0", text="random chatter"),
        ]},
    )
    cfg = _channel("alerts", id="C_ALERTS", pre_filter=True,
                   filter_keywords=["outage", "elevated", "down"])

    snap = fetch_channel_messages(client, cfg, now=FIXED_NOW)

    assert snap.error is None
    assert snap.pre_filter_applied is True
    assert snap.raw_message_count == 3
    assert [m.ts for m in snap.messages] == ["2.0"]


def test_pre_filter_keeps_triage_reactions_even_without_keyword_match():
    client = _FakeSlackClient(
        users=_users(),
        history_by_channel={"C_ALERTS": [
            _msg("1.0", text="weird thing happening",
                 reactions=[{"name": "rotating_light", "count": 2}]),
            _msg("2.0", text="nothing to see"),
        ]},
    )
    cfg = _channel("alerts", id="C_ALERTS", pre_filter=True, filter_keywords=["outage"])
    snap = fetch_channel_messages(client, cfg, now=FIXED_NOW)
    assert [m.ts for m in snap.messages] == ["1.0"]


def test_pre_filter_off_keeps_everything_and_reorders_chronologically():
    # Slack returns conversations.history newest-first; we reverse for prompt readability.
    client = _FakeSlackClient(
        users=_users(),
        history_by_channel={"C_PARTNER": [
            _msg("2.0", text="random update"),   # newest
            _msg("1.0", text="hi"),              # oldest
        ]},
    )
    cfg = _channel("partner", id="C_PARTNER", pre_filter=False)
    snap = fetch_channel_messages(client, cfg, now=FIXED_NOW)
    assert [m.ts for m in snap.messages] == ["1.0", "2.0"]


# ── Threading ────────────────────────────────────────────────────────────────

def test_thread_parent_pulls_replies():
    parent_ts = "100.0"
    client = _FakeSlackClient(
        users=_users(),
        history_by_channel={"C_X": [
            _msg(parent_ts, text="outage in progress",
                 thread_ts=parent_ts, reply_count=2,
                 reactions=[]),
        ]},
        replies_by_parent={parent_ts: [
            _msg(parent_ts, text="outage in progress", thread_ts=parent_ts, reply_count=2),
            _msg("100.1", text="affected: BMW", thread_ts=parent_ts),
            _msg("100.2", text="resolved", thread_ts=parent_ts),
        ]},
    )
    cfg = _channel("x", id="C_X", pre_filter=False)
    snap = fetch_channel_messages(client, cfg, now=FIXED_NOW)

    assert len(snap.messages) == 1
    parent = snap.messages[0]
    assert parent.is_thread_parent is True
    assert [r.ts for r in parent.replies] == ["100.1", "100.2"]
    assert client.replies_calls == [{"channel": "C_X", "ts": parent_ts, "limit": 11}]


def test_thread_replies_dont_run_when_message_filtered_out():
    """Filtered-out parents shouldn't trigger an expensive replies call."""
    parent_ts = "200.0"
    client = _FakeSlackClient(
        users=_users(),
        history_by_channel={"C_X": [
            _msg(parent_ts, text="nothing interesting",
                 thread_ts=parent_ts, reply_count=5),
        ]},
        replies_by_parent={parent_ts: [_msg(parent_ts, thread_ts=parent_ts)]},
    )
    cfg = _channel("x", id="C_X", pre_filter=True, filter_keywords=["outage"])

    snap = fetch_channel_messages(client, cfg, now=FIXED_NOW)
    assert snap.messages == []
    assert client.replies_calls == []


# ── Failure capture ──────────────────────────────────────────────────────────

def test_per_channel_failure_is_captured_not_raised():
    client = _FakeSlackClient(
        users=_users(),
        history_by_channel={"C_OK": [_msg("1.0", text="hello")]},
        raise_history_for={"C_BAD"},
    )
    good = _channel("good", id="C_OK")
    bad = _channel("bad", id="C_BAD")

    good_snap = fetch_channel_messages(client, good, now=FIXED_NOW)
    bad_snap = fetch_channel_messages(client, bad, now=FIXED_NOW)

    assert good_snap.error is None
    assert good_snap.messages
    assert bad_snap.error is not None
    assert "missing_scope" in bad_snap.error
    assert bad_snap.messages == []


# ── Message shape (users, mentions, reactions, bots) ────────────────────────

def test_message_resolves_author_and_mentions():
    client = _FakeSlackClient(
        users=_users(),
        history_by_channel={"C_X": [
            _msg("1.0", user="U01AKHIL01", text="ping <@U01SAM0001> see this"),
        ]},
    )
    cfg = _channel("x", id="C_X", pre_filter=False)
    snap = fetch_channel_messages(client, cfg, now=FIXED_NOW)

    m = snap.messages[0]
    assert m.author == "Akhil"
    assert m.author_id == "U01AKHIL01"
    assert "@Sam" in m.text


def test_bot_message_uses_bot_profile_name():
    raw = {
        "ts": "1.0",
        "bot_id": "B_MONITOR",
        "username": "PagerDuty",
        "text": "incident triggered",
        "type": "message",
        "subtype": "bot_message",
    }
    client = _FakeSlackClient(history_by_channel={"C_X": [raw]})
    cfg = _channel("x", id="C_X", pre_filter=False)
    snap = fetch_channel_messages(client, cfg, now=FIXED_NOW)

    m = snap.messages[0]
    assert m.is_bot is True
    assert m.author == "PagerDuty"


# ── gather_slack_messages env-driven entry point ─────────────────────────────

def test_gather_slack_messages_reports_missing_token(monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    snap = gather_slack_messages()
    assert snap.error == "SLACK_BOT_TOKEN unset"
    assert snap.channels == []


def test_gather_slack_messages_iterates_config(monkeypatch):
    """When called with an explicit client, the env-token check is bypassed."""
    # Build a fake client covering every channel in config/channels.yaml.
    cfgs = slack_mod._load_channel_configs()
    assert cfgs, "config/channels.yaml has no channels — did fixtures regress?"

    history = {c.id: [_msg(f"{i}.0", text="ping")] for i, c in enumerate(cfgs)}
    client = _FakeSlackClient(users=_users(), history_by_channel=history)

    snap = gather_slack_messages(client=client, now=FIXED_NOW)

    assert snap.error is None
    assert {c.channel_name for c in snap.channels} == {c.name for c in cfgs}
    # Pre-filtered channels with no matching keyword in our fake "ping" should drop to 0.
    pre_filter_channels = {c.name for c in cfgs if c.pre_filter}
    for ch in snap.channels:
        if ch.channel_name in pre_filter_channels:
            assert ch.messages == []
        else:
            assert ch.messages, f"{ch.channel_name}: expected at least one message"
