"""Reason-phase tests. Real coverage lands in Step 6 of the dev plan."""

from __future__ import annotations

import pytest

from incident_agent.reason import claude


def test_reason_is_stubbed():
    with pytest.raises(NotImplementedError):
        claude.reason_over_signals()
