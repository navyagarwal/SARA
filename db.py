from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from dotenv import load_dotenv

from logging_config import get_logger, terminal_line

ROOT_DIR = Path(__file__).resolve().parent
load_dotenv(ROOT_DIR / ".env", override=True)
logger = get_logger("sara.audit")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def database_path() -> Path:
    database_url = os.getenv("DATABASE_URL", "sqlite:///data/support_agent.db")
    if not database_url.startswith("sqlite:///"):
        raise ValueError("Only sqlite:/// DATABASE_URL values are supported")
    raw_path = database_url.removeprefix("sqlite:///")
    path = Path(raw_path)
    if not path.is_absolute():
        path = ROOT_DIR / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(database_path(), timeout=30, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
    except sqlite3.OperationalError as error:
        logger.warning(
            "sqlite_wal_setup_skipped",
            message="SQLite WAL setup skipped because the database is currently locked.",
            metadata={"error": str(error)},
        )
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def initialize_database() -> None:
    with connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS students (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              email TEXT UNIQUE NOT NULL,
              name TEXT NOT NULL,
              support_tier TEXT NOT NULL,
              created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS courses (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              course_code TEXT UNIQUE NOT NULL,
              title TEXT NOT NULL,
              certificate_required_progress INTEGER NOT NULL,
              created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS enrollments (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              student_id INTEGER NOT NULL,
              course_id INTEGER NOT NULL,
              status TEXT NOT NULL,
              enrolled_at TEXT NOT NULL,
              FOREIGN KEY(student_id) REFERENCES students(id),
              FOREIGN KEY(course_id) REFERENCES courses(id)
            );

            CREATE TABLE IF NOT EXISTS course_progress (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              student_id INTEGER NOT NULL,
              course_id INTEGER NOT NULL,
              progress_percent INTEGER NOT NULL,
              last_completed_module INTEGER,
              updated_at TEXT NOT NULL,
              FOREIGN KEY(student_id) REFERENCES students(id),
              FOREIGN KEY(course_id) REFERENCES courses(id)
            );

            CREATE TABLE IF NOT EXISTS payments (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              student_id INTEGER NOT NULL,
              course_id INTEGER NOT NULL,
              amount INTEGER NOT NULL,
              currency TEXT NOT NULL,
              status TEXT NOT NULL,
              paid_at TEXT,
              FOREIGN KEY(student_id) REFERENCES students(id),
              FOREIGN KEY(course_id) REFERENCES courses(id)
            );

            CREATE TABLE IF NOT EXISTS certificates (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              student_id INTEGER NOT NULL,
              course_id INTEGER NOT NULL,
              status TEXT NOT NULL,
              retry_count INTEGER DEFAULT 0,
              last_error TEXT,
              updated_at TEXT NOT NULL,
              FOREIGN KEY(student_id) REFERENCES students(id),
              FOREIGN KEY(course_id) REFERENCES courses(id)
            );

            CREATE TABLE IF NOT EXISTS modules (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              course_id INTEGER NOT NULL,
              module_number INTEGER NOT NULL,
              title TEXT NOT NULL,
              prerequisite_module_number INTEGER,
              FOREIGN KEY(course_id) REFERENCES courses(id)
            );

            CREATE TABLE IF NOT EXISTS module_access (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              student_id INTEGER NOT NULL,
              course_id INTEGER NOT NULL,
              module_number INTEGER NOT NULL,
              is_locked INTEGER NOT NULL,
              reason TEXT,
              updated_at TEXT NOT NULL,
              FOREIGN KEY(student_id) REFERENCES students(id),
              FOREIGN KEY(course_id) REFERENCES courses(id)
            );

            CREATE TABLE IF NOT EXISTS refunds (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              student_id INTEGER NOT NULL,
              course_id INTEGER NOT NULL,
              status TEXT NOT NULL,
              reason TEXT,
              created_at TEXT NOT NULL,
              FOREIGN KEY(student_id) REFERENCES students(id),
              FOREIGN KEY(course_id) REFERENCES courses(id)
            );

            CREATE TABLE IF NOT EXISTS tickets (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              student_email TEXT NOT NULL,
              subject TEXT NOT NULL,
              body TEXT NOT NULL,
              status TEXT NOT NULL,
              classification TEXT,
              root_cause TEXT,
              confidence REAL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS audit_logs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ticket_id INTEGER NOT NULL,
              timestamp TEXT NOT NULL,
              level TEXT NOT NULL,
              actor TEXT NOT NULL,
              event TEXT NOT NULL,
              message TEXT NOT NULL,
              metadata_json TEXT,
              FOREIGN KEY(ticket_id) REFERENCES tickets(id)
            );

            CREATE TABLE IF NOT EXISTS human_tickets (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ticket_id INTEGER NOT NULL,
              reason TEXT NOT NULL,
              status TEXT NOT NULL,
              created_at TEXT NOT NULL,
              FOREIGN KEY(ticket_id) REFERENCES tickets(id)
            );

            CREATE TABLE IF NOT EXISTS email_outbox (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ticket_id INTEGER NOT NULL,
              to_email TEXT NOT NULL,
              subject TEXT NOT NULL,
              body TEXT NOT NULL,
              status TEXT NOT NULL,
              created_at TEXT NOT NULL,
              sent_at TEXT,
              FOREIGN KEY(ticket_id) REFERENCES tickets(id)
            );

            CREATE TABLE IF NOT EXISTS agent_actions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ticket_id INTEGER NOT NULL,
              action_type TEXT NOT NULL,
              status TEXT NOT NULL,
              metadata_json TEXT,
              created_at TEXT NOT NULL,
              FOREIGN KEY(ticket_id) REFERENCES tickets(id)
            );
            """
        )


def reset_database() -> None:
    path = database_path()
    if path.exists():
        path.unlink()
    initialize_database()


def log_event(
    ticket_id: int,
    event: str,
    message: str,
    *,
    level: str = "INFO",
    actor: str = "agent",
    metadata: dict[str, Any] | None = None,
) -> None:
    timestamp = utc_now()
    metadata = metadata or {}
    terminal_output = terminal_line(
        timestamp=timestamp,
        level=level,
        actor=actor,
        event=event,
        message=message,
        metadata=metadata,
    )
    log_method_name = "warning" if level.upper() == "WARN" else level.lower()
    log_method = getattr(logger, log_method_name, logger.info)
    log_method(
        event,
        ticket_id=ticket_id,
        actor=actor,
        message=message,
        metadata=metadata,
    )
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO audit_logs (
              ticket_id, timestamp, level, actor, event, message, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ticket_id,
                timestamp,
                level,
                actor,
                event,
                message,
                json.dumps(
                    {
                        "schema_version": "audit.v1",
                        "terminal_output": terminal_output,
                        **metadata,
                    }
                ),
            ),
        )


def create_ticket(student_email: str, subject: str, body: str) -> int:
    now = utc_now()
    with connect() as connection:
        cursor = connection.execute(
            """
            INSERT INTO tickets (
              student_email, subject, body, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (student_email, subject, body, "received", now, now),
        )
        ticket_id = int(cursor.lastrowid)
    log_event(
        ticket_id,
        "ticket_received",
        "Ticket received and queued for agent processing.",
        actor="system",
        metadata={"student_email": student_email, "subject": subject},
    )
    return ticket_id


def get_ticket(ticket_id: int) -> dict[str, Any] | None:
    with connect() as connection:
        return row_to_dict(
            connection.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        )


def update_ticket(ticket_id: int, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = utc_now()
    assignments = ", ".join(f"{field} = ?" for field in fields)
    values = list(fields.values()) + [ticket_id]
    with connect() as connection:
        connection.execute(f"UPDATE tickets SET {assignments} WHERE id = ?", values)


def get_ticket_state(ticket_id: int) -> dict[str, Any] | None:
    with connect() as connection:
        ticket = row_to_dict(
            connection.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        )
        if ticket is None:
            return None
        audit_logs = rows_to_dicts(
            connection.execute(
                "SELECT * FROM audit_logs WHERE ticket_id = ? ORDER BY id ASC",
                (ticket_id,),
            ).fetchall()
        )
        for log in audit_logs:
            log["metadata"] = json.loads(log.pop("metadata_json") or "{}")
        outgoing_email = row_to_dict(
            connection.execute(
                "SELECT to_email, subject, body, status, created_at, sent_at "
                "FROM email_outbox WHERE ticket_id = ? ORDER BY id DESC LIMIT 1",
                (ticket_id,),
            ).fetchone()
        )
        human_ticket = row_to_dict(
            connection.execute(
                "SELECT id, reason, status, created_at FROM human_tickets "
                "WHERE ticket_id = ? ORDER BY id DESC LIMIT 1",
                (ticket_id,),
            ).fetchone()
        )
    return {
        "ticket": ticket,
        "audit_logs": audit_logs,
        "outgoing_email": outgoing_email,
        "human_ticket": human_ticket,
    }


def list_recent_ticket_states(limit: int = 20) -> list[dict[str, Any]]:
    with connect() as connection:
        tickets = rows_to_dicts(
            connection.execute(
                "SELECT * FROM tickets ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        )
    return tickets
