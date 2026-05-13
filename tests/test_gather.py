"""Gather-phase tests. Real coverage lands in Step 5 (Slack)."""

from __future__ import annotations

import pytest

from incident_agent.gather import slack


@pytest.mark.parametrize(
    "fn",
    [
        slack.gather_slack_messages,
    ],
)
def test_gatherers_are_stubbed(fn):
    with pytest.raises(NotImplementedError):
        fn()
