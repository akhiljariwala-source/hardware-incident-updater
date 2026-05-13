"""Tests for the reasoning step (Step 6).

Verifies prompt rendering against all four gather snapshots and exercises the
Anthropic call with a mock client. No real API calls.
"""

from __future__ import annotations

from incident_agent.gather.instatus import (
    InStatusComponent,
    InStatusIncident,
    InStatusIncidentUpdate,
    InStatusSnapshot,
)
from incident_agent.gather.rubric import RubricResult
from incident_agent.gather.slack import (
    SlackChannelData,
    SlackMessage,
    SlackReaction,
    SlackSnapshot,
)
from incident_agent.gather.status_pages import (
    IncidentUpdate,
    StatusPageIncident,
    StatusPageSnapshot,
)
from incident_agent.reason.claude import (
    MAX_TOKENS,
    MODEL,
    ReasoningInputs,
    _render_slack,
    _render_status_page,
    reason_over_signals,
    render_user_prompt,
)
from incident_agent.reason.schema import IncidentCandidate, Signal, SweepResult

_MISSING = object()


def _make_inputs(**overrides) -> ReasoningInputs:
    instatus = overrides.pop("instatus", _MISSING)
    if instatus is _MISSING:
        instatus = InStatusSnapshot(
            page_id="page",
            base_url="https://api.instatus.com",
            active_incidents=[],
            components=[
                InStatusComponent(id="c1", name="BMW", status="OPERATIONAL"),
                InStatusComponent(id="c2", name="Tesla", status="OPERATIONAL"),
            ],
        )
    status_pages = overrides.pop("status_pages", _MISSING)
    if status_pages is _MISSING:
        status_pages = {
            "smartcar": StatusPageSnapshot(
                source="smartcar",
                base_url="https://status.smartcar.com",
                current_status="All Systems Operational",
                current_indicator="none",
                recent_incidents=[],
            ),
            "enode": StatusPageSnapshot(
                source="enode",
                base_url="https://status.enode.com",
                current_status="All Systems Operational",
                current_indicator="none",
                recent_incidents=[],
            ),
        }
    slack = overrides.pop("slack", _MISSING)
    if slack is _MISSING:
        slack = SlackSnapshot(channels=[])
    rubric = overrides.pop("rubric", _MISSING)
    if rubric is _MISSING:
        rubric = RubricResult(
            markdown="# Rubric\n- Flag major outages.\n",
            source="file",
            page_id=None,
        )
    today = overrides.pop("today_iso", "2026-05-13")
    return ReasoningInputs(
        rubric=rubric,
        instatus=instatus,
        status_pages=status_pages,
        slack=slack,
        today_iso=today,
    )


# ── Sub-renderer tests ──────────────────────────────────────────────────────

def test_render_status_page_handles_error_snapshot():
    snap = StatusPageSnapshot(
        source="smartcar",
        base_url="https://status.smartcar.com",
        current_status="unknown",
        current_indicator="unknown",
        recent_incidents=[],
        error="HTTPStatusError: 503",
    )
    current, incidents = _render_status_page(snap)
    assert "HTTPStatusError" in current
    assert "fetch failed" in incidents


def test_render_status_page_lists_recent_incidents_and_updates():
    snap = StatusPageSnapshot(
        source="enode",
        base_url="https://status.enode.com",
        current_status="All Systems Operational",
        current_indicator="none",
        recent_incidents=[
            StatusPageIncident(
                id="i1",
                name="Mercedes: Elevated error rate",
                status="resolved",
                impact="minor",
                created_at="2026-05-12T18:34:12Z",
                updated_at="2026-05-13T09:02:28Z",
                resolved_at="2026-05-13T09:02:28Z",
                incident_updates=[
                    IncidentUpdate(
                        body="Issue resolved.",
                        status="resolved",
                        created_at="2026-05-13T09:02:28Z",
                    ),
                ],
            ),
        ],
    )
    current, incidents = _render_status_page(snap)
    assert "none — All Systems Operational" == current
    assert "Mercedes" in incidents
    assert "Issue resolved." in incidents


def test_render_slack_groups_threads_and_handles_errors():
    snap = SlackSnapshot(channels=[
        SlackChannelData(
            channel_name="hardware-connectivity-alerts",
            channel_id="C1",
            private=False,
            pre_filter_applied=True,
            raw_message_count=2,
            messages=[
                SlackMessage(
                    ts="1.0",
                    author="Akhil",
                    author_id="U01AKHIL01",
                    text="outage in progress",
                    reactions=[SlackReaction(name="rotating_light", count=2)],
                    thread_ts="1.0",
                    is_thread_parent=True,
                    reply_count=1,
                    replies=[
                        SlackMessage(
                            ts="1.1",
                            author="Sam",
                            text="affecting BMW only",
                            thread_ts="1.0",
                        ),
                    ],
                ),
            ],
        ),
        SlackChannelData(
            channel_name="customer-success",
            channel_id="C2",
            private=False,
            pre_filter_applied=False,
            raw_message_count=0,
            messages=[],
            error="not_in_channel",
        ),
    ])

    out = _render_slack(snap)
    assert "#hardware-connectivity-alerts" in out
    assert "outage in progress" in out
    assert ":rotating_light:×2" in out
    assert "↳ **Sam**" in out
    assert "affecting BMW only" in out
    assert "not_in_channel" in out


