import asyncio
import json
import stat
import sys

import pytest

from app.models import Slot
from app.planner.llm import (
    SELECTION_SCHEMA,
    ClaudeClient,
    ClaudeError,
    ProcessResult,
    extract_json,
)


def envelope(**fields) -> str:
    data = {"type": "result", "subtype": "success", "is_error": False, "result": ""}
    data.update(fields)
    return json.dumps(data)


class FakeRunner:
    """Records each CLI call and replies with queued ProcessResults."""

    def __init__(self, *replies: ProcessResult) -> None:
        self.replies = list(replies)
        self.calls: list[dict] = []

    async def __call__(self, args, stdin, timeout, env):
        self.calls.append({"args": list(args), "stdin": stdin, "timeout": timeout, "env": env})
        return self.replies.pop(0)


def ok(stdout: str) -> ProcessResult:
    return ProcessResult(0, stdout, "")


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def candidates(make_place):
    return [
        make_place(1, name="Sushi Ko", cuisine="japanese", opening_hours="Mo-Su 17:00-22:00"),
        make_place(2, name="Ramen Bar", cuisine="japanese"),
        make_place(3, name="Golden Park", category="park"),
        make_place(4, name="Gelato", category="ice_cream"),
    ]


# ---- research ------------------------------------------------------------------


def test_research_runs_headless_with_web_search_only(settings, date_request, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "should-not-be-passed")
    runner = FakeRunner(ok(envelope(result="  - Night market on 4th St  \n")))
    client = ClaudeClient(settings, runner)

    notes = run(client.research_area(date_request))

    assert notes == "- Night market on 4th St"
    call = runner.calls[0]
    args = call["args"]
    assert args[:4] == ["claude", "-p", "--output-format", "json"]
    assert args[args.index("--tools") + 1] == "WebSearch"
    assert args[args.index("--allowedTools") + 1] == "WebSearch"
    assert "--json-schema" not in args
    assert "--model" not in args
    assert "ANTHROPIC_API_KEY" not in call["env"]
    assert call["timeout"] == settings.claude_timeout_seconds
    # The prompt goes on stdin and describes the request.
    assert "Union Square" in call["stdin"]
    assert "japanese" in call["stdin"]
    assert "Saturday 14 November 2026, 17:00" in call["stdin"]


def test_model_setting_is_passed(settings, date_request):
    settings.claude_model = "sonnet"
    runner = FakeRunner(ok(envelope(result="notes")))
    run(ClaudeClient(settings, runner).research_area(date_request))
    args = runner.calls[0]["args"]
    assert args[args.index("--model") + 1] == "sonnet"


# ---- selection -----------------------------------------------------------------


def selection_payload():
    return {
        "slots": [
            {
                "label": "Dinner",
                "alternatives": [
                    {"place_id": "osm:node/2", "reason": "Cozy ramen", "visit_minutes": 75,
                     "opening_hours": "Mo-Su 11:00-23:00"},
                    {"place_id": "osm:node/1", "reason": "Great sushi", "visit_minutes": 90,
                     "opening_hours": "Mo-Su 12:00-14:00"},
                    {"place_id": "osm:node/999", "reason": "Made up", "visit_minutes": 60},
                ],
            },
            {
                "label": "Walk",
                "alternatives": [
                    # Already used for dinner: dropped.
                    {"place_id": "osm:node/1", "reason": "dup", "visit_minutes": 30},
                    {"place_id": "osm:node/3", "reason": "Sunset stroll", "visit_minutes": 45},
                ],
            },
            {"label": "Ghost", "alternatives": [{"place_id": "nope", "reason": "", "visit_minutes": 5}]},
        ]
    }


def test_select_stops_uses_structured_output(settings, date_request, candidates):
    runner = FakeRunner(ok(envelope(result="done", structured_output=selection_payload())))
    client = ClaudeClient(settings, runner)

    slots = run(client.select_stops(date_request, "notes here", candidates, feedback="too long"))

    assert [s.label for s in slots] == ["Dinner", "Walk"]  # empty slot dropped
    dinner, walk = slots
    assert [p.id for p in dinner.alternatives] == ["osm:node/2", "osm:node/1"]
    ramen, sushi = dinner.alternatives
    # Guessed hours fill in only where OSM has none, and are marked unverified.
    assert ramen.opening_hours == "Mo-Su 11:00-23:00"
    assert ramen.hours_verified is False
    assert ramen.visit_minutes == 75
    assert ramen.reason == "Cozy ramen"
    assert sushi.opening_hours == "Mo-Su 17:00-22:00"
    assert [p.id for p in walk.alternatives] == ["osm:node/3"]
    assert walk.current_index == 0 and walk.history == []

    call = runner.calls[0]
    args = call["args"]
    assert args[args.index("--tools") + 1] == ""  # no tools for picking
    assert "--allowedTools" not in args
    assert json.loads(args[args.index("--json-schema") + 1]) == SELECTION_SCHEMA
    for text in ("notes here", "too long", "osm:node/4 | Gelato | ice_cream"):
        assert text in call["stdin"]


