"""Pydantic models for Claude's structured tool output.

Shape matches the dev plan. The fields here flow into Claude via
`messages.parse(output_format=SweepResult)` — every name and every constraint
here ends up in the JSON schema Claude sees, so docstrings double as prompt
instructions.

Notes on schema choices:
- `Literal` translates to JSON Schema `enum`. Constrains severity to the
  three values that map to InStatus's open-incident states.
- No numeric `Field(ge=0, le=1)` on `confidence` because the parser doesn't
  support numeric constraints; ranges are described in the docstring instead.
- `permalink` is intentionally optional. Step 5 captures channel_id + ts but
  not permalinks; the act phase resolves them just-in-time for cited messages.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Signal(BaseModel):
    """A single piece of evidence cited by an incident recommendation."""

    model_config = ConfigDict(extra="ignore")

    source: str = Field(
        ..., description="Where this came from, e.g. '#hardware-connectivity-alerts' or 'enode status page'."
    )
    excerpt: str = Field(
        ..., description="Verbatim snippet up to ~200 chars. Quote the actual text — don't summarize."
    )
    permalink: str | None = Field(
        default=None,
        description="Optional URL. Omit for Slack messages — the act phase resolves permalinks from channel_id+ts.",
    )
    timestamp: str = Field(
        ..., description="ISO-8601 timestamp of the signal, or the Slack ts (e.g. '1778698064.511749')."
    )


class IncidentCandidate(BaseModel):
    """An incident the bot recommends opening on the public InStatus page."""

    model_config = ConfigDict(extra="ignore")

    title: str = Field(..., description="Short internal title (≤80 chars). Not user-facing.")
    severity: Literal["investigating", "identified", "major_outage"] = Field(
        ...,
        description=(
            "Maps to the InStatus open-incident states. "
            "Use the rubric — don't pick severity from prose alone."
        ),
    )
    affected_components: list[str] = Field(
        ...,
        description=(
            "Pick from the closed list of available InStatus components in the prompt. "
            "Do NOT invent new component names. If the outage is at a provider layer "
            "(Smartcar/Enode), translate it to the affected brand/category components."
        ),
    )
    affected_scope: str = Field(
        ...,
        description=(
            "One sentence describing who's affected, e.g. 'Honda/Acura via Enode, ~5% of fleet'. "
            "Avoid percentages unless they appear in the source signals."
        ),
    )
    proposed_external_title: str = Field(
        ..., description="The title users will see on the InStatus page. Factual, brief, no jargon, no blame."
    )
    proposed_external_description: str = Field(
        ...,
        description=(
            "First incident update message users will see. Match the tone of existing "
            "ev.energy InStatus incidents: factual, brief, no internal jargon, no "
            "provider names unless they've publicly acknowledged the issue."
        ),
    )
    supporting_signals: list[Signal] = Field(
        ...,
        description=(
            "Specific evidence. A recommendation without cited signals is not useful — "
            "include at least one Signal per candidate."
        ),
    )
    confidence: float = Field(
        ...,
        description=(
            "0.0 to 1.0. Use 0.9+ only when the signal is unambiguous AND the rubric "
            "clearly applies. When in doubt, lower the confidence or move it to `watching`."
        ),
    )
    duplicates_existing_incident: str | None = Field(
        default=None,
        description=(
            "If this matches an existing active InStatus incident, set to that incident's id. "
            "Otherwise null."
        ),
    )
    reasoning: str = Field(
        ..., description="Why you think this is incident-worthy. Cite the specific rubric clause that applies."
    )


class WatchingItem(BaseModel):
    """Something worth tracking but not yet incident-worthy."""

    model_config = ConfigDict(extra="ignore")

    topic: str = Field(..., description="What you're watching, e.g. 'Tesla charging session drop-offs'.")
    why_not_yet: str = Field(
        ...,
        description="Why this doesn't yet rise to an incident. Cite the rubric ('Do NOT flag' clauses).",
    )
    signals_to_watch_for: list[str] = Field(
        ...,
        description="Concrete signals that, if seen tomorrow, would escalate this to a candidate.",
    )


class SweepResult(BaseModel):
    """The top-level structured response from a single reasoning run."""

    model_config = ConfigDict(extra="ignore")

    candidates: list[IncidentCandidate] = Field(
        default_factory=list,
        description="Incidents you recommend opening today. Empty list is fine — most days will be empty.",
    )
    watching: list[WatchingItem] = Field(
        default_factory=list,
        description="Things to track but not flag yet.",
    )
    summary: str = Field(
        ...,
        description=(
            "One or two sentence overall summary for the daily Slack post. "
            "Plain English. Suitable for someone reading on their phone over coffee."
        ),
    )