# ── Full prompt assembly ────────────────────────────────────────────────────

def test_render_user_prompt_interpolates_all_placeholders():
    inputs = _make_inputs()
    out = render_user_prompt(inputs)

    # Every placeholder must be replaced — none of the literal `{var}` should remain.
    for placeholder in (
        "{rubric_markdown}",
        "{active_incidents_json}",
        "{components_list}",
        "{smartcar_status}",
        "{smartcar_incidents}",
        "{enode_status}",
        "{enode_incidents}",
        "{slack_messages_by_channel}",
        "{today_iso}",
    ):
        assert placeholder not in out, f"unfilled placeholder: {placeholder}"

    assert "Flag major outages." in out               # rubric got in
    assert "BMW" in out and "Tesla" in out            # components got in
    assert "All Systems Operational" in out           # statuspage status got in
    assert "2026-05-13" in out                        # today_iso got in


def test_render_user_prompt_tolerates_curly_braces_in_dynamic_content():
    """str.format() would crash on JSON-like content; str.replace() must not."""
    instatus = InStatusSnapshot(
        page_id="page",
        base_url="https://api.instatus.com",
        active_incidents=[
            InStatusIncident(
                id="open_1",
                name="Active outage",
                status="INVESTIGATING",
                updates=[
                    InStatusIncidentUpdate(
                        message="error rate {spike} observed",  # contains literal `{`
                        status="INVESTIGATING",
                    ),
                ],
            ),
        ],
        components=[],
    )
    out = render_user_prompt(_make_inputs(instatus=instatus))
    assert "Active outage" in out
    assert "{spike}" in out  # passed through literally


def test_render_user_prompt_handles_missing_status_source():
    inputs = _make_inputs(status_pages={})
    out = render_user_prompt(inputs)
    assert "no snapshot available" in out


# ── Anthropic call ──────────────────────────────────────────────────────────

class _FakeMessagesParseResponse:
    def __init__(self, parsed: SweepResult):
        self.parsed_output = parsed


class _FakeMessages:
    def __init__(self, parsed: SweepResult):
        self._parsed = parsed
        self.last_kwargs: dict | None = None

    def parse(self, **kwargs):
        self.last_kwargs = kwargs
        return _FakeMessagesParseResponse(self._parsed)


class _FakeAnthropicClient:
    def __init__(self, parsed: SweepResult):
        self.messages = _FakeMessages(parsed)


def test_reason_over_signals_sends_expected_params_and_returns_parsed():
    canned = SweepResult(
        candidates=[
            IncidentCandidate(
                title="Test candidate",
                severity="investigating",
                affected_components=["BMW"],
                affected_scope="BMW vehicles via Smartcar",
                proposed_external_title="BMW connectivity degradation",
                proposed_external_description="We are investigating elevated error rates affecting BMW.",
                supporting_signals=[
                    Signal(source="#alerts", excerpt="error rate up", timestamp="1.0"),
                ],
                confidence=0.85,
                reasoning="Rubric: provider degradation publicly posted.",
            )
        ],
        watching=[],
        summary="One candidate flagged.",
    )
    client = _FakeAnthropicClient(canned)
    inputs = _make_inputs()

    result, system_prompt, user_prompt = reason_over_signals(inputs, client=client)

    assert result.summary == "One candidate flagged."
    assert result.candidates[0].title == "Test candidate"

    sent = client.messages.last_kwargs
    assert sent["model"] == MODEL
    assert sent["max_tokens"] == MAX_TOKENS
    assert sent["thinking"] == {"type": "adaptive"}
    assert sent["output_config"] == {"effort": "high"}
    assert sent["output_format"] is SweepResult
    # No sampling params (would 400 on Opus 4.7).
    assert "temperature" not in sent
    assert "top_p" not in sent
    assert "top_k" not in sent

    # Prompts were threaded through unchanged.
    assert "integration incident analyst" in system_prompt
    assert sent["system"] == system_prompt
    assert sent["messages"][0]["role"] == "user"
    assert sent["messages"][0]["content"] == user_prompt


def test_reason_over_signals_accepts_dict_response_for_robustness():
    """If the SDK changes the response shape, we should still cope."""
    canned = SweepResult(candidates=[], watching=[], summary="Nothing to flag.")

    class _DictResponseMessages:
        def parse(self, **kwargs):
            class R:
                parsed_output = canned.model_dump()  # dict, not SweepResult
            return R()

    class _DictClient:
        messages = _DictResponseMessages()

    result, _, _ = reason_over_signals(_make_inputs(), client=_DictClient())
    assert isinstance(result, SweepResult)
    assert result.summary == "Nothing to flag."
