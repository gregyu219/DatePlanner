"""Choose the visit order of the stops. Deterministic; the LLM never does this.

A date has only a handful of stops, so every order is tried. Each order is
walked from the start point: travel, wait for the place to open if needed,
stay ``visit_minutes``, move on. An order is rejected when

* a stop closes before the visit is over (from ``opening_hours``),
* a stop's arrival window (e.g. dinner 18:00-20:00) can't be met,
* a leg is longer than the per-leg limit (always 60 minutes by transit), or
* the date runs past its length.

Of the valid orders the one with the least total travel time wins (ties go to
the earliest finish). That keeps stops close together and naturally puts
places that close early first.
"""

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from itertools import permutations

from app.models import Place, TransportMode
from app.planner.hours import open_intervals

TRANSIT_MAX_LEG_S = 60 * 60
MAX_STOPS = 8  # 8! = 40,320 orders; beyond that brute force gets slow

# travel[i][j] = seconds from point i to point j. Point 0 is the start,
# point k + 1 is places[k]. None means there is no route.
Matrix = list[list[float | None]]


@dataclass
class Visit:
    place_index: int  # index into the ``places`` list
    arrive: datetime
    start: datetime  # after any wait for opening / arrival window
    leave: datetime
    travel_s: float  # leg that leads here
    hours_known: bool  # False when opening hours were missing or unreadable


@dataclass
class Schedule:
    order: list[int]
    visits: list[Visit]
    travel_s: float
    end: datetime


class OrderingError(Exception):
    """No order satisfies the limits. ``reasons`` counts why orders failed."""

    def __init__(self, reasons: Counter | None = None):
        self.reasons = reasons or Counter()
        super().__init__(str(self))

    def __str__(self) -> str:
        if not self.reasons:
            return "No valid order of stops."
        top = "; ".join(f"{reason} ({count} orders)" for reason, count in self.reasons.most_common(3))
        return f"No valid order of stops: {top}"


def max_leg_seconds(mode: TransportMode, limit_s: float | None = None) -> float | None:
    """Per-leg limit: the caller's limit, capped at one hour for transit."""
    if mode == TransportMode.transit:
        return TRANSIT_MAX_LEG_S if limit_s is None else min(limit_s, TRANSIT_MAX_LEG_S)
    return limit_s


def _window_on(day_start: datetime, window: tuple[time, time]) -> tuple[datetime, datetime]:
    """Place a clock-time window on the date's calendar, rolling to the next day if it is already over."""
    earliest = datetime.combine(day_start.date(), window[0], tzinfo=day_start.tzinfo)
    latest = datetime.combine(day_start.date(), window[1], tzinfo=day_start.tzinfo)
    if latest < earliest:
        latest += timedelta(days=1)
    if latest < day_start:
        earliest += timedelta(days=1)
        latest += timedelta(days=1)
    return earliest, latest


def check_order(
    order: list[int] | tuple[int, ...],
    places: list[Place],
    travel: Matrix,
    start_time: datetime,
    duration_hours: float,
    mode: TransportMode,
    *,
    max_leg_s: float | None = None,
    arrival_windows: dict[int, tuple[time, time]] | None = None,
) -> Schedule | str:
    """Walk one order. Returns the schedule, or a short reason why it fails."""
    date_end = start_time + timedelta(hours=duration_hours)
    leg_limit = max_leg_seconds(mode, max_leg_s)
    arrival_windows = arrival_windows or {}
    now = start_time
    point = 0
    visits: list[Visit] = []
    total = 0.0
    for index in order:
        place = places[index]
        leg = travel[point][index + 1]
        if leg is None:
            return f"no route to {place.name}"
        if leg_limit is not None and leg > leg_limit:
            return f"leg to {place.name} is over {round(leg_limit / 60)} min"
        arrive = now + timedelta(seconds=leg)
        stay = timedelta(minutes=place.visit_minutes)
        begin = arrive

        if index in arrival_windows:
            earliest, latest = _window_on(start_time, arrival_windows[index])
            begin = max(begin, earliest)
            if begin > latest:
                return f"misses the time window for {place.name}"

        intervals = open_intervals(place.opening_hours, begin, date_end + stay)
        if intervals is not None:
            # Earliest opening interval that fits the whole visit.
            fit = next((max(begin, o) for o, c in intervals if max(begin, o) + stay <= c), None)
            if fit is None:
                return f"{place.name} is closed"
            if index in arrival_windows and fit > latest:
                return f"misses the time window for {place.name}"
            begin = fit

        leave = begin + stay
        if leave > date_end:
            return "runs past the end of the date"
        visits.append(Visit(index, arrive, begin, leave, leg, intervals is not None))
        total += leg
        now = leave
        point = index + 1
    return Schedule(order=list(order), visits=visits, travel_s=total, end=now)


def best_order(
    places: list[Place],
    travel: Matrix,
    start_time: datetime,
    duration_hours: float,
    mode: TransportMode,
    *,
    max_leg_s: float | None = None,
    arrival_windows: dict[int, tuple[time, time]] | None = None,
) -> Schedule:
    """Try every order and return the valid one with the least travel time.

    Raises :class:`OrderingError` when no order works.
    """
    if not places:
        raise ValueError("no stops to order")
    if len(places) > MAX_STOPS:
        raise ValueError(f"at most {MAX_STOPS} stops can be ordered")
    size = len(places) + 1
    if len(travel) != size or any(len(row) != size for row in travel):
        raise ValueError(f"travel matrix must be {size}x{size}")

    best: Schedule | None = None
    reasons: Counter = Counter()
    for order in permutations(range(len(places))):
        result = check_order(
            order,
            places,
            travel,
            start_time,
            duration_hours,
            mode,
            max_leg_s=max_leg_s,
            arrival_windows=arrival_windows,
        )
        if isinstance(result, str):
            reasons[result] += 1
            continue
        if best is None or (result.travel_s, result.end) < (best.travel_s, best.end):
            best = result
    if best is None:
        raise OrderingError(reasons)
    return best
