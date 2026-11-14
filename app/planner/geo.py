"""Address lookup with Nominatim (OpenStreetMap).

Nominatim's usage policy: a real User-Agent, at most 1 request per second, and
cache results. Results are cached in the SQLite `cache` table.
"""

import math

import httpx

from app.config import Settings
from app.db import Database
from app.models import Location
from app.planner.http import RateLimiter, http_client, request_json

CACHE_NAMESPACE = "nominatim"

# One limiter for the whole process, shared by every request.
limiter = RateLimiter(min_interval_s=1.0)


async def geocode(
    query: str,
    *,
    settings: Settings,
    db: Database | None = None,
    client: httpx.AsyncClient | None = None,
    limit: int = 5,
) -> list[Location]:
    """Turn an address or place name into matching locations, best match first."""
    query = " ".join(query.split())
    if not query:
        return []
    key = f"{query.lower()}|{limit}"
    if db is not None:
        cached = db.cache_get(CACHE_NAMESPACE, key)
        if cached is not None:
            return [Location.model_validate(item) for item in cached]

    await limiter.wait()
    async with http_client(settings, client) as http:
        data = await request_json(
            http,
            "GET",
            f"{settings.nominatim_url.rstrip('/')}/search",
            service="Nominatim",
            params={"q": query, "format": "jsonv2", "limit": limit, "addressdetails": 0},
            headers={"User-Agent": settings.user_agent},
        )

    locations = [_to_location(item) for item in data if _has_coords(item)]
    if db is not None:
        db.cache_set(CACHE_NAMESPACE, key, [loc.model_dump() for loc in locations])
    return locations


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Straight-line distance in metres between two points."""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def _has_coords(item: dict) -> bool:
    return "lat" in item and "lon" in item


def _to_location(item: dict) -> Location:
    display = item.get("display_name") or ""
    name = item.get("name") or display.split(",")[0].strip() or "Unnamed place"
    return Location(
        name=name,
        lat=float(item["lat"]),
        lon=float(item["lon"]),
        address=display or None,
    )
