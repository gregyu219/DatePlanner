import asyncio
from datetime import datetime

import pytest

from app import intake
from app.intake import IntakeState, Step
from app.models import Location, TransportMode

# Saturday 14 Nov 2026, 15:00 local time.
NOW = datetime(2026, 11, 14, 15, 0)

UNION = Location(name="Union Square", lat=37.788, lon=-122.4075, address="Union Square, San Francisco")
FERRY = Location(name="Ferry Building", lat=37.7955, lon=-122.3937, address="Ferry Building, San Francisco")


def make_geocoder(results=None, error=None):
    calls = []

    async def geocode(query):
        calls.append(query)
        if error:
            raise error
        return results if results is not None else [UNION]

    geocode.calls = calls
    return geocode


def say(state, text, geocode=None):
    return asyncio.run(intake.answer(state, text, geocode=geocode or make_geocoder(), now=NOW))


def run_chat(answers, geocode=None):
    state, _ = intake.start_chat()
    reply = None
    for text in answers:
        state, reply = say(state, text, geocode)
    return state, reply


FULL = ["Union Square", "yes", "bus", "tonight 6pm", "4 hours", "Japanese", "close together", "skip"]


def test_start_chat_asks_for_start_place():
    state, reply = intake.start_chat()
    assert state.step == Step.start
    assert "Where will you start" in reply.messages[-1]


def test_full_chat_builds_date_request():
    state, reply = run_chat(FULL)

    assert state.done
    request = state.to_request()
    assert request.start == UNION
    assert request.transport == TransportMode.transit
    assert request.start_time == datetime(2026, 11, 14, 18, 0)
    assert request.duration_hours == 4
    assert request.cuisine == "japanese"
    assert request.here_to_there is False
    assert request.ideas is None
    assert "Here's what I have" in reply.messages[0]
    assert "bus / transit" in reply.messages[0]


def test_questions_come_in_order():
    state, _ = intake.start_chat()
    steps = [state.step]
    for text in FULL:
        state, _ = say(state, text)
        steps.append(state.step)
    assert steps == [
        Step.start, Step.confirm_start, Step.transport, Step.start_time, Step.duration,
        Step.cuisine, Step.here_to_there, Step.ideas, Step.done,
    ]


def test_bus_answer_mentions_one_hour_limit():
    _, reply = run_chat(FULL[:3])
    assert "under 1 hour" in reply.messages[0]


def test_start_place_with_several_matches_lets_user_pick():
    geocode = make_geocoder([UNION, FERRY])
    state, reply = run_chat(["the square"], geocode)
    assert state.step == Step.confirm_start
    assert reply.options == ["1", "2", "None of these"]

    state, _ = say(state, "2", geocode)
    assert state.start == FERRY
    assert state.step == Step.transport


def test_rejecting_match_asks_again():
    state, _ = run_chat(["Union Square", "no"])
    assert state.step == Step.start
    assert state.start is None


def test_unknown_place_and_lookup_failure_stay_on_question():
    state, reply = run_chat(["Atlantis"], make_geocoder([]))
    assert state.step == Step.start
    assert "couldn't find" in reply.messages[0]

    state, reply = run_chat(["Union Square"], make_geocoder(error=RuntimeError("down")))
    assert state.step == Step.start
    assert "isn't working" in reply.messages[0]


def test_invalid_answers_are_asked_again():
    state, reply = run_chat(FULL[:2] + ["rocket ship"])
    assert state.step == Step.transport
    assert "didn't get that" in reply.messages[0]
    assert reply.options  # buttons still shown

    state, _ = say(state, "car")
    state, reply = say(state, "whenever")
    assert state.step == Step.start_time
    state, _ = say(state, "6pm")
    state, reply = say(state, "forever")
    assert state.step == Step.duration
    state, reply = say(state, "30 hours")
    assert state.step == Step.duration


def test_time_in_the_past_is_rejected():
    state, reply = run_chat(FULL[:3] + ["today 9am"])
    assert state.step == Step.start_time
    assert "already passed" in reply.messages[0]


