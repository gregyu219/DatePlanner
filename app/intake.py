"""The opening chat: a scripted list of questions that fills in a DateRequest.

The chat is a small state machine, not free-form LLM chat, so the required
questions are always asked, in order:

1. Where do you start? (looked up with Nominatim, then confirmed)
2. How are you getting around? (car / walk / bike / bus)
3. When does the date start?
4. How long is the date?
5. Any cuisine?
6. Stay close together, or go "from here to there"?
7. Any ideas? (optional)

Typing "back" re-asks the previous question. `IntakeState` is saved between
messages, so the chat survives a restart.
"""

import re
from collections.abc import Awaitable, Callable
from datetime import date, datetime, time, timedelta
from enum import StrEnum

from pydantic import BaseModel, Field

from app.models import DateRequest, Location, TransportMode

Geocoder = Callable[[str], Awaitable[list[Location]]]


class Step(StrEnum):
    start = "start"
    confirm_start = "confirm_start"
    transport = "transport"
    start_time = "start_time"
    duration = "duration"
    cuisine = "cuisine"
    here_to_there = "here_to_there"
    ideas = "ideas"
    done = "done"


# Order used by "back". confirm_start goes back to start.
ORDER = [
    Step.start,
    Step.transport,
    Step.start_time,
    Step.duration,
    Step.cuisine,
    Step.here_to_there,
    Step.ideas,
]


class IntakeState(BaseModel):
    step: Step = Step.start
    start: Location | None = None
    start_options: list[Location] = Field(default_factory=list)
    transport: TransportMode | None = None
    start_time: datetime | None = None
    duration_hours: float | None = None
    cuisine: str | None = None
    here_to_there: bool = False
    ideas: str | None = None

    @property
    def done(self) -> bool:
        return self.step == Step.done

    def to_request(self) -> DateRequest:
        assert self.start and self.transport and self.start_time and self.duration_hours
        return DateRequest(
            start=self.start,
            transport=self.transport,
            start_time=self.start_time,
            duration_hours=self.duration_hours,
            cuisine=self.cuisine,
            here_to_there=self.here_to_there,
            ideas=self.ideas,
        )


class Reply(BaseModel):
    """What the bot says back, plus quick-reply buttons for the next answer."""

    messages: list[str]
    options: list[str] = Field(default_factory=list)


GREETING = "Hi! Let's plan a date. I'll ask a few quick questions, then research the area and build the plan."

TRANSPORT_WORDS = {
    TransportMode.car: {"car", "drive", "driving", "uber", "taxi", "lyft"},
    TransportMode.walk: {"walk", "walking", "foot", "on foot", "feet"},
    TransportMode.bike: {"bike", "biking", "bicycle", "cycle", "cycling", "scooter"},
    TransportMode.transit: {
        "bus", "buse", "transit", "public transit", "public transport", "train", "metro",
        "subway", "tram", "light rail", "muni", "bart",
    },
}
TRANSPORT_LABELS = {
    TransportMode.car: "car",
    TransportMode.walk: "walking",
    TransportMode.bike: "bike",
    TransportMode.transit: "bus / transit",
}
NO_PREFERENCE = {
    "any", "anything", "none", "no", "nope", "n/a", "na", "skip", "no preference",
    "whatever", "don't care", "dont care", "doesn't matter", "doesnt matter", "-",
}
YES = {"yes", "y", "yeah", "yep", "sure", "ok", "okay", "correct", "right", "that's it", "thats it"}
NO = {"no", "n", "nope", "nah"}


