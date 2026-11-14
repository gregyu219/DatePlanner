"""Travel times between two points, behind one interface: `leg(a, b, mode)`.

- car / walk / bike: OSRM (route geometry as GeoJSON).
- transit (bus): Transitous (MOTIS 2 `/plan`).
- If a provider fails or has no route, fall back to a straight-line estimate
  marked `estimated=True` (source "estimate") so the UI can label it.

`travel_matrix` gives all pairwise durations for stop ordering (one OSRM
`/table` call, or pairwise Transitous calls).
"""

from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import urlparse

import httpx

from app.config import Settings
from app.db import Database
from app.models import Leg, TransportMode
from app.planner.geo import haversine_m
from app.planner.http import ServiceError, cache_key, http_client, request_json

TRANSITOUS_PLAN_PATH = "/api/v1/plan"

# The public OSRM demo server only routes cars (it ignores the profile). When it
# is configured, walk and bike go to the FOSSGIS OSRM servers, which run
# separate foot and bike graphs.
OSRM_DEMO_HOST = "router.project-osrm.org"
FOSSGIS_OSRM = {
    TransportMode.walk: "https://routing.openstreetmap.de/routed-foot",
    TransportMode.bike: "https://routing.openstreetmap.de/routed-bike",
}
OSRM_PROFILE = {TransportMode.car: "driving", TransportMode.walk: "foot", TransportMode.bike: "bike"}

# Fallback estimates: road distance ≈ straight line × detour factor.
ESTIMATE = {
    # mode: (speed km/h, detour factor, fixed wait seconds)
    TransportMode.transit: (18.0, 1.4, 600.0),
    TransportMode.car: (30.0, 1.3, 0.0),
    TransportMode.bike: (15.0, 1.3, 0.0),
    TransportMode.walk: (4.8, 1.3, 0.0),
}


class Point(Protocol):
    lat: float
    lon: float


class TravelMatrix:
    """durations_s[i][j] / distances_m[i][j] from point i to point j."""

    def __init__(self, durations_s: list[list[float]], distances_m: list[list[float]],
                 estimated: list[list[bool]]):
        self.durations_s = durations_s
        self.distances_m = distances_m
        self.estimated = estimated


# --- public interface -------------------------------------------------------


async def leg(
    a: Point,
    b: Point,
    mode: TransportMode,
    *,
    settings: Settings,
    db: Database | None = None,
    client: httpx.AsyncClient | None = None,
    depart_at: datetime | None = None,
) -> Leg:
    """Travel from a to b. Never raises for provider trouble: falls back to an estimate."""
    try:
        if mode == TransportMode.transit:
            result = await _transitous_leg(a, b, settings, db, client, depart_at)
        else:
            result = await _osrm_leg(a, b, mode, settings, db, client)
    except ServiceError:
        result = None
    return result or estimate_leg(a, b, mode)


async def travel_matrix(
    points: list[Point],
    mode: TransportMode,
    *,
    settings: Settings,
    db: Database | None = None,
    client: httpx.AsyncClient | None = None,
    depart_at: datetime | None = None,
) -> TravelMatrix:
    n = len(points)
    durations = [[0.0] * n for _ in range(n)]
    distances = [[0.0] * n for _ in range(n)]
    estimated = [[False] * n for _ in range(n)]
    if n < 2:
        return TravelMatrix(durations, distances, estimated)

    table = None
    if mode != TransportMode.transit:
        try:
            table = await _osrm_table(points, mode, settings, client)
        except ServiceError:
            table = None

    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            if table is not None and table[0][i][j] is not None:
                durations[i][j] = float(table[0][i][j])
                distances[i][j] = float(table[1][i][j] or 0.0)
                continue
            if mode == TransportMode.transit:
                result = await leg(points[i], points[j], mode, settings=settings, db=db,
                                   client=client, depart_at=depart_at)
            else:
                result = estimate_leg(points[i], points[j], mode)
            durations[i][j] = result.duration_s
            distances[i][j] = result.distance_m
            estimated[i][j] = result.estimated
    return TravelMatrix(durations, distances, estimated)


def estimate_leg(a: Point, b: Point, mode: TransportMode) -> Leg:
    """Straight-line guess, used when no provider has a route."""
    speed_kmh, detour, wait_s = ESTIMATE[mode]
    distance = haversine_m(a.lat, a.lon, b.lat, b.lon) * detour
    duration = distance / (speed_kmh / 3.6) + (wait_s if distance > 0 else 0.0)
    return Leg(
        mode=mode,
        duration_s=round(duration, 1),
        distance_m=round(distance, 1),
        geometry=[(a.lat, a.lon), (b.lat, b.lon)],
        source="estimate",
        estimated=True,
    )


# --- OSRM -------------------------------------------------------------------


def osrm_base(settings: Settings, mode: TransportMode) -> str:
    base = settings.osrm_url.rstrip("/")
    if mode in FOSSGIS_OSRM and urlparse(base).hostname == OSRM_DEMO_HOST:
        return FOSSGIS_OSRM[mode]
    return base


def _coords(points: list[Point]) -> str:
    return ";".join(f"{p.lon:.6f},{p.lat:.6f}" for p in points)