def test_select_stops_falls_back_to_json_in_text(settings, date_request, candidates):
    text = "Here you go:\n```json\n" + json.dumps(selection_payload()) + "\n```"
    runner = FakeRunner(ok(envelope(result=text)))
    slots = run(ClaudeClient(settings, runner).select_stops(date_request, None, candidates))
    assert [s.label for s in slots] == ["Dinner", "Walk"]


def test_select_stops_rejects_bad_schema(settings, date_request, candidates):
    runner = FakeRunner(ok(envelope(structured_output={"slots": [{"label": "x"}]})))
    with pytest.raises(ClaudeError, match="schema"):
        run(ClaudeClient(settings, runner).select_stops(date_request, None, candidates))


def test_select_stops_rejects_unknown_places(settings, date_request, candidates):
    payload = {"slots": [{"label": "Dinner", "alternatives": [{"place_id": "x", "reason": "", "visit_minutes": 5}]}]}
    runner = FakeRunner(ok(envelope(structured_output=payload)))
    with pytest.raises(ClaudeError, match="no places"):
        run(ClaudeClient(settings, runner).select_stops(date_request, None, candidates))


def test_select_stops_needs_candidates(settings, date_request):
    runner = FakeRunner()
    with pytest.raises(ClaudeError, match="No candidate"):
        run(ClaudeClient(settings, runner).select_stops(date_request, None, []))
    assert runner.calls == []


# ---- more alternatives ---------------------------------------------------------


def test_more_alternatives_excludes_seen_places(settings, date_request, candidates):
    slot = Slot(label="Dessert", alternatives=[candidates[3]])
    payload = {"alternatives": [
        {"place_id": "osm:node/4", "reason": "seen", "visit_minutes": 20},
        {"place_id": "osm:node/2", "reason": "Late-night ramen", "visit_minutes": 40},
    ]}
    runner = FakeRunner(ok(envelope(structured_output=payload)))

    places = run(ClaudeClient(settings, runner).more_alternatives(
        date_request, None, slot, candidates, exclude_ids=["osm:node/1"]
    ))

    assert [p.id for p in places] == ["osm:node/2"]
    prompt = runner.calls[0]["stdin"]
    assert "osm:node/1 |" not in prompt and "osm:node/4 |" not in prompt
    assert "osm:node/2 |" in prompt and "already seen: Gelato" in prompt


def test_more_alternatives_with_empty_pool_skips_claude(settings, date_request, candidates):
    slot = Slot(label="All", alternatives=candidates)
    runner = FakeRunner()
    assert run(ClaudeClient(settings, runner).more_alternatives(date_request, None, slot, candidates)) == []
    assert runner.calls == []


# ---- errors and parsing --------------------------------------------------------


@pytest.mark.parametrize(
    "reply, message",
    [
        (ProcessResult(1, "", "Not logged in"), "Not logged in"),
        (ProcessResult(0, "not json", ""), "not json"),
        (ProcessResult(0, envelope(is_error=True, subtype="error_max_turns", result=None), ""), "error_max_turns"),
        (ProcessResult(1, envelope(result="Credit balance too low"), ""), "Credit balance"),
    ],
)
def test_cli_errors_raise_claude_error(settings, date_request, reply, message):
    with pytest.raises(ClaudeError, match=message):
        run(ClaudeClient(settings, FakeRunner(reply)).research_area(date_request))


def test_extract_json_variants():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('Sure!\n```json\n{"a": 2}\n```\nEnjoy') == {"a": 2}
    assert extract_json('Result: {"a": {"b": 3}} done') == {"a": {"b": 3}}
    with pytest.raises(ClaudeError):
        extract_json("no json here")


# ---- real subprocess, with a fake `claude` script ----------------------------


def fake_cli(tmp_path, body: str):
    script = tmp_path / "fake-claude"
    script.write_text(f"#!{sys.executable}\nimport json, os, sys, time\n{body}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def test_subprocess_round_trip(settings, date_request, tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "should-not-be-passed")
    settings.claude_cli = fake_cli(
        tmp_path,
        "prompt = sys.stdin.read()\n"
        "print(json.dumps({'type': 'result', 'is_error': False, 'result': json.dumps({\n"
        "    'argv': sys.argv[1:], 'has_key': 'ANTHROPIC_API_KEY' in os.environ,\n"
        "    'prompt_has_start': 'Union Square' in prompt})}))",
    )
    answer = json.loads(run(ClaudeClient(settings).research_area(date_request)))
    assert answer["argv"][:3] == ["-p", "--output-format", "json"]
    assert answer["has_key"] is False
    assert answer["prompt_has_start"] is True


def test_subprocess_timeout(settings, date_request, tmp_path):
    settings.claude_cli = fake_cli(tmp_path, "time.sleep(10)")
    settings.claude_timeout_seconds = 0.3
    with pytest.raises(ClaudeError, match="timed out"):
        run(ClaudeClient(settings).research_area(date_request))


def test_missing_cli(settings, date_request, tmp_path):
    settings.claude_cli = str(tmp_path / "no-such-claude")
    with pytest.raises(ClaudeError, match="not found"):
        run(ClaudeClient(settings).research_area(date_request))