def question(state: IntakeState) -> Reply:
    """The question for the current step."""
    match state.step:
        case Step.start:
            return Reply(messages=["Where will you start? Type an address or a place name."])
        case Step.confirm_start:
            opts = state.start_options
            lines = [f"{i}. {loc.address or loc.name}" for i, loc in enumerate(opts, 1)]
            if len(opts) == 1:
                text = f"I found: {opts[0].address or opts[0].name}\nIs that right?"
                options = ["Yes", "No"]
            else:
                text = "I found a few matches. Which one?\n" + "\n".join(lines)
                options = [str(i) for i in range(1, len(lines) + 1)] + ["None of these"]
            return Reply(messages=[text], options=options)
        case Step.transport:
            return Reply(
                messages=["How are you getting around: car, walking, bike, or bus/transit?"],
                options=["Car", "Walking", "Bike", "Bus / transit"],
            )
        case Step.start_time:
            return Reply(
                messages=["When does the date start? For example \"tonight 6pm\", \"Saturday 2pm\" or \"2026-11-20 18:30\"."],
                options=["Tonight 6pm", "Tomorrow 6pm", "Saturday 2pm"],
            )
        case Step.duration:
            return Reply(
                messages=["How long is the date? For example \"3 hours\" or \"2h30\"."],
                options=["2 hours", "3 hours", "4 hours", "6 hours"],
            )
        case Step.cuisine:
            return Reply(
                messages=["Any type of cuisine you'd like? (say \"any\" if you don't mind)"],
                options=["Any", "Italian", "Japanese", "Mexican", "Thai"],
            )
        case Step.here_to_there:
            return Reply(
                messages=[
                    "Should all the stops be close together, or are you happy to travel "
                    "\"from here to there\" between farther-apart places?"
                ],
                options=["Close together", "Here to there"],
            )
        case Step.ideas:
            return Reply(
                messages=["Last one: any ideas or things you'd love to include? (or \"skip\")"],
                options=["Skip"],
            )
        case Step.done:
            return Reply(messages=[summary(state), "Great, I'm on it! I'll research the area and build the plan now."])


def start_chat() -> tuple[IntakeState, Reply]:
    state = IntakeState()
    first = question(state)
    return state, Reply(messages=[GREETING, *first.messages], options=first.options)


async def answer(
    state: IntakeState, text: str, *, geocode: Geocoder, now: datetime | None = None
) -> tuple[IntakeState, Reply]:
    """Handle one user message. Returns the new state and the bot's reply.

    The given state is not modified.
    """
    state = state.model_copy(deep=True)
    text = " ".join(text.split())
    norm = _normalize(text)
    now = now or datetime.now()

    if state.done:
        return state, Reply(messages=["This date is already being planned."])
    if not text:
        return state, question(state)
    if norm in {"back", "go back", "undo", "previous"}:
        return _go_back(state)

    error: str | None = None
    note: str | None = None
    match state.step:
        case Step.start:
            try:
                matches = await geocode(text)
            except Exception:  # noqa: BLE001 - any lookup failure is shown, not raised
                return state, Reply(
                    messages=["Sorry, the address lookup isn't working right now. Please try again in a moment."]
                )
            if not matches:
                error = "I couldn't find that place. Try a fuller address, or add the city."
            else:
                state.start_options = matches[:5]
                state.step = Step.confirm_start
        case Step.confirm_start:
            chosen = _pick_option(norm, state.start_options)
            if chosen is not None:
                state.start = chosen
                state.start_options = []
                state.step = Step.transport
                note = f"Starting at {chosen.name}."
            elif norm in NO or norm.startswith("none"):
                state.start_options = []
                state.step = Step.start
                return state, Reply(messages=["No problem. Try typing the address in more detail, with the city."])
            else:
                error = "Please pick one of the matches, or say \"none of these\"."
        case Step.transport:
            mode = parse_transport(norm)
            if mode is None:
                error = "Sorry, I didn't get that. Is it car, walking, bike, or bus/transit?"
            else:
                state.transport = mode
                state.step = Step.start_time
                if mode == TransportMode.transit:
                    note = "Got it, bus/transit. I'll keep each trip between stops under 1 hour."
                else:
                    note = f"Got it, {TRANSPORT_LABELS[mode]}."
        case Step.start_time:
            when = parse_start_time(norm, now)
            if when is None:
                error = "I couldn't read that time. Try something like \"Saturday 6pm\" or \"2026-11-20 18:30\"."
            elif when < now - timedelta(minutes=30):
                error = "That time has already passed. When does the date start?"
            else:
                state.start_time = when
                state.step = Step.duration
                note = f"Starting {format_when(when, now)}."
        case Step.duration:
            hours = parse_duration_hours(norm)
            if hours is None:
                error = "How many hours? For example \"3 hours\" or \"2h30\"."
            elif not 0.5 <= hours <= 24:
                error = "Please pick a length between 30 minutes and 24 hours."
            else:
                state.duration_hours = hours
                state.step = Step.cuisine
        case Step.cuisine:
            state.cuisine = parse_cuisine(text)
            state.step = Step.here_to_there
        case Step.here_to_there:
            choice = parse_here_to_there(norm)
            if choice is None:
                error = "Close together, or here to there?"
            else:
                state.here_to_there = choice
                state.step = Step.ideas
        case Step.ideas:
            state.ideas = None if norm in NO_PREFERENCE else text
            state.step = Step.done

    if error:
        reply = question(state)
        return state, Reply(messages=[error], options=reply.options)
    reply = question(state)
    if note:
        reply.messages.insert(0, note)
    return state, reply


