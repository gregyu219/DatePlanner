import pytest

from app.db import Database
from app.models import DateStatus, Plan, Slot


def test_init_creates_parent_folder(tmp_path):
    database = Database(tmp_path / "nested" / "dir" / "app.db")
    database.init()
    database.init()  # running twice is safe
    assert database.path.exists()


def test_create_and_get_date(db):
    record = db.create_date()
    assert record.status == DateStatus.intake
    assert record.request is None
    assert record.plan is None
    assert db.get_date(record.id) == record


def test_get_missing_date_returns_none(db):
    assert db.get_date(999) is None


def test_update_date_round_trips_all_fields(db, date_request, make_place):
    record = db.create_date()
    places = [make_place(1), make_place(2, category="park")]
    plan = Plan(slots=[Slot(label="Dinner", alternatives=places, current_index=1, history=[0])])

    updated = db.update_date(
        record.id,
        status=DateStatus.selecting,
        request=date_request,
        notes="Night market on Saturday",
        candidates=places,
        plan=plan,
        attempts=2,
        error="retrying",
    )

    assert updated.status == DateStatus.selecting
    assert updated.request == date_request
    assert updated.notes == "Night market on Saturday"
    assert updated.candidates == places
    assert updated.plan == plan
    assert updated.plan.slots[0].current.name == "Place 2"
    assert updated.attempts == 2
    assert updated.error == "retrying"
    assert updated.updated_at >= record.updated_at


def test_update_only_touches_given_fields(db):
    record = db.create_date()
    db.update_date(record.id, notes="keep me", error="boom")
    updated = db.update_date(record.id, error=None)
    assert updated.notes == "keep me"
    assert updated.error is None


def test_update_missing_date_raises(db):
    with pytest.raises(KeyError):
        db.update_date(42, notes="x")


def test_list_dates_filters_by_status(db):
    a = db.create_date()
    b = db.create_date()
    db.update_date(b.id, status=DateStatus.done)

    assert [d.id for d in db.list_dates()] == [a.id, b.id]
    assert [d.id for d in db.list_dates([DateStatus.done])] == [b.id]
    assert [d.id for d in db.list_dates([DateStatus.intake, DateStatus.failed])] == [a.id]


def test_messages_are_kept_in_order_per_date(db):
    a = db.create_date()
    b = db.create_date()
    db.add_message(a.id, "bot", "Where do you start?")
    db.add_message(b.id, "bot", "other date")
    db.add_message(a.id, "user", "Union Square")

    messages = db.list_messages(a.id)
    assert [(m.role, m.content) for m in messages] == [
        ("bot", "Where do you start?"),
        ("user", "Union Square"),
    ]


def test_intake_state_get_and_set(db):
    record = db.create_date()
    assert db.get_intake_state(record.id) is None
    db.set_intake_state(record.id, {"step": "start"})
    db.set_intake_state(record.id, {"step": "transport"})
    assert db.get_intake_state(record.id) == {"step": "transport"}


def test_cache_get_and_set(db):
    assert db.cache_get("nominatim", "union square") is None
    db.cache_set("nominatim", "union square", [{"lat": "37.7", "lon": "-122.4"}])
    assert db.cache_get("nominatim", "union square") == [{"lat": "37.7", "lon": "-122.4"}]
    db.cache_set("nominatim", "union square", {"replaced": True})
    assert db.cache_get("nominatim", "union square") == {"replaced": True}
    assert db.cache_get("osrm", "union square") is None
