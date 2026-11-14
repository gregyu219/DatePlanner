"""Shared HTTP plumbing for the free map services: client setup, retries, caching keys."""

import asyncio
import hashlib
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx

from app.config import Settings

RETRY_STATUSES = {429, 500, 502, 503, 504}


class ServiceError(RuntimeError):
    """A public map service failed or returned something unusable."""


@asynccontextmanager
async def http_client(
    settings: Settings, client: httpx.AsyncClient | None = None
) -> AsyncIterator[httpx.AsyncClient]:
    """Use the given client, or open a short-lived one with our User-Agent and timeout."""
    if client is not None:
        yield client
        return
    async with httpx.AsyncClient(
        headers={"User-Agent": settings.user_agent},
        timeout=settings.http_timeout_seconds,
    ) as new_client:
        yield new_client


async def request_json(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    service: str,
    retries: int = 2,
    backoff_s: float = 1.0,
    **kwargs: Any,
) -> Any:
    """Send a request and return the JSON body, retrying on rate limits, 5xx and network errors."""
    last_error = ""
    for attempt in range(retries + 1):
        if attempt:
            await asyncio.sleep(backoff_s * attempt)
        try:
            response = await client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            continue
        if response.status_code in RETRY_STATUSES:
            last_error = f"HTTP {response.status_code}"
            continue
        if response.status_code >= 400:
            raise ServiceError(f"{service} returned HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError as exc:
            raise ServiceError(f"{service} returned invalid JSON") from exc
    raise ServiceError(f"{service} unavailable ({last_error})")


def cache_key(*parts: Any) -> str:
    """Stable, short cache key for any JSON-serialisable parts."""
    raw = json.dumps(parts, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


class RateLimiter:
    """Keeps at least `min_interval_s` between calls (Nominatim allows 1 request/second)."""

    def __init__(self, min_interval_s: float):
        self.min_interval_s = min_interval_s
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            delay = self._last + self.min_interval_s - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._last = time.monotonic()