def test_back_reasks_previous_question():
    state, _ = run_chat(FULL[:4])
    assert state.step == Step.duration
    state, reply = say(state, "back")
    assert state.step == Step.start_time
    state, _ = say(state, "back")
    assert state.step == Step.transport
    state, _ = say(state, "walk")
    assert state.transport == TransportMode.walk
    assert state.start_time is not None  # earlier answer kept until re-answered


def test_back_at_first_question_stays_put():
    state, _ = intake.start_chat()
    state, _ = say(state, "back")
    assert state.step == Step.start


def test_answer_does_not_modify_given_state():
    state, _ = intake.start_chat()
    asyncio.run(intake.answer(state, "Union Square", geocode=make_geocoder(), now=NOW))
    assert state.step == Step.start


def test_here_to_there_and_ideas():
    answers = FULL[:6] + ["here to there", "Sunset at the beach"]
    state, _ = run_chat(answers)
    assert state.here_to_there is True
    assert state.ideas == "Sunset at the beach"


def test_state_round_trips_through_json():
    state, _ = run_chat(FULL[:5])
    again = IntakeState.model_validate(state.model_dump(mode="json"))
    assert again == state


@pytest.mark.parametrize(
    "text, expected",
    [
        ("car", TransportMode.car),
        ("we'll drive", TransportMode.car),
        ("walking", TransportMode.walk),
        ("on foot", TransportMode.walk),
        ("our bikes", TransportMode.bike),
        ("taking the buses", TransportMode.transit),
        ("bike", TransportMode.bike),
        ("bus / transit", TransportMode.transit),
        ("public transport", TransportMode.transit),
        ("car or bike", None),
        ("teleport", None),
    ],
)
def test_parse_transport(text, expected):
    assert intake.parse_transport(intake._normalize(text)) == expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("tonight 6pm", datetime(2026, 11, 14, 18, 0)),
        ("tonight at 7", datetime(2026, 11, 14, 19, 0)),
        ("6:30 pm", datetime(2026, 11, 14, 18, 30)),
        ("18:30", datetime(2026, 11, 14, 18, 30)),
        ("7", datetime(2026, 11, 14, 19, 0)),
        ("10:00", datetime(2026, 11, 15, 10, 0)),  # already past today -> tomorrow
        ("tomorrow 11am", datetime(2026, 11, 15, 11, 0)),
        ("sunday 2pm", datetime(2026, 11, 15, 14, 0)),
        ("next saturday 2pm", datetime(2026, 11, 21, 14, 0)),
        ("Friday at noon", datetime(2026, 11, 20, 12, 0)),
        ("2026-11-20 18:30", datetime(2026, 11, 20, 18, 30)),
        ("11/20 7pm", datetime(2026, 11, 20, 19, 0)),
        ("Nov 20 at 6pm", datetime(2026, 11, 20, 18, 0)),
        ("20th december 5pm", datetime(2026, 12, 20, 17, 0)),
        ("whenever", None),
        ("25:00", None),
        ("13pm", None),
    ],
)
def test_parse_start_time(text, expected):
    assert intake.parse_start_time(intake._normalize(text), NOW) == expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("3", 3),
        ("3 hours", 3),
        ("2.5h", 2.5),
        ("2,5 hours", 2.5),
        ("2h30", 2.5),
        ("2:30", 2.5),
        ("90 min", 1.5),
        ("an hour", 1),
        ("two hours", 2),
        ("about 4 hrs", 4),
        ("3 and a half hours", 3.5),
        ("3 hours and a half", 3.5),
        ("half an hour", 0.5),
        ("1 hour 45 minutes", 1.75),
        ("3 4", None),
        ("all day", 10),
        ("forever", None),
    ],
)
def test_parse_duration(text, expected):
    assert intake.parse_duration_hours(intake._normalize(text)) == expected


@pytest.mark.parametrize(
    "text, expected",
    [("Any", None), ("no preference", None), ("skip", None), ("Japanese", "japanese"), ("  Thai  food ", "thai food")],
)
def test_parse_cuisine(text, expected):
    assert intake.parse_cuisine(text) == expected


def test_format_hours():
    assert intake.format_hours(4) == "4 hours"
    assert intake.format_hours(1) == "1 hour"
    assert intake.format_hours(2.5) == "2h30"
    assert intake.format_hours(0.5) == "30 minutes"
