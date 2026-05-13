"""Cross-gatherer smoke tests.

All four gatherers (status_pages, instatus, rubric, slack) are now
implemented; their detailed coverage lives in dedicated test_*.py files.
"""

from __future__ import annotations


def test_gatherer_modules_importable():
    from incident_agent.gather import instatus, rubric, slack, status_pages  # noqa: F401
