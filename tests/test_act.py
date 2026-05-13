"""Act-phase tests. Real coverage lands in Step 7 of the dev plan."""

from __future__ import annotations

import pytest

from incident_agent.act import post_to_slack


def test_post_is_stubbed():
    with pytest.raises(NotImplementedError):
        post_to_slack.post_sweep_result()
