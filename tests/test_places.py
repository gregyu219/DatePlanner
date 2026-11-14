import asyncio

import httpx
import pytest
import respx

from app.models import TransportMode
from app.planner import places

OVERPASS_URL = "https://overpass-api.de/api/interpreter"

ELEMENTS = {
    "elements": [
        {
            "type": "node",
            "id": 1,
            "lat": 37.790,
            "lon": -122.4075,
            "tags": {
                "amenity": "restaurant",
                "name": "Far Sushi",
                "cuisine": "japanese;sushi",
                "opening_hours": "Mo-Su 17:00-22:00",
                "website": "https://example.com",
                "addr:housenumber": "12",
                "addr:street": "Post St",
            },
        },
        {
            "type": "node",
            "id": 2,
            "lat": 37.7881,
            "lon": -122.4075,
            "tags": {"amenity": "restaurant", "name": "Near Ramen", "cuisine": "ramen"},
        },
        {
            "type": "way",
            "id": 3,
            "center": {"lat": 37.789, "lon": -122.41},
            "tags": {"leisure": "park", "name": "Little Park"},
        },
        {"type": "node", "id": 4, "lat": 37.79, "lon": -122.4, "tags": {"amenity": "restaurant"}},
        {"type": "node", "id": 2, "lat": 37.7881, "lon": -122.4075, "tags": {"amenity": "restaurant", "name": "Near Ramen"}},
    ]
}


def test_search_radius_depends_on_mode():
    assert places.search_radius_m(TransportMode.walk) < places.search_radius_m(TransportMode.bike)
    assert places.search_radius_m(TransportMode.bike) < places.search_radius_m(TransportMode.car)
    assert places.search_radius_m(TransportMode.walk, here_to_there=True) == 4500


def test_build_query_filters_cuisine_on_restaurants_only():
    query = places.build_query(["restaurant", "park"], 37.788, -122.4075, 1500, cuisine="Japanese")
    assert '["amenity"="restaurant"]["name"]["cuisine"~"japanese",i](around:1500,37.788000,-122.407500);' in query
    assert '["leisure"~"^(park|garden)$"]["name"](around:1500' in query
    assert "out center tags;" in query


def test_build_query_strips_unsafe_cuisine_characters():
    query = places.build_query(["restaurant"], 0, 0, 100, cuisine='thai"];out;')
    assert '"thai|out",i' in query


def test_build_query_rejects_unknown_category():
    with pytest.raises(ValueError):
        places.build_query(["spaceport"], 0, 0, 100)


@respx.mock
def test_find_places_parses_sorts_and_caches(settings, db):
    route = respx.post(OVERPASS_URL).mock(return_value=httpx.Response(200, json=ELEMENTS))

    def run():
        return asyncio.run(
            places.find_places(
                37.788, -122.4075, ["restaurant", "park"], radius_m=1500, settings=settings, db=db
            )
        )

    found = run()
    assert [p.name for p in found] == ["Near Ramen", "Far Sushi", "Little Park"]
    sushi = found[1]
    assert sushi.id == "osm:node/1"
    assert sushi.opening_hours == "Mo-Su 17:00-22:00"
    assert sushi.hours_verified is True
    assert sushi.address == "12 Post St"
    assert sushi.visit_minutes == 90
    assert found[0].hours_verified is False
    assert found[2].id == "osm:way/3"
    assert found[2].category == "park"
    assert "data=" in route.calls.last.request.content.decode()

    assert run() == found
    assert route.call_count == 1


@respx.mock
def test_find_places_limit_per_category(settings):
    respx.post(OVERPASS_URL).mock(return_value=httpx.Response(200, json=ELEMENTS))
    found = asyncio.run(
        places.find_places(
            37.788, -122.4075, ["restaurant"], radius_m=1500, settings=settings, limit_per_category=1
        )
    )
    assert [p.name for p in found] == ["Near Ramen"]