def _go_back(state: IntakeState) -> tuple[IntakeState, Reply]:
    current = Step.start if state.step == Step.confirm_start else state.step
    index = ORDER.index(current)
    if index == 0:
        state.start_options = []
        state.step = Step.start
        return state, question(state)
    state.step = ORDER[index - 1]
    state.start_options = []
    reply = question(state)
    reply.messages.insert(0, "Okay, let's redo that one.")
    return state, reply


def summary(state: IntakeState) -> str:
    assert state.start and state.transport and state.start_time and state.duration_hours
    lines = [
        "Here's what I have:",
        f"- Start: {state.start.address or state.start.name}",
        f"- Getting around: {TRANSPORT_LABELS[state.transport]}",
        f"- When: {state.start_time:%a %d %b, %H:%M} for {format_hours(state.duration_hours)}",
        f"- Cuisine: {state.cuisine or 'any'}",
        f"- Stops: {'here to there' if state.here_to_there else 'close together'}",
    ]
    if state.ideas:
        lines.append(f"- Ideas: {state.ideas}")
    return "\n".join(lines)


# --- parsers -----------------------------------------------------------------


def _normalize(text: str) -> str:
    return " ".join(text.lower().replace("’", "'").split()).strip(" .!?")


def _pick_option(norm: str, options: list[Location]) -> Location | None:
    if not options:
        return None
    if len(options) == 1 and norm in YES:
        return options[0]
    m = re.fullmatch(r"(?:#|no\.?|number|option)?\s*(\d+)", norm)
    if m and 1 <= int(m.group(1)) <= len(options):
        return options[int(m.group(1)) - 1]
    if norm in YES or norm in {"first", "the first one", "first one"}:
        return options[0]
    return None


def parse_transport(norm: str) -> TransportMode | None:
    words = set(re.split(r"[^a-z]+", norm)) | {norm}
    words |= {w[:-1] for w in words if w.endswith("s")}  # "bikes", "cars", "buses"->"buse"
    for mode, keys in TRANSPORT_WORDS.items():
        if norm in keys:
            return mode
    found = [mode for mode, keys in TRANSPORT_WORDS.items() if words & keys]
    return found[0] if len(found) == 1 else None


def parse_cuisine(text: str) -> str | None:
    norm = _normalize(text)
    if norm in NO_PREFERENCE or norm.startswith("no pref"):
        return None
    return norm or None


def parse_here_to_there(norm: str) -> bool | None:
    if any(w in norm for w in ("here to there", "far", "travel", "anywhere", "spread")):
        return True
    if any(w in norm for w in ("close", "near", "together", "nearby", "local")) or norm in NO:
        return False
    if norm in YES:
        return True
    return None


def parse_duration_hours(norm: str) -> float | None:
    """'3', '3 hours', '2.5h', '2h30', '2:30', '90 min', 'half a day', 'all day'."""
    norm = norm.replace(",", ".")
    if norm in {"all day", "full day", "whole day"}:
        return 10.0
    if norm in {"half day", "half a day"}:
        return 4.0
    if m := re.fullmatch(r"(\d+):(\d{2})", norm):
        return int(m.group(1)) + int(m.group(2)) / 60

    norm = re.sub(r"\bhalf (?:an )?hour\b", "30 min", norm)
    norm = re.sub(r"\b(?:and )?an? half\b", " half", norm)
    tokens = re.findall(r"\d+(?:\.\d+)?|[a-z]+", norm)
    total = 0.0
    pending: float | None = None  # a number waiting for its unit
    seen_hours = False
    for token in tokens:
        if token in FILLER_WORDS:
            continue
        if token in NUMBER_WORDS or token[0].isdigit():
            if pending is not None:
                return None
            pending = float(NUMBER_WORDS[token]) if token in NUMBER_WORDS else float(token)
        elif token in HOUR_UNITS:
            if pending is not None:
                total += pending
            elif not total:
                total = 1.0  # "hour"
            pending, seen_hours = None, True
        elif token in MINUTE_UNITS:
            if pending is None:
                return None
            total += pending / 60
            pending = None
        elif token == "half":  # "3 and a half (hours)", "3 hours and a half"
            total += (pending or 0) + 0.5
            pending, seen_hours = None, True
        else:
            return None
    if pending is not None:  # "3" -> hours; the "30" in "2h30" -> minutes
        total += pending / 60 if seen_hours else pending
    return total or None


