"""Claude helper: runs the headless Claude Code CLI (`claude -p`) with JSON output.

Claude is reached through the local `claude` login (a Claude subscription), so no
API key is used. `ANTHROPIC_API_KEY` is removed from the CLI's environment so a key
left in the shell can't switch it to pay-per-use billing.

Two jobs:
- `research_area`: web search for what's good near the start point on the date.
- `select_stops`: pick the stops from real map candidates, with ranked
  alternatives per stop, returned as JSON that matches a schema.
`more_alternatives` asks for extra ideas for one stop when the swap list runs out.

Claude only chooses *what* to visit. Distances and ordering are computed in code.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from app.config import Settings
from app.models import DateRequest, Place, Slot


class ClaudeError(RuntimeError):
    """The Claude CLI failed, timed out or returned something unusable."""


@dataclass
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str


# (args, stdin text, timeout seconds, env) -> ProcessResult. Swappable in tests.
Runner = Callable[[Sequence[str], str, float, dict[str, str]], Awaitable[ProcessResult]]


async def run_subprocess(
    args: Sequence[str], stdin: str, timeout: float, env: dict[str, str]
) -> ProcessResult:
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except FileNotFoundError as exc:
        raise ClaudeError(
            f"Claude CLI not found: {args[0]!r}. Install Claude Code and log in, "
            "or set CLAUDE_CLI to its path."
        ) from exc
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin.encode()), timeout)
    except TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise ClaudeError(f"Claude CLI timed out after {timeout:g} s") from exc
    return ProcessResult(proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace"))


# ---- JSON schemas Claude must answer with -------------------------------------


class _Pick(BaseModel):
    place_id: str
    reason: str = ""
    visit_minutes: int | None = Field(default=None, gt=0)
    opening_hours: str | None = None  # Claude's guess, used when OSM has none


class _SlotPick(BaseModel):
    label: str
    alternatives: list[_Pick] = Field(min_length=1)


class _Selection(BaseModel):
    slots: list[_SlotPick] = Field(min_length=1)


class _MorePicks(BaseModel):
    alternatives: list[_Pick]


_PICK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "place_id": {"type": "string", "description": "id of a candidate from the list"},
        "reason": {"type": "string", "description": "one short sentence on why it fits"},
        "visit_minutes": {"type": "integer", "minimum": 1},
        "opening_hours": {
            "type": ["string", "null"],
            "description": "OSM opening_hours syntax; your best guess if unknown",
        },
    },
    "required": ["place_id", "reason", "visit_minutes"],
}

SELECTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "slots": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "alternatives": {"type": "array", "minItems": 1, "items": _PICK_SCHEMA},
                },
                "required": ["label", "alternatives"],
            },
        }
    },
    "required": ["slots"],
}

MORE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"alternatives": {"type": "array", "items": _PICK_SCHEMA}},
    "required": ["alternatives"],
}


# ---- The client ----------------------------------------------------------------


class ClaudeClient:
    def __init__(self, settings: Settings, runner: Runner | None = None) -> None:
        self.settings = settings
        self.runner = runner or run_subprocess

    def _args(self, *, tools: Sequence[str], json_schema: dict[str, Any] | None) -> list[str]:
        args = [
            self.settings.claude_cli,
            "-p",
            "--output-format",
            "json",
            "--no-session-persistence",
            # Only these built-in tools exist for the run, and they are pre-approved.
            "--tools",
            ",".join(tools),
        ]
        if tools:
            args += ["--allowedTools", ",".join(tools)]
        if json_schema is not None:
            args += ["--json-schema", json.dumps(json_schema)]
        if self.settings.claude_model:
            args += ["--model", self.settings.claude_model]
        return args

    async def run(
        self,
        prompt: str,
        *,
        tools: Sequence[str] = (),
        json_schema: dict[str, Any] | None = None,
    ) -> Any:
        """Run one headless prompt. Returns the text, or the parsed JSON when a schema is given."""
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        result = await self.runner(
            self._args(tools=tools, json_schema=json_schema),
            prompt,
            self.settings.claude_timeout_seconds,
            env,
        )
        envelope = _parse_envelope(result)
        if json_schema is None:
            return envelope.get("result") or ""
        if isinstance(envelope.get("structured_output"), dict):
            return envelope["structured_output"]
        return extract_json(envelope.get("result") or "")

    # -- Jobs --

    async def research_area(self, request: DateRequest) -> str:
        """Web-search what's good near the start point on the date. Returns notes."""
        return (await self.run(research_prompt(request), tools=["WebSearch"])).strip()

    async def select_stops(
        self,
        request: DateRequest,
        notes: str | None,
        candidates: Sequence[Place],
        feedback: str | None = None,
    ) -> list[Slot]:
        """Pick the stops from `candidates`, each with ranked alternatives."""
        if not candidates:
            raise ClaudeError("No candidate places to choose from")
        data = await self.run(
            selection_prompt(request, notes, candidates, feedback), json_schema=SELECTION_SCHEMA
        )
        try:
            selection = _Selection.model_validate(data)
        except ValidationError as exc:
            raise ClaudeError(f"Claude's selection didn't match the schema: {exc}") from exc
        by_id = {p.id: p for p in candidates}
        slots: list[Slot] = []
        used: set[str] = set()
        for slot in selection.slots:
            places = _to_places(slot.alternatives, by_id, used)
            if places:
                slots.append(Slot(label=slot.label, alternatives=places))
        if not slots:
            raise ClaudeError("Claude picked no places from the candidate list")
        return slots

    async def more_alternatives(
        self,
        request: DateRequest,
        notes: str | None,
        slot: Slot,
        candidates: Sequence[Place],
        exclude_ids: Sequence[str] = (),
    ) -> list[Place]:
        """Extra ideas for one stop, for when its swap list runs out."""
        skip = {p.id for p in slot.alternatives} | set(exclude_ids)
        pool = [p for p in candidates if p.id not in skip]
        if not pool:
            return []
        data = await self.run(more_prompt(request, notes, slot, pool), json_schema=MORE_SCHEMA)
        try:
            picks = _MorePicks.model_validate(data)
        except ValidationError as exc:
            raise ClaudeError(f"Claude's alternatives didn't match the schema: {exc}") from exc
        return _to_places(picks.alternatives, {p.id: p for p in pool}, set())


