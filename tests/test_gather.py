"""Gather-phase tests. Real coverage lands in Steps 2-5 of the dev plan."""

from __future__ import annotations

import pytest

from incident_agent.gather import instatus, rubric, slack, status_pages


@pytest.mark.parametrize(
    "fn",
    [
        slack.gather_slack_messages,
        status_pages.gather_status_pages,
        instatus.gather_instatus_state,
        rubric.gather_rubric,
    ],
)
def test_gatherers_are_stubbed(fn):
    with pytest.raises(NotImplementedError):
        fn()
