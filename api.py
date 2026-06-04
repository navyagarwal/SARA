from __future__ import annotations

from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel

from agent import process_ticket_with_agent
from db import create_ticket, get_ticket_state, initialize_database, list_recent_ticket_states
from seed import reset_demo_data

app = FastAPI(title="SARA Support Agent")


class TicketCreate(BaseModel):
    student_email: str
    subject: str
    body: str


@app.on_event("startup")
def startup() -> None:
    initialize_database()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/tickets")
def post_ticket(payload: TicketCreate, background_tasks: BackgroundTasks) -> dict[str, int | str]:
    ticket_id = create_ticket(payload.student_email, payload.subject, payload.body)
    background_tasks.add_task(process_ticket_with_agent, ticket_id)
    return {"ticket_id": ticket_id, "status": "received"}


@app.get("/tickets/{ticket_id}")
def get_ticket(ticket_id: int) -> dict:
    state = get_ticket_state(ticket_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return state


@app.get("/tickets")
def get_tickets() -> dict[str, list[dict]]:
    return {"tickets": list_recent_ticket_states()}


@app.post("/seed/reset")
def reset_seed_data() -> dict[str, str]:
    reset_demo_data()
    return {"status": "reset", "message": "Demo data, tickets, logs, actions, and outbox restored to seed state"}
