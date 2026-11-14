from datetime import datetime, time

import pytest

from app.models import TransportMode
from app.planner.ordering import OrderingError, best_order, check_order

START = datetime(2026, 11, 14, 17, 0)  # Saturday 17:00
MIN = 60


def line_matrix(n: int, step_s: float = 10 * MIN) -> list[list[float]]:
    """Points 0..n on a line, ``step_s`` apart: travel time grows with distance."""
    return [[abs(i - j) * step_s for j in range(n + 1)] for i in range(n + 1)]


def names(places, schedule):
    return [places[i].name for i in schedule.order]


def test_closest_stops_first_when_nothing_closes(make_place):
    places = [make_place(3), make_place(1), make_place(2)]
    # Matrix positions: start=0, places[0] at 3, places[1] at 1, places[2] at 2.
    pos = [0, 3, 1, 2]
    travel = [[abs(a - b) * 10 * MIN for b in pos] for a in pos]
    schedule = best_order(places, travel, START, 6, TransportMode.car)
    assert names(places, schedule) == ["Place 1", "Place 2", "Place 3"]
    assert schedule.travel_s == 30 * MIN
    assert schedule.visits[0].arrive == datetime(2026, 11, 14, 17, 10)
    assert schedule.visits[0].leave == datetime(2026, 11, 14, 18, 10)
    assert schedule.end == datetime(2026, 11, 14, 20, 30)


def test_place_closing_early_goes_first(make_place):
    places = [make_place(1), make_place(2), make_place(3, opening_hours="Sa 10:00-18:30")]
    schedule = best_order(places, line_matrix(3), START, 6, TransportMode.car)
    assert schedule.order[0] == 2
    assert schedule.visits[0].hours_known is True
    assert schedule.visits[1].hours_known is False


def test_waits_for_opening(make_place):
    places = [make_place(1, opening_hours="Sa 18:00-23:00")]
    schedule = best_order(places, line_matrix(1), START, 4, TransportMode.walk)
    visit = schedule.visits[0]
    assert visit.arrive == datetime(2026, 11, 14, 17, 10)
    assert visit.start == datetime(2026, 11, 14, 18, 0)
    assert schedule.end == datetime(2026, 11, 14, 19, 0)


def test_closed_place_has_no_valid_order(make_place):
    places = [make_place(1, opening_hours="Sa off; Mo-Fr 09:00-17:00")]
    with pytest.raises(OrderingError) as err:
        best_order(places, line_matrix(1), START, 4, TransportMode.car)
    assert "Place 1 is closed" in str(err.value)


def test_visit_must_fit_before_closing(make_place):
    places = [make_place(1, opening_hours="Sa 10:00-17:30")]  # 60 min visit won't fit
    with pytest.raises(OrderingError):
        best_order(places, line_matrix(1), START, 4, TransportMode.car)


def test_transit_leg_over_one_hour_rejected(make_place):
    places = [make_place(1), make_place(2)]
    travel = [
        [0, 30 * MIN, 61 * MIN],
        [30 * MIN, 0, 61 * MIN],
        [61 * MIN, 61 * MIN, 0],
    ]
    with pytest.raises(OrderingError) as err:
        best_order(places, travel, START, 8, TransportMode.transit)
    assert "over 60 min" in str(err.value)
    # The same legs are fine by car.
    assert best_order(places, travel, START, 8, TransportMode.car).order == [0, 1]


def test_transit_limit_picks_order_with_short_legs(make_place):
    places = [make_place(1), make_place(2)]
    # Start->2 is short but 2->1 is 70 min; start->1->2 is longer in total but legal.
    travel = [
        [0, 50 * MIN, 5 * MIN],
        [50 * MIN, 0, 55 * MIN],
        [5 * MIN, 70 * MIN, 0],
    ]
    assert best_order(places, travel, START, 8, TransportMode.car).order == [1, 0]
    assert best_order(places, travel, START, 8, TransportMode.transit).order == [0, 1]


def test_custom_leg_limit(make_place):
    places = [make_place(1)]
    with pytest.raises(OrderingError):
        best_order(places, line_matrix(1, 20 * MIN), START, 4, TransportMode.walk, max_leg_s=15 * MIN)


def test_date_length_limit(make_place):
    places = [make_place(1), make_place(2), make_place(3)]
    # 3 x 60 min visits + 30 min travel = 3.5 hours.
    assert best_order(places, line_matrix(3), START, 3.5, TransportMode.car)
    with pytest.raises(OrderingError) as err:
        best_order(places, line_matrix(3), START, 3, TransportMode.car)
    assert "runs past the end of the date" in str(err.value)


def test_arrival_window_for_dinner(make_place):
    dinner = make_place(1, category="restaurant", visit_minutes=90)
    places = [dinner, make_place(2, category="park"), make_place(3, category="bar")]
    windows = {0: (time(18, 0), time(20, 0))}
    schedule = best_order(places, line_matrix(3), START, 6, TransportMode.car, arrival_windows=windows)
    dinner_visit = next(v for v in schedule.visits if v.place_index == 0)
    assert time(18, 0) <= dinner_visit.start.time() <= time(20, 0)


def test_arrival_window_that_cannot_be_met(make_place):
    places = [make_place(1)]
    windows = {0: (time(17, 0), time(17, 5))}  # the 10 min drive arrives too late
    with pytest.raises(OrderingError) as err:
        best_order(places, line_matrix(1), START, 4, TransportMode.car, arrival_windows=windows)
    assert "time window" in str(err.value)


def test_arrival_window_after_midnight_rolls_to_next_day(make_place):
    places = [make_place(1, visit_minutes=30)]
    late = datetime(2026, 11, 14, 23, 0)
    windows = {0: (time(0, 0), time(1, 0))}
    schedule = best_order(places, line_matrix(1), late, 3, TransportMode.car, arrival_windows=windows)
    assert schedule.visits[0].start == datetime(2026, 11, 15, 0, 0)


def test_no_route_rejected(make_place):
    places = [make_place(1), make_place(2)]
    travel = [[0, 600, None], [600, 0, None], [None, None, 0]]
    with pytest.raises(OrderingError) as err:
        best_order(places, travel, START, 6, TransportMode.car)
    assert "no route to Place 2" in str(err.value)


def test_check_order_keeps_given_order(make_place):
    places = [make_place(1), make_place(2)]
    schedule = check_order([1, 0], places, line_matrix(2), START, 6, TransportMode.car)
    assert schedule.order == [1, 0]
    assert schedule.travel_s == 30 * MIN


def test_six_stops_brute_force(make_place):
    places = [make_place(n, visit_minutes=30) for n in (6, 2, 4, 1, 5, 3)]
    pos = [0, 6, 2, 4, 1, 5, 3]
    travel = [[abs(a - b) * 5 * MIN for b in pos] for a in pos]
    schedule = best_order(places, travel, START, 6, TransportMode.walk)
    assert names(places, schedule) == [f"Place {n}" for n in range(1, 7)]


def test_bad_inputs(make_place):
    with pytest.raises(ValueError):
        best_order([], [[0]], START, 4, TransportMode.car)
    with pytest.raises(ValueError):
        best_order([make_place(1)], [[0]], START, 4, TransportMode.car)
    with pytest.raises(ValueError):
        best_order([make_place(n) for n in range(9)], line_matrix(9), START, 24, TransportMode.car)
