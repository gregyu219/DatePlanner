from collections.abc import Iterator
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.main import create_app
from app.models import DateRequest, Location, Place, TransportMode


@pytest.fixture
def settings(tmp_path) -> Settings:
    # _env_file=None keeps a developer's local .env out of the tests.
    return Settings(_env_file=None, database_path=tmp_path / "test.db", heartbeat_seconds=0.01)


@pytest.fixture
def db(settings: Settings) -> Database:
    database = Database(settings.database_path)
    database.init()
    return database


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def date_request() -> DateRequest:
    return DateRequest(
        start=Location(name="Union Square", lat=37.788, lon=-122.4075),
        transport=TransportMode.walk,
        start_time=datetime(2026, 11, 14, 17, 0),
        duration_hours=4,
        cuisine="japanese",
    )


@pytest.fixture
def make_place():
    def _make(n: int = 1, **overrides) -> Place:
        data = {
            "id": f"osm:node/{n}",
            "name": f"Place {n}",
            "lat": 37.78 + n / 1000,
            "lon": -122.40,
            "category": "restaurant",
        }
        data.update(overrides)
        return Place(**data)

    return _make
