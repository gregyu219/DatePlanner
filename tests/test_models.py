from datetime import datetime

import pytest
from pydantic import ValidationError

from app.models import DateRequest, Leg, Location, Plan, Slot, TransportMode


def test_slot_current_follows_index(make_place):
    slot = Slot(label="Dinner", alternatives=[make_place(1), make_place(2)])
    assert slot.current.name == "Place 1"
    slot.current_index = 1
    assert slot.current.name == "Place 2"


def test_slot_needs_at_least_one_alternative():
    with pytest.raises(ValidationError):
        Slot(label="Dinner", alternatives=[])


def test_date_request_rejects_bad_duration():
    with pytest.raises(ValidationError):
        DateRequest(
            start=Location(name="x", lat=0, lon=0),
            transport="car",
            start_time=datetime(2026, 11, 14, 18),
            duration_hours=0,
        )


def test_date_request_parses_transport_string(date_request):
    data = date_request.model_dump(mode="json")
    data["transport"] = "transit"
    assert DateRequest.model_validate(data).transport == TransportMode.transit


def test_plan_json_round_trip(make_place):
    plan = Plan(
        slots=[Slot(label="Walk", alternatives=[make_place(1, category="park")])],
        legs=[
            Leg(
                mode=TransportMode.transit,
                duration_s=1200,
                distance_m=3000,
                geometry=[(37.78, -122.40), (37.79, -122.41)],
                source="estimate",
                estimated=True,
            )
        ],
        arrivals=[datetime(2026, 11, 14, 17, 20)],
    )
    assert Plan.model_validate_json(plan.model_dump_json()) == plan
