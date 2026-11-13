"""App settings, read from environment variables or a local `.env` file.

Every setting has a working default, so the app runs with no `.env` at all.
Claude is reached through the local `claude` CLI login, so there is no API key here.
"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Storage
    database_path: Path = Path("data/dateplanner.db")

    # Planning loop: seconds between heartbeat ticks.
    heartbeat_seconds: float = 5.0
    max_plan_attempts: int = 3

    # Claude (headless CLI, uses the local Claude Code login).
    claude_cli: str = "claude"
    claude_model: str | None = None
    claude_timeout_seconds: float = 300.0

    # Free map services (no Google). Nominatim requires a real User-Agent.
    user_agent: str = "DatePlanner/0.1 (personal use)"
    nominatim_url: str = "https://nominatim.openstreetmap.org"
    overpass_url: str = "https://overpass-api.de/api/interpreter"
    osrm_url: str = "https://router.project-osrm.org"
    transitous_url: str = "https://api.transitous.org"
    http_timeout_seconds: float = 20.0

    # Planning rules.
    max_transit_leg_minutes: int = 60


@lru_cache
def get_settings() -> Settings:
    return Settings()
