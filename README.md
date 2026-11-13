# DatePlanner

A web app that plans a date: a short chat collects where you start, how you travel,
how long you have and what you'd like to eat, then Claude researches the area and the
app builds an ordered route shown on a map. Uses free map services only
(OpenStreetMap, Nominatim, Overpass, OSRM, Transitous), with no Google account.

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # optional; every setting has a default
```

Claude runs through your local `claude` CLI login, so no API key is needed.

## Run

```sh
.venv/bin/uvicorn app.main:app --reload
```

Then open http://127.0.0.1:8000. Data is stored in SQLite at `data/dateplanner.db`.

## Test

```sh
.venv/bin/pytest
```

## Layout

```
app/
  main.py       FastAPI app (create_app), pages and API routes
  config.py     settings from env / .env
  db.py         SQLite storage: dates, chat messages, service cache
  models.py     shared Pydantic models (DateRequest, Place, Slot, Leg, Plan, ...)
  planner/      map services, routing, ordering, Claude helper
  templates/    HTML pages
  static/       CSS and JS
tests/          pytest suite (external HTTP mocked with respx)
```
