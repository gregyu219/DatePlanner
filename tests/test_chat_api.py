import httpx
import pytest
import respx

from app.models import DateStatus, TransportMode
from app.planner import geo

SEARCH_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_RESULT = [
    {
        "lat": "37.7879",
        "lon": "-122.4075",
        "name": "Union Square",
        "display_name": "Union Square, San Francisco, California, United States",
    }
]


@pytest.fixture(autouse=True)
def no_rate_limit(monkeypatch):
    monkeypatch.setattr(geo.limiter, "min_interval_s", 0.0)


def send(client, date_id, text):
    response = client.post(f"/api/dates/{date_id}/chat", json={"text": text})
    assert response.status_code == 200, response.text
    return response.json()


def test_create_date_starts_chat(client):
    response = client.post("/api/dates")
    assert response.status_code == 201
    view = response.json()
    assert view["status"] == "intake"
    assert view["step"] == "start"
    assert view["done"] is False
    assert [m["role"] for m in view["messages"]] == ["bot", "bot"]
    assert "Where will you start" in view["messages"][-1]["content"]


@respx.mock
def test_full_chat_saves_request_and_starts_planning(client):
    route = respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=NOMINATIM_RESULT))
    woken = []
    client.app.state.wake_heartbeat = lambda: woken.append(True)
    date_id = client.post("/api/dates").json()["id"]

    view = send(client, date_id, "Union Square, SF")
    assert view["step"] == "confirm_start"
    assert view["options"] == ["Yes", "No"]
    assert "Union Square, San Francisco" in view["messages"][-1]["content"]

    for text in ["Yes", "Car", "2099-06-01 18:00", "3 hours", "Italian", "Close together"]:
        view = send(client, date_id, text)
    assert view["step"] == "ideas"
    view = send(client, date_id, "Skip")

    assert view["done"] is True
    assert view["status"] == "researching"
    assert view["options"] == []
    assert woken == [True]
    assert route.call_count == 1

    record = client.app.state.db.get_date(date_id)
    assert record.status == DateStatus.researching
    assert record.request.start.name == "Union Square"
    assert record.request.transport == TransportMode.car
    assert record.request.duration_hours == 3
    assert record.request.cuisine == "italian"

    # More answers after the chat is finished are refused.
    response = client.post(f"/api/dates/{date_id}/chat", json={"text": "hello"})
    assert response.status_code == 409


def test_chat_progress_is_saved_between_requests(client):
    date_id = client.post("/api/dates").json()["id"]
    client.app.state.db.set_intake_state(date_id, {"step": "duration"})
    view = client.get(f"/api/dates/{date_id}/chat").json()
    assert view["step"] == "duration"
    assert "2 hours" in view["options"]

    view = send(client, date_id, "back")
    assert view["step"] == "start_time"
    assert client.get(f"/api/dates/{date_id}/chat").json()["step"] == "start_time"


def test_messages_are_recorded_in_order(client):
    date_id = client.post("/api/dates").json()["id"]
    client.app.state.db.set_intake_state(date_id, {"step": "transport"})
    view = send(client, date_id, "teleport")
    roles = [m["role"] for m in view["messages"]]
    assert roles == ["bot", "bot", "user", "bot"]
    assert view["messages"][2]["content"] == "teleport"
    assert view["step"] == "transport"


def test_unknown_date_returns_404(client):
    assert client.get("/api/dates/999/chat").status_code == 404
    assert client.post("/api/dates/999/chat", json={"text": "hi"}).status_code == 404
    assert client.get("/dates/999/chat").status_code == 404


def test_new_date_page_redirects_to_chat(client):
    response = client.post("/dates", follow_redirects=False)
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/dates/") and location.endswith("/chat")

    page = client.get(location)
    assert page.status_code == 200
    assert "chat.js" in page.text
    assert client.get("/static/chat.js").status_code == 200


def test_index_links_to_new_date(client):
    assert 'action="/dates"' in client.get("/").text
