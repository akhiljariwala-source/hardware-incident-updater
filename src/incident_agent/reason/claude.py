"""Single Claude call with adaptive thinking + structured output.

Takes the four gather snapshots, renders them into the user prompt, and asks
Claude Opus 4.7 to produce a SweepResult. Uses `messages.parse(output_format=...)`
so the schema constraints in `reason/schema.py` are enforced by the API.

Model + parameters per `claude-api` skill:
- claude-opus-4-7 (no model alternatives)
- thinking: adaptive (Claude decides depth)
- effort: high (intelligence-sensitive but not coding/agentic)
- no temperature/top_p/top_k (400s on 4.7)
- max_tokens: 16k (well under the streaming threshold)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
from dataclasses import dataclass
from typing import Any

from incident_agent.config import PROMPTS_DIR
from incident_agent.gather.instatus import InStatusSnapshot
from incident_agent.gather.rubric import RubricResult
from incident_agent.gather.slack import SlackSnapshot
from incident_agent.gather.status_pages import StatusPageSnapshot
from incident_agent.reason.schema import SweepResult

LOG = logging.getLogger(__name__)

MODEL = "claude-opus-4-7"
MAX_TOKENS = 16_000


# ── Prompt rendering ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ReasoningInputs:
    """Everything the reasoning step needs to render its prompt."""

    rubric: RubricResult
    instatus: InStatusSnapshot
    status_pages: dict[str, StatusPageSnapshot]
    slack: SlackSnapshot
    today_iso: str = ""


def _load_prompts() -> tuple[str, str]:
    system = (PROMPTS_DIR / "system.md").read_text(encoding="utf-8")
    user_template = (PROMPTS_DIR / "user_template.md").read_text(encoding="utf-8")
    return system, user_template


def _render_status_page(snapshot: StatusPageSnapshot | None) -> tuple[str, str]:
    """Returns (current_status_line, incidents_block)."""
    if snapshot is None:
        return "(no snapshot available)", "(no snapshot available)"
    if snapshot.error:
        return f"(unknown — {snapshot.error})", "(unknown — fetch failed)"

    current = f"{snapshot.current_indicator} — {snapshot.current_status}"

    if not snapshot.recent_incidents:
        return current, "(no incidents in the last 7 days)"

    lines = []
    for inc in snapshot.recent_incidents:
        head = f"- [{inc.status}/{inc.impact}] {inc.name} (started {inc.created_at}, updated {inc.updated_at})"
        lines.append(head)
        for upd in inc.incident_updates[:3]:
            body = (upd.body or "").strip().replace("\n", " ")
            if body:
                lines.append(f"  · ({upd.status} {upd.created_at}) {body[:300]}")
            else:
                lines.append(f"  · ({upd.status} {upd.created_at})")
    return current, "\n".join(lines)


def _render_instatus(snapshot: InStatusSnapshot) -> tuple[str, str]:
    """Returns (active_incidents_json, components_list_md)."""
    if snapshot.error:
        return f"(unknown — {snapshot.error})", "(unknown — fetch failed)"

    incidents = [
        {
            "id": inc.id,
            "name": inc.name,
            "status": inc.status,
            "started": inc.started,
            "affected_components": [c.name for c in inc.components],
            "last_update": (inc.updates[0].message if inc.updates else None),
        }
        for inc in snapshot.active_incidents
    ]
    active_json = json.dumps(incidents, indent=2) if incidents else "(no active incidents on the InStatus page)"

    if not snapshot.components:
        components_md = "(no components fetched)"
    else:
        components_md = "\n".join(f"- {c.name} (id: {c.id}, status: {c.status})" for c in snapshot.components)

    return active_json, components_md


def _render_slack(snapshot: SlackSnapshot) -> str:
    """Render the Slack snapshot as a Claude-readable block."""
    if snapshot.error:
        return f"(slack unavailable: {snapshot.error})"
    if not snapshot.channels:
        return "(no channels configured)"

    sections = []
    for ch in snapshot.channels:
        header = f"### #{ch.channel_name} ({ch.channel_id})"
        if ch.error:
            sections.append(f"{header}\n_error: {ch.error}_\n")
            continue
        if not ch.messages:
            note = (
                " — pre-filter dropped all messages"
                if ch.pre_filter_applied and ch.raw_message_count > 0
                else ""
            )
            sections.append(f"{header}\n_no messages in the last 24h{note}_\n")
            continue

        body_lines = [
            f"_{len(ch.messages)} of {ch.raw_message_count} message(s) after filtering_"
            if ch.pre_filter_applied
            else f"_{len(ch.messages)} message(s) in the last 24h_",
        ]
        for msg in ch.messages:
            reactions = (
                " " + " ".join(f":{r.name}:×{r.count}" for r in msg.reactions)
                if msg.reactions
                else ""
            )
            tag = " [bot]" if msg.is_bot else ""
            body_lines.append(
                f"\n**{msg.author}**{tag} (ts: {msg.ts}){reactions}\n{msg.text.strip()}"
            )
            for reply in msg.replies:
                rrx = (
                    " " + " ".join(f":{r.name}:×{r.count}" for r in reply.reactions)
                    if reply.reactions
                    else ""
                )
                rtag = " [bot]" if reply.is_bot else ""
                body_lines.append(
                    f"  ↳ **{reply.author}**{rtag} (ts: {reply.ts}){rrx}\n  {reply.text.strip()}"
                )
        sections.append(f"{header}\n" + "\n".join(body_lines) + "\n")

    return "\n".join(sections)


def render_user_prompt(inputs: ReasoningInputs, template: str | None = None) -> str:
    """Render the user prompt by interpolating the template with gathered data.

    Uses str.replace() rather than str.format() because dynamic content
    (Slack messages, JSON dumps) contains literal `{` and `}` characters that
    str.format() would treat as malformed placeholders.
    """
    if template is None:
        _, template = _load_prompts()

    smartcar = inputs.status_pages.get("smartcar")
    enode = inputs.status_pages.get("enode")
    smartcar_status, smartcar_incidents = _render_status_page(smartcar)
    enode_status, enode_incidents = _render_status_page(enode)
    active_incidents_json, components_list = _render_instatus(inputs.instatus)

    replacements = {
        "{rubric_markdown}": inputs.rubric.markdown,
        "{active_incidents_json}": active_incidents_json,
        "{components_list}": components_list,
        "{smartcar_status}": smartcar_status,
        "{smartcar_incidents}": smartcar_incidents,
        "{enode_status}": enode_status,
        "{enode_incidents}": enode_incidents,
        "{slack_messages_by_channel}": _render_slack(inputs.slack),
        "{today_iso}": inputs.today_iso or dt.date.today().isoformat(),
    }

    out = template
    for placeholder, value in replacements.items():
        out = out.replace(placeholder, value)
    return out


# ── Claude call ──────────────────────────────────────────────────────────────

def reason_over_signals(
    inputs: ReasoningInputs,
    client: Any | None = None,
) -> tuple[SweepResult, str, str]:
    """Run one reasoning pass.

    Returns (sweep_result, system_prompt, user_prompt) so the fixtures dump
    can save exactly what was sent to the API alongside the response.
    """
    system_prompt, user_template = _load_prompts()
    user_prompt = render_user_prompt(inputs, template=user_template)

    if client is None:
        import anthropic
        client = anthropic.Anthropic()

    response = client.messages.parse(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        thinking={"type": "adaptive"},
        output_config={"effort": "high"},
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
        output_format=SweepResult,
    )

    parsed = getattr(response, "parsed_output", None) or getattr(response, "parsed", None)
    if parsed is None:
        raise RuntimeError(
            "messages.parse() returned no parsed output. "
            "Check that the SDK version supports output_format and that the "
            "model returned text matching the schema."
        )
    if not isinstance(parsed, SweepResult):
        parsed = SweepResult.model_validate(parsed)
    return parsed, system_prompt, user_prompt


# ── Local replay CLI ────────────────────────────────────────────────────────

def _cli() -> int:
    parser = argparse.ArgumentParser(
        description="Run the reasoning step against gathered fixtures. Replay-friendly."
    )
    parser.add_argument(
        "--fixture-dir",
        required=True,
        help=(
            "Directory containing slack.json, status_pages.json, instatus.json, rubric.md. "
            "Either a recent live capture (fixtures/<run>/) or a tests/fixtures/ replay."
        ),
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Write the SweepResult JSON to this path instead of stdout.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    inputs = _load_inputs_from_dir(args.fixture_dir)
    result, _system, _user = reason_over_signals(inputs)

    payload = json.dumps(result.model_dump(), indent=2, default=str)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(payload)
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        print(payload)
    return 0


def _load_inputs_from_dir(path: str) -> ReasoningInputs:
    import pathlib
    p = pathlib.Path(path)
    if not p.is_dir():
        raise SystemExit(f"not a directory: {p}")

    def _read(name: str) -> dict:
        f = p / name
        if not f.is_file():
            return {}
        return json.loads(f.read_text(encoding="utf-8"))

    rubric_md = (p / "rubric.md").read_text(encoding="utf-8") if (p / "rubric.md").is_file() else ""
    rubric = RubricResult(markdown=rubric_md, source="file", page_id=None)

    instatus_raw = _read("instatus.json")
    instatus = InStatusSnapshot.model_validate(instatus_raw) if instatus_raw else InStatusSnapshot(
        page_id="(missing)", base_url="https://api.instatus.com",
        active_incidents=[], components=[],
    )

    status_pages_raw = _read("status_pages.json")
    status_pages: dict[str, StatusPageSnapshot] = {}
    for name, snap in status_pages_raw.items():
        status_pages[name] = StatusPageSnapshot.model_validate(snap)

    slack_raw = _read("slack.json")
    slack = SlackSnapshot.model_validate(slack_raw) if slack_raw else SlackSnapshot(channels=[])

    return ReasoningInputs(
        rubric=rubric,
        instatus=instatus,
        status_pages=status_pages,
        slack=slack,
        today_iso=os.environ.get("INCIDENT_AGENT_TODAY") or dt.date.today().isoformat(),
    )


if __name__ == "__main__":
    raise SystemExit(_cli())