# ---- Helpers -------------------------------------------------------------------


def _parse_envelope(result: ProcessResult) -> dict[str, Any]:
    try:
        envelope = json.loads(result.stdout)
    except json.JSONDecodeError:
        envelope = None
    if not isinstance(envelope, dict):
        detail = (result.stderr or result.stdout).strip()[:500]
        raise ClaudeError(f"Claude CLI failed (exit {result.returncode}): {detail}")
    if result.returncode != 0 or envelope.get("is_error"):
        detail = envelope.get("result") or envelope.get("subtype") or result.stderr.strip()
        raise ClaudeError(f"Claude CLI returned an error: {str(detail)[:500]}")
    return envelope


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> Any:
    """Parse JSON from Claude's text answer, allowing ```json fences or prose around it."""
    candidates = [text.strip(), *(m.strip() for m in _FENCE.findall(text))]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for chunk in candidates:
        try:
            return json.loads(chunk)
        except json.JSONDecodeError:
            continue
    raise ClaudeError(f"Claude's answer had no JSON: {text.strip()[:200]}")


def _to_places(picks: Sequence[_Pick], by_id: dict[str, Place], used: set[str]) -> list[Place]:
    """Turn picks into Places, dropping unknown ids and places already used elsewhere."""
    places: list[Place] = []
    for pick in picks:
        base = by_id.get(pick.place_id)
        if base is None or pick.place_id in used:
            continue
        used.add(pick.place_id)
        update: dict[str, Any] = {"reason": pick.reason or base.reason}
        if pick.visit_minutes:
            update["visit_minutes"] = pick.visit_minutes
        if not base.opening_hours and pick.opening_hours:
            update["opening_hours"] = pick.opening_hours
            update["hours_verified"] = False
        places.append(base.model_copy(update=update))
    return places


