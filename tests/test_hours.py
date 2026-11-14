from datetime import datetime

import pytest

from app.planner.hours import open_intervals, parse

SAT = datetime(2026, 11, 14, 0, 0)  # a Saturday
assert SAT.weekday() == 5


def at(hour: int, minute: int = 0, day: int = 14) -> datetime:
    return datetime(2026, 11, day, hour, minute)


def test_24_7():
    assert parse("24/7") == {d: [(0, 1440)] for d in range(7)}


def test_day_ranges_and_multiple_times():
    schedule = parse("Mo-Fr 11:00-14:00, 17:00-22:00; Sa 10:00-23:00; Su off")
    assert schedule[0] == [(660, 840), (1020, 1320)]
    assert schedule[4] == [(660, 840), (1020, 1320)]
    assert schedule[5] == [(600, 1380)]
    assert schedule[6] == []


def test_wrapping_day_range_and_day_list():
    schedule = parse("Fr-Mo 18:00-23:00; We,Th 12:00-13:00")
    assert {d for d, t in schedule.items() if t == [(1080, 1380)]} == {4, 5, 6, 0}
    assert schedule[2] == schedule[3] == [(720, 780)]
    assert schedule[1] == []


def test_later_rule_overrides_earlier():
    schedule = parse("Mo-Su 09:00-17:00; Su 12:00-15:00")
    assert schedule[6] == [(720, 900)]
    assert schedule[5] == [(540, 1020)]


def test_times_without_days_and_holidays_ignored():
    schedule = parse("10:00-20:00; PH off")
    assert all(schedule[d] == [(600, 1200)] for d in range(7))


def test_past_midnight():
    assert parse("Sa 20:00-02:00")[5] == [(1200, 1560)]


@pytest.mark.parametrize("spec", ["", "Jan-Mar 10:00-12:00", "sunrise-sunset", "Mo 10:00+", "Mo 25:00-26:00"])
def test_unknown_formats(spec):
    assert parse(spec) is None


def test_open_intervals_none_when_missing_or_unknown():
    assert open_intervals(None, at(17), at(22)) is None
    assert open_intervals("by appointment", at(17), at(22)) is None


def test_open_intervals_on_a_day():
    assert open_intervals("Sa 10:00-14:00, 17:00-22:00", at(15), at(23)) == [(at(17), at(22))]


def test_open_intervals_include_yesterdays_late_hours():
    # Friday 20:00-02:00 means open until 2am on Saturday.
    assert open_intervals("Fr 20:00-02:00", at(0, 30), at(3)) == [(at(20, day=13), at(2))]


def test_open_intervals_merge_across_midnight():
    result = open_intervals("Sa 18:00-24:00; Su 00:00-03:00", at(19), at(2, day=15))
    assert result == [(at(18), at(3, day=15))]