async def _osrm_leg(a, b, mode, settings, db, client) -> Leg | None:
    url = f"{osrm_base(settings, mode)}/route/v1/{OSRM_PROFILE[mode]}/{_coords([a, b])}"
    key = cache_key(url)
    if db is not None:
        cached = db.cache_get("osrm", key)
        if cached is not None:
            return Leg.model_validate(cached)

    async with http_client(settings, client) as http:
        data = await request_json(
            http, "GET", url, service="OSRM",
            params={"overview": "full", "geometries": "geojson"},
        )
    routes = data.get("routes") or []
    if data.get("code") != "Ok" or not routes:
        return None
    route = routes[0]
    coordinates = (route.get("geometry") or {}).get("coordinates") or []
    result = Leg(
        mode=mode,
        duration_s=float(route["duration"]),
        distance_m=float(route["distance"]),
        geometry=[(lat, lon) for lon, lat in coordinates] or None,
        source="osrm",
    )
    if db is not None:
        db.cache_set("osrm", key, result.model_dump(mode="json"))
    return result


async def _osrm_table(points, mode, settings, client):
    url = f"{osrm_base(settings, mode)}/table/v1/{OSRM_PROFILE[mode]}/{_coords(points)}"
    async with http_client(settings, client) as http:
        data = await request_json(
            http, "GET", url, service="OSRM", params={"annotations": "duration,distance"}
        )
    if data.get("code") != "Ok" or "durations" not in data:
        return None
    n = len(points)
    distances = data.get("distances") or [[None] * n for _ in range(n)]
    return data["durations"], distances


# --- Transitous (MOTIS 2) ----------------------------------------------------


async def _transitous_leg(a, b, settings, db, client, depart_at) -> Leg | None:
    depart_at = _as_utc(depart_at or datetime.now(UTC))
    # Cache per 15-minute departure window.
    window = depart_at.replace(minute=depart_at.minute // 15 * 15, second=0, microsecond=0)
    key = cache_key(round(a.lat, 5), round(a.lon, 5), round(b.lat, 5), round(b.lon, 5), window)
    if db is not None:
        cached = db.cache_get("transitous", key)
        if cached is not None:
            return Leg.model_validate(cached)

    async with http_client(settings, client) as http:
        data = await request_json(
            http,
            "GET",
            f"{settings.transitous_url.rstrip('/')}{TRANSITOUS_PLAN_PATH}",
            service="Transitous",
            params={
                "fromPlace": f"{a.lat:.6f},{a.lon:.6f}",
                "toPlace": f"{b.lat:.6f},{b.lon:.6f}",
                "time": depart_at.isoformat().replace("+00:00", "Z"),
                "arriveBy": "false",
            },
        )
    result = parse_transitous(data, depart_at)
    if result is not None and db is not None:
        db.cache_set("transitous", key, result.model_dump(mode="json"))
    return result


def parse_transitous(data: dict, depart_at: datetime) -> Leg | None:
    """Pick the itinerary that arrives first; its time includes waiting before departure."""
    best = None
    for itinerary in data.get("itineraries") or []:
        try:
            end = _parse_time(itinerary["endTime"])
        except (KeyError, ValueError):
            continue
        if best is None or end < best[0]:
            best = (end, itinerary)
    if best is None:
        return None
    end, itinerary = best
    duration = max((end - depart_at).total_seconds(), float(itinerary.get("duration") or 0))

    geometry: list[tuple[float, float]] = []
    distance = 0.0
    for item in itinerary.get("legs") or []:
        points = _leg_points(item)
        geometry.extend(points)
        if item.get("distance") is not None:
            distance += float(item["distance"])
        else:
            distance += sum(
                haversine_m(*points[k], *points[k + 1]) for k in range(len(points) - 1)
            )
    return Leg(
        mode=TransportMode.transit,
        duration_s=duration,
        distance_m=round(distance, 1),
        geometry=geometry or None,
        source="transitous",
    )


def _leg_points(item: dict) -> list[tuple[float, float]]:
    encoded = (item.get("legGeometry") or {}).get("points")
    start = item.get("from") or {}
    fallback = [
        (p["lat"], p["lon"]) for p in (start, item.get("to") or {}) if "lat" in p and "lon" in p
    ]
    if not encoded:
        return fallback
    precisions = [item["legGeometry"].get("precision") or 7, 7, 6, 5]
    for precision in dict.fromkeys(precisions):
        points = decode_polyline(encoded, precision)
        # Guard against guessing the precision wrong: the line must start near the leg start.
        if not points or "lat" not in start:
            return points
        if haversine_m(*points[0], start["lat"], start["lon"]) < 500:
            return points
    return fallback


def decode_polyline(encoded: str, precision: int = 5) -> list[tuple[float, float]]:
    """Decode a Google-style encoded polyline into [(lat, lon), ...]."""
    factor = 10**precision
    points, index, lat, lon = [], 0, 0, 0
    while index < len(encoded):
        deltas = []
        for _ in range(2):
            result, shift = 0, 0
            while True:
                if index >= len(encoded):
                    return points
                byte = ord(encoded[index]) - 63
                index += 1
                result |= (byte & 0x1F) << shift
                shift += 5
                if byte < 0x20:
                    break
            deltas.append(~(result >> 1) if result & 1 else result >> 1)
        lat += deltas[0]
        lon += deltas[1]
        points.append((lat / factor, lon / factor))
    return points


def _parse_time(value: str) -> datetime:
    return _as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


def _as_utc(value: datetime) -> datetime:
    """Naive datetimes are treated as local time, then converted to UTC."""
    if value.tzinfo is None:
        value = value.astimezone()
    return value.astimezone(UTC)
