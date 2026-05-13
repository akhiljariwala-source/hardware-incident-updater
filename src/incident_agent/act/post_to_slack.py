"""Block Kit message posting for incident proposals.

Threading model (per build-time decision): each candidate is a top-level post
so reactions are per-incident. The 'watching' summary and 'all clear' messages
are also top-level posts.

Stubbed at Step 1; implementation in Step 7.
"""

from __future__ import annotations


def post_sweep_result():
    raise NotImplementedError("post_sweep_result is implemented in Step 7.")
