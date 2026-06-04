from __future__ import annotations

import os
import time
from html import escape
from pathlib import Path

import requests
import streamlit as st
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env", override=True)

API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000").rstrip("/")


def initialize_state() -> None:
    if "student_email" not in st.session_state:
        st.session_state.student_email = ""
        st.session_state.subject = ""
        st.session_state.body = ""
    if "ticket_id" not in st.session_state:
        st.session_state.ticket_id = None


def fetch_ticket(ticket_id: int) -> dict | None:
    response = requests.get(f"{API_BASE_URL}/tickets/{ticket_id}", timeout=10)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()


def send_ticket() -> None:
    payload = {
        "student_email": st.session_state.student_email,
        "subject": st.session_state.subject,
        "body": st.session_state.body,
    }
    response = requests.post(f"{API_BASE_URL}/tickets", json=payload, timeout=10)
    response.raise_for_status()
    st.session_state.ticket_id = response.json()["ticket_id"]


initialize_state()

st.set_page_config(page_title="SARA", page_icon="S", layout="wide")
st.title("SARA [Support And Resolution Agent]")

st.markdown(
    """
    <style>
      section[data-testid="stSidebar"] { display: none; }
      .terminal {
        background: #09090b;
        border: 1px solid #27272a;
        border-radius: 8px;
        color: #d4d4d8;
        font-family: Consolas, "SFMono-Regular", Menlo, Monaco, monospace;
        font-size: 0.86rem;
        line-height: 1.55;
        max-height: 440px;
        overflow: auto;
        padding: 1rem;
        white-space: pre-wrap;
      }
      .terminal .cursor {
        animation: blink 1s steps(2, start) infinite;
        color: #22c55e;
      }
      @keyframes blink { to { visibility: hidden; } }
    </style>
    """,
    unsafe_allow_html=True,
)

state = None
if st.session_state.ticket_id:
    state = fetch_ticket(st.session_state.ticket_id)

left, right = st.columns(2)

with left:
    st.subheader("Incoming Student Email")
    if state:
        ticket = state["ticket"]
        st.text_input("From", value=ticket["student_email"], disabled=True)
        st.text_input("Incoming subject", value=ticket["subject"], disabled=True)
        st.text_area("Incoming body", value=ticket["body"], disabled=True, height=220)
        st.text_input("Status", value=ticket["status"], disabled=True)
        if ticket.get("classification"):
            st.text_input("Classification", value=ticket["classification"], disabled=True)
    else:
        st.text_input("From", key="student_email")
        st.text_input("Subject", key="subject")
        st.text_area("Body", key="body", height=220)
        st.button("Send", type="primary", use_container_width=True, on_click=send_ticket)

with right:
    st.subheader("Reply Email Sent to Student")
    outgoing = state.get("outgoing_email") if state else None
    if outgoing:
        st.text_input("To", value=outgoing["to_email"], disabled=True)
        st.text_input("Reply subject", value=outgoing["subject"], disabled=True)
        st.text_area("Reply body", value=outgoing["body"], disabled=True, height=220)
        st.text_input("Reply status", value=outgoing["status"], disabled=True)
    else:
        st.text_input("To", value="", disabled=True)
        st.text_input("Reply subject", value="", disabled=True)
        st.text_area("Reply body", value="", disabled=True, height=220)
        st.text_input("Reply status", value="pending" if state else "", disabled=True)

st.subheader("Internal Agent Audit Logs")
if state:
    ticket = state["ticket"]
    if ticket.get("root_cause"):
        st.info(ticket["root_cause"])
    if state.get("human_ticket"):
        st.warning(f"Human review: {state['human_ticket']['reason']}")
    terminal_lines = [
        log["metadata"].get(
            "terminal_output",
            f"ts='{log['timestamp']}' level='{log['level']}' actor='{log['actor']}' event='{log['event']}' message='{log['message']}'",
        )
        for log in state["audit_logs"]
    ]
    terminal_text = escape("\n".join(terminal_lines))
    if ticket["status"] in {"received", "processing"}:
        terminal_text = f"{terminal_text}\n$ waiting_for_agent_output <span class=\"cursor\">█</span>"
    st.markdown(
        f"<div class=\"terminal\">{terminal_text}</div>",
        unsafe_allow_html=True,
    )
    if ticket["status"] in {"received", "processing"}:
        time.sleep(1)
        st.rerun()
else:
    st.markdown(
        "<div class=\"terminal\">$ waiting_for_ticket</div>",
        unsafe_allow_html=True,
    )