NUMBER_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
                "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "twelve": 12}
HOUR_UNITS = {"h", "hr", "hrs", "hour", "hours"}
MINUTE_UNITS = {"m", "min", "mins", "minute", "minutes"}
FILLER_WORDS = {"about", "around", "maybe", "roughly", "like", "for", "and", "or", "so", "ish", "approx"}

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


def parse_start_time(norm: str, now: datetime) -> datetime | None:
    """Read a local start time like 'tonight 6pm', 'sat 14:00', '2026-11-20 18:30', '7:30 pm'.

    Without a day, the next time that clock time comes round is used (today or tomorrow).
    Without am/pm, 1-7 o'clock (and any hour after "tonight") is read as afternoon/evening,
    since that's when most dates start.
    """
    text = norm.replace(".", "")
    day, rest = _parse_day(text, now)
    clock = _parse_clock(rest, evening=("tonight" in text or "evening" in text))
    if clock is None:
        return None
    when = datetime.combine(day or now.date(), clock)
    if day is None and when < now:
        when += timedelta(days=1)
    return when


def _parse_day(text: str, now: datetime) -> tuple[date | None, str]:
    today = now.date()
    if m := re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", text):
        try:
            return date(int(m[1]), int(m[2]), int(m[3])), _cut(text, m)
        except ValueError:
            return None, text
    if m := re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", text):
        year = int(m[3]) if m[3] else today.year
        year += 2000 if year < 100 else 0
        try:
            d = date(year, int(m[1]), int(m[2]))  # month/day
        except ValueError:
            return None, text
        if not m[3] and d < today:
            d = d.replace(year=d.year + 1)
        return d, _cut(text, m)
    month_re = "|".join(MONTHS)
    if m := re.search(rf"\b(?:({month_re})[a-z]*\s+(\d{{1,2}})(?:st|nd|rd|th)?|(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({month_re})[a-z]*)\b", text):
        month = MONTHS.index((m[1] or m[4])[:3]) + 1
        dom = int(m[2] or m[3])
        try:
            d = date(today.year, month, dom)
        except ValueError:
            return None, text
        if d < today:
            d = d.replace(year=d.year + 1)
        return d, _cut(text, m)
    if m := re.search(r"\b(today|tonight|this evening|this afternoon)\b", text):
        return today, _cut(text, m)
    if m := re.search(r"\btomorrow\b", text):
        return today + timedelta(days=1), _cut(text, m)
    if m := re.search(r"\b(?:next |this |on )?(mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)[a-z]*\b", text):
        target = next(i for i, d in enumerate(WEEKDAYS) if d.startswith(m[1][:3]))
        ahead = (target - today.weekday()) % 7
        if m[0].startswith("next ") and ahead == 0:
            ahead = 7
        return today + timedelta(days=ahead), _cut(text, m)
    return None, text


def _cut(text: str, m: re.Match) -> str:
    return (text[: m.start()] + " " + text[m.end():]).strip()


def _parse_clock(text: str, evening: bool = False) -> time | None:
    text = re.sub(r"\b(at|from|around|about|starting|start|on|in the|evening|afternoon|morning)\b", " ", text)
    text = " ".join(text.split()).strip(" ,@")
    if text in {"noon", "midday"}:
        return time(12, 0)
    m = re.fullmatch(r"(\d{1,2})(?:[:h](\d{2}))?\s*(am|pm|a|p)?", text)
    if not m:
        return None
    hour, minute = int(m[1]), int(m[2] or 0)
    suffix = m[3]
    if minute > 59:
        return None
    if suffix:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if suffix.startswith("p") else 0)
    elif 1 <= hour <= 11 and (evening or hour <= 7):
        hour += 12  # "6", "7:30" or "tonight 9" -> evening; "10:00" stays morning
    if hour > 23:
        return None
    return time(hour, minute)


def format_when(when: datetime, now: datetime) -> str:
    if when.date() == now.date():
        day = "today"
    elif when.date() == now.date() + timedelta(days=1):
        day = "tomorrow"
    else:
        day = f"{when:%A %d %B}"
    return f"{day} at {when:%H:%M}"


def format_hours(hours: float) -> str:
    total = round(hours * 60)
    h, m = divmod(total, 60)
    if h and m:
        return f"{h}h{m:02d}"
    if h:
        return f"{h} hour{'s' if h != 1 else ''}"
    return f"{m} minutes"
