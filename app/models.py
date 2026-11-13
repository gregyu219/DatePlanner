"""Shared data models used across intake, planning, routing and the API."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class TransportMode(StrEnum):
    car = "car"
    walk = "walk"
    bike = "bike"
    transit = "transit"


class DateStatus(StrEnum):
    """Lifecycle of a date. The heartbeat advances one stage per tick."""

    intake = "intake"
    researching = "researching"
    gathering = "gathering"
    selecting = "selecting"
    routing = "routing"
    validating = "validating"
    done = "done"
    failed = "failed"


class Location(BaseModel):
    name: str
    lat: float
    lon: float
    address: str | None = None


class DateRequest(BaseModel):
    """Answers collected by the opening chat."""

    start: Location
    transport: TransportMode
    start_time: datetime
    duration_hours: float = Field(gt=0, le=24)
    cuisine: str | None = None
    here_to_there: bool = False  # allow stops far apart
    ideas: str | None = None


class Place(BaseModel):
    """A candidate stop (restaurant, park, museum, ...)."""

    id: str  # e.g. "osm:node/123"
    name: str
    lat: float
    lon: float
    category: str
    cuisine: str | None = None
    address: str | None = None
    website: str | None = None
    opening_hours: str | None = None  # OSM opening_hours syntax
    hours_verified: bool = False  # False when hours were guessed, not from OSM
    visit_minutes: int = Field(default=60, gt=0)
    reason: str | None = None  # why it was picked


class Slot(BaseModel):
    """One stop in the plan, with ranked alternatives and swap history."""

    label: str  # e.g. "Dinner"
    alternatives: list[Place] = Field(min_length=1)
    current_index: int = 0
    history: list[int] = Field(default_factory=list)  # stack of previous indexes

    @property
    def current(self) -> Place:
        return self.alternatives[self.current_index]


class Leg(BaseModel):
    """Travel between two consecutive points of the plan."""

    mode: TransportMode
    duration_s: float = Field(ge=0)
    distance_m: float = Field(ge=0)
    geometry: list[tuple[float, float]] | None = None  # [(lat, lon), ...]
    source: str  # "osrm", "transitous", "estimate"
    estimated: bool = False


class Plan(BaseModel):
    """Stops in visit order. legs[i] leads to slots[i] (legs[0] starts at the start point)."""

    slots: list[Slot] = Field(default_factory=list)
    legs: list[Leg] = Field(default_factory=list)
    arrivals: list[datetime] = Field(default_factory=list)


class ChatMessage(BaseModel):
    role: str  # "bot" or "user"
    content: str
    created_at: datetime


class DateRecord(BaseModel):
    """A date as stored in the database."""

    id: int
    status: DateStatus
    request: DateRequest | None = None
    notes: str | None = None  # area research from Claude
    candidates: list[Place] = Field(default_factory=list)
    plan: Plan | None = None
    attempts: int = 0
    error: str | None = None
    created_at: datetime
    updated_at: datetime
