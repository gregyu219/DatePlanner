"""Routes for the opening chat: start a date, answer questions, show the chat page."""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from app import intake
from app.db import Database, get_db
from app.models import ChatMessage, DateStatus, Location
from app.planner.geo import geocode

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
router = APIRouter()


class ChatIn(BaseModel):
    text: str = Field(max_length=500)


class ChatView(BaseModel):
    id: int
    status: DateStatus
    step: intake.Step
    done: bool
    messages: list[ChatMessage]
    options: list[str]


def _load_state(db: Database, date_id: int) -> intake.IntakeState:
    if db.get_date(date_id) is None:
        raise HTTPException(status_code=404, detail="Date not found")
    saved = db.get_intake_state(date_id)
    return intake.IntakeState.model_validate(saved) if saved else intake.IntakeState()


def _view(db: Database, date_id: int, state: intake.IntakeState, options: list[str]) -> ChatView:
    record = db.get_date(date_id)
    assert record is not None
    return ChatView(
        id=date_id,
        status=record.status,
        step=state.step,
        done=state.done,
        messages=db.list_messages(date_id),
        options=[] if state.done else options,
    )


def _new_date(db: Database) -> int:
    record = db.create_date()
    state, reply = intake.start_chat()
    db.set_intake_state(record.id, state.model_dump(mode="json"))
    for text in reply.messages:
        db.add_message(record.id, "bot", text)
    return record.id


@router.post("/api/dates", response_model=ChatView, status_code=201)
def create_date(db: Database = Depends(get_db)) -> ChatView:
    date_id = _new_date(db)
    state = _load_state(db, date_id)
    return _view(db, date_id, state, intake.question(state).options)


@router.get("/api/dates/{date_id}/chat", response_model=ChatView)
def get_chat(date_id: int, db: Database = Depends(get_db)) -> ChatView:
    state = _load_state(db, date_id)
    return _view(db, date_id, state, intake.question(state).options)


@router.post("/api/dates/{date_id}/chat", response_model=ChatView)
async def post_chat(
    date_id: int, body: ChatIn, request: Request, db: Database = Depends(get_db)
) -> ChatView:
    state = _load_state(db, date_id)
    if state.done:
        raise HTTPException(status_code=409, detail="The opening chat for this date is finished")
    settings = request.app.state.settings

    async def lookup(query: str) -> list[Location]:
        return await geocode(query, settings=settings, db=db)

    text = body.text.strip()
    if text:
        db.add_message(date_id, "user", text)
    state, reply = await intake.answer(state, text, geocode=lookup)
    db.set_intake_state(date_id, state.model_dump(mode="json"))
    for message in reply.messages:
        db.add_message(date_id, "bot", message)

    if state.done:
        db.update_date(date_id, request=state.to_request(), status=DateStatus.researching)
        wake = getattr(request.app.state, "wake_heartbeat", None)
        if callable(wake):  # the planning heartbeat can register this to start at once
            wake()
    return _view(db, date_id, state, reply.options)


@router.post("/dates")
def new_date_page(db: Database = Depends(get_db)) -> RedirectResponse:
    date_id = _new_date(db)
    return RedirectResponse(f"/dates/{date_id}/chat", status_code=303)


@router.get("/dates/{date_id}/chat", response_class=HTMLResponse)
def chat_page(date_id: int, request: Request, db: Database = Depends(get_db)) -> HTMLResponse:
    if db.get_date(date_id) is None:
        raise HTTPException(status_code=404, detail="Date not found")
    return templates.TemplateResponse(request, "chat.html", {"date_id": date_id})
