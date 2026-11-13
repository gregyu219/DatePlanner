from app.config import Settings


def test_health(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_index_page_renders_with_static_css(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "DatePlanner" in response.text
    assert client.get("/static/style.css").status_code == 200


def test_startup_creates_database(client, settings):
    assert settings.database_path.exists()


def test_settings_read_from_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("HEARTBEAT_SECONDS", "2.5")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "x.db"))
    settings = Settings(_env_file=None)
    assert settings.heartbeat_seconds == 2.5
    assert settings.database_path == tmp_path / "x.db"
    assert settings.max_transit_leg_minutes == 60
