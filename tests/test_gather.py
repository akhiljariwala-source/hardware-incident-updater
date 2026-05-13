"""Gather-phase tests. Real coverage lands in Steps 2-5 of the dev plan."""

from __future__ import annotations

import pytest

from incident_agent.gather import rubric, slack


@pytest.mark.parametrize(
    "fn",
    [
        slack.gather_slack_messages,
        rubric.gather_rubric,
    ],
)
def test_gatherers_are_stubbed(fn):
    with pytest.raises(NotImplementedError):
        fn()
