import asyncio
from datetime import UTC, datetime

import httpx
import pytest
import respx

from app.models import Location, TransportMode
from app.planner import routing

A = Location(name="A", lat=37.788, lon=-122.4075)
B = Location(name="B", lat=37.795, lon=-122.40)
C = Location(name="C", lat=37.80, lon=-122.41)
DEPART = datetime(2026, 11, 14, 17, 0, tzinfo=UTC)

OSRM_ROUTE = {
    "code": "Ok",
    "routes": [
        {
            "duration": 420.5,
            "distance": 1900.0,
            "geometry": {"type": "LineString", "coordinates": [[-122.4075, 37.788], [-122.40, 37.795]]},
        }
    ],
}


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr("app.planner.http.asyncio.sleep", _no_sleep)


def run(coro):
    return asyncio.run(coro)


def _encode(points, precision):
    """Polyline encoder used to build test fixtures."""
    out, prev = [], (0, 0)
    for lat, lon in points:
        cur = (round(lat * 10**precision), round(lon * 10**precision))
        for delta in (cur[0] - prev[0], cur[1] - prev[1]):
            value = ~(delta << 1) if delta < 0 else delta << 1
            while value >= 0x20:
                out.append(chr((0x20 | (value & 0x1F)) + 63))
                value >>= 5
            out.append(chr(value + 63))
        prev = cur
    return "".join(out)


def test_decode_polyline_google_example():
    points = routing.decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@", 5)
    assert points == [(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)]


# --- OSRM -------------------------------------------------------------------


@respx.mock
def test_car_leg_uses_osrm_and_caches(settings, db):
    route = respx.get(url__startswith="https://router.project-osrm.org/route/v1/driving/").mock(
        return_value=httpx.Response(200, json=OSRM_ROUTE)
    )

    result = run(routing.leg(A, B, TransportMode.car, settings=settings, db=db))

    assert result.source == "osrm"
    assert result.estimated is False
    assert result.duration_s == 420.5
    assert result.distance_m == 1900.0
    assert result.geometry == [(37.788, -122.4075), (37.795, -122.40)]
    assert "-122.407500,37.788000;-122.400000,37.795000" in str(route.calls.last.request.url)
    assert route.calls.last.request.url.params["geometries"] == "geojson"

    assert run(routing.leg(A, B, TransportMode.car, settings=settings, db=db)) == result
    assert route.call_count == 1


@respx.mock
def test_walk_and_bike_use_foot_and_bike_servers(settings):
    foot = respx.get(url__startswith="https://routing.openstreetmap.de/routed-foot/route/v1/foot/").mock(
        return_value=httpx.Response(200, json=OSRM_ROUTE)
    )
    bike = respx.get(url__startswith="https://routing.openstreetmap.de/routed-bike/route/v1/bike/").mock(
        return_value=httpx.Response(200, json=OSRM_ROUTE)
    )
    assert run(routing.leg(A, B, TransportMode.walk, settings=settings)).source == "osrm"
    assert run(routing.leg(A, B, TransportMode.bike, settings=settings)).source == "osrm"
    assert foot.call_count == 1
    assert bike.call_count == 1


def test_custom_osrm_server_is_used_for_every_mode(settings):
    settings.osrm_url = "http://localhost:5000/"
    assert routing.osrm_base(settings, TransportMode.walk) == "http://localhost:5000"
    assert routing.osrm_base(settings, TransportMode.car) == "http://localhost:5000"


@respx.mock
def test_osrm_failure_falls_back_to_estimate(settings):
    respx.get(url__startswith="https://router.project-osrm.org/").mock(return_value=httpx.Response(503))
    result = run(routing.leg(A, B, TransportMode.car, settings=settings))
    assert result.source == "estimate"
    assert result.estimated is True


@respx.mock
def test_osrm_no_route_falls_back_to_estimate(settings):
    respx.get(url__startswith="https://router.project-osrm.org/").mock(
        return_value=httpx.Response(200, json={"code": "NoRoute", "routes": []})
    )
    assert run(routing.leg(A, B, TransportMode.car, settings=settings)).estimated is True