def _describe_request(request: DateRequest) -> str:
    lines = [
        f"- Start: {request.start.name} ({request.start.lat:.5f}, {request.start.lon:.5f})"
        + (f", {request.start.address}" if request.start.address else ""),
        f"- When: {request.start_time:%A %d %B %Y, %H:%M}, for {request.duration_hours:g} hours",
        f"- Getting around: {request.transport.value}",
        f"- Cuisine: {request.cuisine or 'no preference'}",
    ]
    if request.here_to_there:
        lines.append("- Happy to travel further between stops (from here to there)")
    else:
        lines.append("- Keep stops close to each other and to the start")
    if request.ideas:
        lines.append(f"- Their ideas: {request.ideas}")
    return "\n".join(lines)


def _candidate_line(p: Place) -> str:
    parts = [p.id, p.name, p.category]
    if p.cuisine:
        parts.append(f"cuisine={p.cuisine}")
    if p.opening_hours:
        parts.append(f"hours={p.opening_hours}")
    parts.append(f"({p.lat:.5f}, {p.lon:.5f})")
    return " | ".join(parts)


def research_prompt(request: DateRequest) -> str:
    return (
        "You are helping a couple plan a date. Use web search to find what is good "
        "near the start point at that time.\n\n"
        f"{_describe_request(request)}\n\n"
        "Look for: events or seasonal things happening that day, well-reviewed "
        "restaurants (of the chosen cuisine, if any), cafes, dessert, bars, parks, "
        "viewpoints, museums and anything closing early that day. Give short notes as "
        "a bulleted list: place name, what it is, why it's good, and opening hours or "
        "event times when known. Only include places reachable from the start by the "
        "chosen transport. No introduction, just the notes."
    )


def selection_prompt(
    request: DateRequest,
    notes: str | None,
    candidates: Sequence[Place],
    feedback: str | None = None,
) -> str:
    listing = "\n".join(_candidate_line(p) for p in candidates)
    text = (
        "You are planning a date. Choose the stops (for example: an activity, dinner, "
        "then dessert or a walk) that fit the date length and the request. For each "
        "stop give 3-5 ranked alternatives, best first. Only use place_id values from "
        "the candidate list; never use a place in more than one stop. Set visit_minutes "
        "to a sensible time to spend there, and opening_hours (OSM syntax) as your best "
        "guess when the candidate has no hours. Do not plan the order or travel; that is "
        "computed separately.\n\n"
        f"Request:\n{_describe_request(request)}\n\n"
        f"Area research notes:\n{notes or '(none)'}\n\n"
        f"Candidates (place_id | name | category | details | coordinates):\n{listing}\n"
    )
    if feedback:
        text += f"\nThe previous plan didn't work: {feedback}\nFix that in this selection.\n"
    return text + "\nAnswer with JSON only."


def more_prompt(
    request: DateRequest, notes: str | None, slot: Slot, pool: Sequence[Place]
) -> str:
    seen = ", ".join(p.name for p in slot.alternatives)
    listing = "\n".join(_candidate_line(p) for p in pool)
    return (
        f"You are planning a date. The stop \"{slot.label}\" needs more ideas; they've "
        f"already seen: {seen}. Choose up to 5 more alternatives, best first, only from "
        "the candidate list below.\n\n"
        f"Request:\n{_describe_request(request)}\n\n"
        f"Area research notes:\n{notes or '(none)'}\n\n"
        f"Candidates (place_id | name | category | details | coordinates):\n{listing}\n\n"
        "Answer with JSON only."
    )
