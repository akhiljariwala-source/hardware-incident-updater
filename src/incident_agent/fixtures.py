"""Per-run fixture dump for audit + prompt-iteration replay.

Writes a directory under fixtures/{YYYY-MM-DD-HHMMSS}/ with:
- slack.json, status_pages.json, instatus.json, rubric.md
- prompt.txt, response.json, posted_messages.json, run_metadata.json

Stubbed at Step 1; implementation in Step 8.
"""

from __future__ import annotations


def write_run_fixtures():
    raise NotImplementedError("write_run_fixtures is implemented in Step 8.")
