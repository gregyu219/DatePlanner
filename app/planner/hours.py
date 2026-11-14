"""A small parser for the common forms of the OSM ``opening_hours`` tag.

Supported: ``24/7``, weekday lists and ranges (``Mo,We``, ``Mo-Fr``, ``Fr-Mo``),
several time ranges (``11:00-14:00,17:00-22:00``), times without days
(``10:00-20:00``), hours past midnight (``18:00-02:00``), ``off``/``closed``
and several rules separated by ``;`` (later rules replace earlier ones for the
days they name). Holiday rules (``PH``/``SH``) are ignored.

Anything else (months, week numbers, ``sunrise``, comments, ...) makes the
whole value "unknown": :func:`open_intervals` returns ``None`` and callers
should treat the place as open.
"""

import re
from datetime import date, datetime, timedelta

DAYS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]
_TIME_RANGE = re.compile(r"^(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})$")
_DAY_RANGE = re.compile(r"^(Mo|Tu|We|Th|Fr|Sa|Su)(?:-(Mo|Tu|We|Th|Fr|Sa|Su))?$")

# Weekly schedule: weekday (0=Mo) -> list of (start_minute, end_minute).
# end_minute may exceed 1440 for hours that run past midnight.
Schedule = dict[int, list[tuple[int, int]]]


class _Unknown(Exception):
    pass


def _parse_days(text: str) -> set[int]:
    days: set[int] = set()
    for part in text.split(","):
        match = _DAY_RANGE.match(part.strip())
        if not match:
            raise _Unknown(part)
        first = DAYS.index(match.group(1))
        last = DAYS.index(match.group(2)) if match.group(2) else first
        day = first
        while True:
            days.add(day)
            if day == last:
                break
            day = (day + 1) % 7
    return days


def _parse_times(text: str) -> list[tuple[int, int]]:
    ranges = []
    for part in text.split(","):
        match = _TIME_RANGE.match(part.strip())
        if not match:
            raise _Unknown(part)
        h1, m1, h2, m2 = (int(g) for g in match.groups())
        if h1 > 24 or h2 > 48 or m1 > 59 or m2 > 59:
            raise _Unknown(part)
        start, end = h1 * 60 + m1, h2 * 60 + m2
        if end <= start:
            end += 24 * 60  # runs past midnight
        ranges.append((start, end))
    return ranges


def parse(spec: str) -> Schedule | None:
    """Turn an ``opening_hours`` value into a weekly schedule, or ``None`` if unknown."""
    spec = spec.strip()
    if not spec:
        return None
    if spec == "24/7":
        return {day: [(0, 24 * 60)] for day in range(7)}
    schedule: Schedule = {day: [] for day in range(7)}
    try:
        for rule in spec.split(";"):
            rule = rule.strip()
            if not rule:
                continue
            head, _, rest = rule.partition(" ")
            if head in ("PH", "SH") or head.startswith(("PH,", "SH,")):
                continue
            if _DAY_RANGE.match(head.split(",")[0]):
                days = _parse_days(head)
                body = rest.strip()
            else:
                days = set(range(7))
                body = rule
            if body in ("off", "closed"):
                times: list[tuple[int, int]] = []
            elif body in ("", "24/7"):
                # "Mo-Fr" alone or "Sa 24/7" both mean open all day.
                times = [(0, 24 * 60)]
            else:
                times = _parse_times(body)
            for day in days:
                schedule[day] = list(times)
    except _Unknown:
        return None
    return schedule


def open_intervals(spec: str | None, start: datetime, end: datetime) -> list[tuple[datetime, datetime]] | None:
    """Opening intervals overlapping ``[start, end]``, merged and sorted.

    Returns ``None`` when the hours are missing or can't be parsed.
    """
    if spec is None:
        return None
    schedule = parse(spec)
    if schedule is None:
        return None
    intervals: list[tuple[datetime, datetime]] = []
    day: date = start.date() - timedelta(days=1)  # yesterday's hours may run past midnight
    while day <= end.date():
        midnight = datetime.combine(day, datetime.min.time(), tzinfo=start.tzinfo)
        for open_min, close_min in schedule[day.weekday()]:
            intervals.append((midnight + timedelta(minutes=open_min), midnight + timedelta(minutes=close_min)))
        day += timedelta(days=1)
    intervals.sort()
    merged: list[tuple[datetime, datetime]] = []
    for opens, closes in intervals:
        if merged and opens <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], closes))
        else:
            merged.append((opens, closes))
    return [(o, c) for o, c in merged if c > start and o < end]