@respx.mock
def test_matrix_uses_one_osrm_table_call(settings):
    table = respx.get(url__startswith="https://router.project-osrm.org/table/v1/driving/").mock(
        return_value=httpx.Response(
            200,
            json={
                "code": "Ok",
                "durations": [[0, 100, 200], [110, 0, None], [210, 120, 0]],
                "distances": [[0, 1000, 2000], [1100, 0, None], [2100, 1200, 0]],
            },
        )
    )
    matrix = run(routing.travel_matrix([A, B, C], TransportMode.car, settings=settings))
    assert table.call_count == 1
    assert matrix.durations_s[0][1] == 100
    assert matrix.distances_m[2][0] == 2100
    assert matrix.estimated[0][1] is False
    # Missing entry is filled with an estimate.
    assert matrix.estimated[1][2] is True
    assert matrix.durations_s[1][2] > 0


# --- Transitous ---------------------------------------------------------------


def _itinerary(start, end, legs):
    return {"startTime": start, "endTime": end, "duration": 0, "legs": legs}


@respx.mock
def test_transit_leg_picks_earliest_arrival_and_counts_waiting(settings, db):
    shape = [(37.788, -122.4075), (37.791, -122.403), (37.795, -122.40)]
    legs = [
        {
            "mode": "BUS",
            "from": {"lat": 37.788, "lon": -122.4075},
            "to": {"lat": 37.795, "lon": -122.40},
            "distance": 1500.0,
            "legGeometry": {"points": _encode(shape, 7), "precision": 7},
        }
    ]
    data = {
        "itineraries": [
            _itinerary("2026-11-14T17:10:00Z", "2026-11-14T17:40:00Z", legs),
            _itinerary("2026-11-14T17:05:00Z", "2026-11-14T17:25:00Z", legs),
        ]
    }
    route = respx.get("https://api.transitous.org/api/v1/plan").mock(
        return_value=httpx.Response(200, json=data)
    )

    result = run(routing.leg(A, B, TransportMode.transit, settings=settings, db=db, depart_at=DEPART))

    assert result.source == "transitous"
    assert result.estimated is False
    assert result.duration_s == 25 * 60  # 5 min wait + 20 min ride
    assert result.distance_m == 1500.0
    assert result.geometry[0] == pytest.approx(shape[0])
    assert result.geometry[-1] == pytest.approx(shape[-1])
    params = route.calls.last.request.url.params
    assert params["fromPlace"] == "37.788000,-122.407500"
    assert params["time"] == "2026-11-14T17:00:00Z"

    run(routing.leg(A, B, TransportMode.transit, settings=settings, db=db, depart_at=DEPART))
    assert route.call_count == 1


def test_transit_polyline_with_wrong_precision_label_is_still_decoded():
    shape = [(37.788, -122.4075), (37.795, -122.40)]
    data = {
        "itineraries": [
            _itinerary(
                "2026-11-14T17:00:00Z",
                "2026-11-14T17:20:00Z",
                [
                    {
                        "from": {"lat": 37.788, "lon": -122.4075},
                        "to": {"lat": 37.795, "lon": -122.40},
                        "legGeometry": {"points": _encode(shape, 6), "precision": 7},
                    }
                ],
            )
        ]
    }
    result = routing.parse_transitous(data, DEPART)
    assert result.geometry[0] == pytest.approx(shape[0])
    assert result.distance_m > 500  # computed from the shape when the leg has no distance


@respx.mock
def test_transit_no_itinerary_falls_back_to_estimate(settings):
    respx.get("https://api.transitous.org/api/v1/plan").mock(
        return_value=httpx.Response(200, json={"itineraries": []})
    )
    result = run(routing.leg(A, C, TransportMode.transit, settings=settings, depart_at=DEPART))
    assert result.source == "estimate"
    assert result.estimated is True


@respx.mock
def test_transit_server_error_falls_back_to_estimate(settings):
    respx.get("https://api.transitous.org/api/v1/plan").mock(return_value=httpx.Response(500))
    result = run(routing.leg(A, C, TransportMode.transit, settings=settings, depart_at=DEPART))
    assert result.source == "estimate"


def test_transit_estimate_formula():
    result = routing.estimate_leg(A, C, TransportMode.transit)
    straight = routing.haversine_m(A.lat, A.lon, C.lat, C.lon)
    assert result.distance_m == pytest.approx(straight * 1.4, rel=1e-3)
    assert result.duration_s == pytest.approx(straight * 1.4 / (18 / 3.6) + 600, rel=1e-3)


def test_estimate_same_point_is_zero():
    assert routing.estimate_leg(A, A, TransportMode.transit).duration_s == 0
