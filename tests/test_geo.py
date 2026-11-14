import asyncio

import httpx
import pytest
import respx

from app.planner import geo
from app.planner.http import ServiceError

SEARCH_URL = "https://nominatim.openstreetmap.org/search"


@pytest.fixture(autouse=True)
def no_rate_limit(monkeypatch):
    monkeypatch.setattr(geo.limiter, "min_interval_s", 0.0)


NOMINATIM_RESULT = [
    {
        "lat": "37.7879",
        "lon": "-122.4075",
        "name": "Union Square",
        "display_name": "Union Square, San Francisco, California, United States",
    }
]


@respx.mock
def test_geocode_returns_locations_and_sends_user_agent(settings):
    route = respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=NOMINATIM_RESULT))

    results = asyncio.run(geo.geocode("Union Square, SF", settings=settings))

    assert len(results) == 1
    assert results[0].name == "Union Square"
    assert results[0].lat == pytest.approx(37.7879)
    assert results[0].address.startswith("Union Square, San Francisco")
    request = route.calls.last.request
    assert request.headers["User-Agent"] == settings.user_agent
    assert request.url.params["q"] == "Union Square, SF"


@respx.mock
def test_geocode_uses_cache(settings, db):
    route = respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=NOMINATIM_RESULT))

    first = asyncio.run(geo.geocode("Union Square", settings=settings, db=db))
    second = asyncio.run(geo.geocode("  union   square ", settings=settings, db=db))

    assert first == second
    assert route.call_count == 1


@respx.mock
def test_geocode_empty_query_makes_no_request(settings):
    route = respx.get(SEARCH_URL)
    assert asyncio.run(geo.geocode("   ", settings=settings)) == []
    assert route.call_count == 0


@respx.mock
def test_geocode_retries_then_raises(settings, monkeypatch):
    monkeypatch.setattr("app.planner.http.asyncio.sleep", _no_sleep)
    route = respx.get(SEARCH_URL).mock(return_value=httpx.Response(503))

    with pytest.raises(ServiceError):
        asyncio.run(geo.geocode("Nowhere", settings=settings))
    assert route.call_count == 3


async def _no_sleep(_seconds):
    return None
