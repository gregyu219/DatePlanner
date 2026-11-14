"""SQLite storage (stdlib sqlite3).

Each heartbeat stage saves its output on the `dates` row, so a restart picks up
where it stopped. The `cache` table stores responses from public map services
(Nominatim asks clients to cache).
"""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import Request

from app.models import ChatMessage, DateRecord, DateRequest, DateStatus, Place, Plan

SCHEMA = """
CREATE TABLE IF NOT EXISTS dates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL,
    request_json TEXT,
    notes TEXT,
    candidates_json TEXT,
    plan_json TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date_id INTEGER NOT NULL REFERENCES dates(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_date_id ON messages(date_id);
CREATE TABLE IF NOT EXISTS intake (
    date_id INTEGER PRIMARY KEY REFERENCES dates(id) ON DELETE CASCADE,
    state_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cache (
    namespace TEXT NOT NULL,
    key TEXT NOT NULL,
    value_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (namespace, key)
);
"""

_UNSET: Any = object()


def _now() -> datetime:
    return datetime.now(UTC)


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            with conn:  # commit on success, roll back on error
                yield conn
        finally:
            conn.close()

    def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    # --- dates -------------------------------------------------------------

    def create_date(self, status: DateStatus = DateStatus.intake) -> DateRecord:
        now = _now().isoformat()
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO dates (status, created_at, updated_at) VALUES (?, ?, ?)",
                (status.value, now, now),
            )
            date_id = cur.lastrowid
        record = self.get_date(date_id)
        assert record is not None
        return record

    def get_date(self, date_id: int) -> DateRecord | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM dates WHERE id = ?", (date_id,)).fetchone()
        return _row_to_date(row) if row else None

    def list_dates(self, statuses: list[DateStatus] | None = None) -> list[DateRecord]:
        sql = "SELECT * FROM dates"
        params: list[str] = []
        if statuses:
            sql += f" WHERE status IN ({', '.join('?' for _ in statuses)})"
            params = [s.value for s in statuses]
        with self.connect() as conn:
            rows = conn.execute(sql + " ORDER BY id", params).fetchall()
        return [_row_to_date(r) for r in rows]

    def update_date(
        self,
        date_id: int,
        *,
        status: DateStatus = _UNSET,
        request: DateRequest | None = _UNSET,
        notes: str | None = _UNSET,
        candidates: list[Place] = _UNSET,
        plan: Plan | None = _UNSET,
        attempts: int = _UNSET,
        error: str | None = _UNSET,
    ) -> DateRecord:
        """Update only the fields that are passed. Pass None to clear a field."""
        fields: dict[str, Any] = {}
        if status is not _UNSET:
            fields["status"] = status.value
        if request is not _UNSET:
            fields["request_json"] = request.model_dump_json() if request else None
        if notes is not _UNSET:
            fields["notes"] = notes
        if candidates is not _UNSET:
            fields["candidates_json"] = json.dumps([c.model_dump(mode="json") for c in candidates])
        if plan is not _UNSET:
            fields["plan_json"] = plan.model_dump_json() if plan else None
        if attempts is not _UNSET:
            fields["attempts"] = attempts
        if error is not _UNSET:
            fields["error"] = error
        fields["updated_at"] = _now().isoformat()

        assignments = ", ".join(f"{name} = ?" for name in fields)
        with self.connect() as conn:
            cur = conn.execute(
                f"UPDATE dates SET {assignments} WHERE id = ?", [*fields.values(), date_id]
            )
            if cur.rowcount == 0:
                raise KeyError(f"date {date_id} not found")
        record = self.get_date(date_id)
        assert record is not None
        return record

    # --- chat messages -----------------------------------------------------

    def add_message(self, date_id: int, role: str, content: str) -> ChatMessage:
        created_at = _now()
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO messages (date_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                (date_id, role, content, created_at.isoformat()),
            )
        return ChatMessage(role=role, content=content, created_at=created_at)

    def list_messages(self, date_id: int) -> list[ChatMessage]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT role, content, created_at FROM messages WHERE date_id = ? ORDER BY id",
                (date_id,),
            ).fetchall()
        return [ChatMessage(**dict(r)) for r in rows]

    # --- opening chat progress --------------------------------------------

    def get_intake_state(self, date_id: int) -> dict | None:
        """The opening chat's saved progress (app.intake.IntakeState as a dict)."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT state_json FROM intake WHERE date_id = ?", (date_id,)
            ).fetchone()
        return json.loads(row["state_json"]) if row else None

    def set_intake_state(self, date_id: int, state: dict) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO intake (date_id, state_json, updated_at) VALUES (?, ?, ?)",
                (date_id, json.dumps(state), _now().isoformat()),
            )

    # --- cache for external services --------------------------------------

    def cache_get(self, namespace: str, key: str) -> Any | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT value_json FROM cache WHERE namespace = ? AND key = ?", (namespace, key)
            ).fetchone()
        return json.loads(row["value_json"]) if row else None

    def cache_set(self, namespace: str, key: str, value: Any) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO cache (namespace, key, value_json, created_at)"
                " VALUES (?, ?, ?, ?)",
                (namespace, key, json.dumps(value), _now().isoformat()),
            )


def get_db(request: Request) -> Database:
    """FastAPI dependency: the app's Database (set up in app.main.create_app)."""
    return request.app.state.db


def _row_to_date(row: sqlite3.Row) -> DateRecord:
    return DateRecord(
        id=row["id"],
        status=DateStatus(row["status"]),
        request=DateRequest.model_validate_json(row["request_json"]) if row["request_json"] else None,
        notes=row["notes"],
        candidates=[Place.model_validate(c) for c in json.loads(row["candidates_json"])]
        if row["candidates_json"]
        else [],
        plan=Plan.model_validate_json(row["plan_json"]) if row["plan_json"] else None,
        attempts=row["attempts"],
        error=row["error"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )
