"""Nearby places from OpenStreetMap via the Overpass API.

Returns real places (name, coordinates, opening_hours, website, cuisine) of the
requested categories around a point. Results are cached in SQLite.
"""

import math
import re

import httpx

from app.config import Settings
from app.db import Database
from app.models import Place, TransportMode
from app.planner.http import cache_key, http_client, request_json

CACHE_NAMESPACE = "overpass"

# Category name -> Overpass tag filters (any filter matches).
CATEGORIES: dict[str, list[str]] = {
    "restaurant": ['["amenity"="restaurant"]'],
    "cafe": ['["amenity"="cafe"]'],
    "bar": ['["amenity"~"^(bar|pub)$"]'],
    "dessert": ['["amenity"="ice_cream"]', '["shop"~"^(pastry|confectionery|chocolate)$"]'],
    "park": ['["leisure"~"^(park|garden)$"]'],
    "museum": ['["tourism"="museum"]'],
    "gallery": ['["tourism"="gallery"]'],
    "viewpoint": ['["tourism"="viewpoint"]'],
    "cinema": ['["amenity"="cinema"]'],
    "theatre": ['["amenity"="theatre"]'],
    "attraction": ['["tourism"="attraction"]'],
}

# Typical time spent at each kind of stop.
VISIT_MINUTES: dict[str, int] = {
    "restaurant": 90,
    "cafe": 45,
    "bar": 60,
    "dessert": 30,
    "park": 45,
    "museum": 90,
    "gallery": 60,
    "viewpoint": 20,
    "cinema": 150,
    "theatre": 150,
    "attraction": 60,
}

# Search radius per transport mode. Transit is capped by the 1-hour-per-leg rule.
RADIUS_M: dict[TransportMode, int] = {
    TransportMode.walk: 1500,
    TransportMode.bike: 4000,
    TransportMode.car: 10000,
    TransportMode.transit: 8000,
}
HERE_TO_THERE_FACTOR = 3


def search_radius_m(mode: TransportMode, here_to_there: bool = False) -> int:
    radius = RADIUS_M[mode]
    return radius * HERE_TO_THERE_FACTOR if here_to_there else radius


def _cuisine_filter(cuisine: str | None) -> str:
    """Case-insensitive regex filter on the cuisine tag, with unsafe characters removed."""
    if not cuisine:
        return ""
    words = [w for w in re.split(r"[^a-z0-9_]+", cuisine.lower()) if w]
    if not words:
        return ""
    return f'["cuisine"~"{"|".join(words)}",i]'


def build_query(
    categories: list[str], lat: float, lon: float, radius_m: int, cuisine: str | None = None
) -> str:
    """Overpass QL for named places of the given categories within radius_m of (lat, lon)."""
    unknown = [c for c in categories if c not in CATEGORIES]
    if unknown:
        raise ValueError(f"unknown place categories: {', '.join(unknown)}")
    around = f"(around:{int(radius_m)},{lat:.6f},{lon:.6f})"
    parts = []
    for category in categories:
        extra = _cuisine_filter(cuisine) if category == "restaurant" else ""
        for tag in CATEGORIES[category]:
            parts.append(f'  nwr{tag}["name"]{extra}{around};')
    body = "\n".join(parts)
    return f"[out:json][timeout:25];\n(\n{body}\n);\nout center tags;"


async def find_places(
    lat: float,
    lon: float,
    categories: list[str],
    *,
    radius_m: int,
    settings: Settings,
    cuisine: str | None = None,
    db: Database | None = None,
    client: httpx.AsyncClient | None = None,
    limit_per_category: int = 15,
) -> list[Place]:
    """Named places of each category, nearest first, at most `limit_per_category` each."""
    if not categories:
        return []
    query = build_query(categories, lat, lon, radius_m, cuisine)
    key = cache_key(query)
    data = db.cache_get(CACHE_NAMESPACE, key) if db is not None else None
    if data is None:
        async with http_client(settings, client) as http:
            data = await request_json(
                http, "POST", settings.overpass_url, service="Overpass", data={"data": query}
            )
        if db is not None:
            db.cache_set(CACHE_NAMESPACE, key, data)

    by_category: dict[str, list[tuple[float, Place]]] = {c: [] for c in categories}
    seen: set[str] = set()
    for element in data.get("elements", []):
        place = parse_element(element, categories)
        if place is None or place.id in seen:
            continue
        seen.add(place.id)
        by_category[place.category].append((_distance_m(lat, lon, place.lat, place.lon), place))

    results: list[Place] = []
    for category in categories:
        nearest = sorted(by_category[category], key=lambda item: item[0])
        results.extend(place for _, place in nearest[:limit_per_category])
    return results


def parse_element(element: dict, categories: list[str]) -> Place | None:
    """Turn an Overpass element into a Place, or None if it has no name or position."""
    tags = element.get("tags") or {}
    name = tags.get("name")
    if "lat" in element and "lon" in element:
        lat, lon = element["lat"], element["lon"]
    elif "center" in element:
        lat, lon = element["center"]["lat"], element["center"]["lon"]
    else:
        return None
    category = categorize(tags, categories)
    if not name or category is None:
        return None
    return Place(
        id=f"osm:{element['type']}/{element['id']}",
        name=name,
        lat=float(lat),
        lon=float(lon),
        category=category,
        cuisine=tags.get("cuisine"),
        address=_address(tags),
        website=tags.get("website") or tags.get("contact:website"),
        opening_hours=tags.get("opening_hours"),
        hours_verified=bool(tags.get("opening_hours")),
        visit_minutes=VISIT_MINUTES.get(category, 60),
    )


def categorize(tags: dict, categories: list[str]) -> str | None:
    """First requested category whose filters match these tags."""
    for category in categories:
        for tag_filter in CATEGORIES[category]:
            if _matches(tags, tag_filter):
                return category
    return None


_FILTER = re.compile(r'\["(?P<key>[^"]+)"(?P<op>=|~)"(?P<value>[^"]+)"\]')


def _matches(tags: dict, tag_filter: str) -> bool:
    m = _FILTER.fullmatch(tag_filter)
    assert m, tag_filter
    value = tags.get(m["key"])
    if value is None:
        return False
    if m["op"] == "=":
        return value == m["value"]
    return re.search(m["value"], value) is not None


def _address(tags: dict) -> str | None:
    street = " ".join(p for p in (tags.get("addr:housenumber"), tags.get("addr:street")) if p)
    parts = [p for p in (street, tags.get("addr:city")) if p]
    return ", ".join(parts) or None


def _distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))
