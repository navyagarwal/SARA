from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from db import ROOT_DIR, connect, log_event, rows_to_dicts, utc_now

DOMAIN_DOCS_DIR = ROOT_DIR / "domain_docs"
DOMAIN_DOCS = ["overview.md", "certificates.md", "refunds_billing.md", "course_access.md"]

EXPOSED_TABLES = {
    "students": "Stores learner identity and support tier.",
    "courses": "Stores course metadata and certificate requirements.",
    "enrollments": "Stores each learner's course enrollment status.",
    "course_progress": "Stores learner completion progress by course.",
    "payments": "Stores learner payment records and payment status.",
    "certificates": "Stores certificate generation state.",
    "modules": "Stores course module structure and prerequisites.",
    "module_access": "Stores module lock state for each learner.",
    "refunds": "Stores refund request state.",
}

BLOCKED_SQL = re.compile(r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|PRAGMA|REPLACE|VACUUM)\b", re.I)
LIMIT_RE = re.compile(r"\bLIMIT\b", re.I)


def summarize_output(result: Any) -> str:
    if isinstance(result, dict):
        if "rows" in result:
            return f"{len(result['rows'])} row(s) returned"
        if "columns" in result:
            return f"{len(result['columns'])} column(s) returned"
        if "content" in result:
            return f"{result.get('doc_name', 'document')} read"
    return "Tool completed"


def list_domain_docs(ticket_id: int) -> dict[str, Any]:
    result = {"documents": DOMAIN_DOCS}
    log_event(
        ticket_id,
        "tool_result",
        "Available domain documents returned.",
        metadata={"tool_name": "list_domain_docs", "output_summary": summarize_output(result)},
    )
    return result


def read_domain_doc(ticket_id: int, doc_name: str) -> dict[str, Any]:
    if doc_name not in DOMAIN_DOCS:
        result = {"error": f"Unknown domain document: {doc_name}"}
    else:
        result = {
            "doc_name": doc_name,
            "content": (DOMAIN_DOCS_DIR / doc_name).read_text(encoding="utf-8"),
        }
    log_event(
        ticket_id,
        "domain_doc_read",
        f"Agent read {doc_name}.",
        metadata={"doc_name": doc_name, "output_summary": summarize_output(result)},
    )
    return result


def list_database_tables(ticket_id: int) -> dict[str, Any]:
    result = {"tables": list(EXPOSED_TABLES)}
    log_event(
        ticket_id,
        "tool_result",
        "Queryable database tables returned.",
        metadata={"tool_name": "list_database_tables", "output_summary": summarize_output(result)},
    )
    return result


def describe_table(ticket_id: int, table_name: str) -> dict[str, Any]:
    if table_name not in EXPOSED_TABLES:
        result = {"error": f"Table is not available to the agent: {table_name}"}
    else:
        with connect() as connection:
            columns = rows_to_dicts(
                connection.execute(f"PRAGMA table_info({table_name})").fetchall()
            )
        result = {
            "table_name": table_name,
            "columns": [
                {
                    "name": column["name"],
                    "type": column["type"],
                    "primary_key": bool(column["pk"]),
                }
                for column in columns
            ],
            "description": EXPOSED_TABLES[table_name],
        }
    log_event(
        ticket_id,
        "table_described",
        f"Agent inspected schema for {table_name}.",
        metadata={"table_name": table_name, "output_summary": summarize_output(result)},
    )
    return result


def _reject_query(ticket_id: int, sql: str, reason: str) -> dict[str, Any]:
    log_event(
        ticket_id,
        "database_query_rejected",
        "Database query rejected by guardrails.",
        level="WARN",
        metadata={"sql": sql, "reason": reason},
    )
    return {"error": reason}


def query_database(ticket_id: int, sql: str, params: list[Any] | None = None) -> dict[str, Any]:
    params = params or []
    clean_sql = sql.strip()
    if not clean_sql.lower().startswith("select"):
        return _reject_query(ticket_id, sql, "Only SELECT queries are allowed.")
    if BLOCKED_SQL.search(clean_sql):
        return _reject_query(ticket_id, sql, "Query contains a blocked SQL keyword.")
    if ";" in clean_sql.rstrip(";"):
        return _reject_query(ticket_id, sql, "Multiple SQL statements are not allowed.")
    if not LIMIT_RE.search(clean_sql):
        clean_sql = f"{clean_sql.rstrip(';')} LIMIT 20"

    started = time.perf_counter()
    try:
        with connect() as connection:
            rows = connection.execute(clean_sql, params).fetchmany(20)
    except sqlite3.Error as error:
        return _reject_query(ticket_id, sql, str(error))

    duration_ms = round((time.perf_counter() - started) * 1000, 2)
    result = {"sql": clean_sql, "params": params, "rows": rows_to_dicts(rows)}
    log_event(
        ticket_id,
        "database_query_executed",
        "Agent executed a guarded read-only database query.",
        metadata={
            "sql": clean_sql,
            "params": params,
            "duration_ms": duration_ms,
            "rows_returned": len(result["rows"]),
        },
    )
    return result


def create_agent_action(
    ticket_id: int,
    action_type: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metadata = metadata or {}
    allowed_actions = {"retry_certificate_generation", "unlock_module_access", "send_reply_email"}
    if action_type not in allowed_actions:
        result = {"status": "rejected", "error": f"Unsupported action_type: {action_type}"}
    else:
        result = {"status": "completed", "action_type": action_type}
        with connect() as connection:
            connection.execute(
                """
                INSERT INTO agent_actions (
                  ticket_id, action_type, status, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (ticket_id, action_type, "completed", json.dumps(metadata), utc_now()),
            )
            if action_type == "retry_certificate_generation":
                connection.execute(
                    """
                    UPDATE certificates
                    SET status = 'retry_queued',
                        retry_count = retry_count + 1,
                        updated_at = ?
                    WHERE student_id = (
                      SELECT id FROM students WHERE email = ?
                    )
                    AND course_id = (
                      SELECT id FROM courses WHERE course_code = ?
                    )
                    AND status = 'failed'
                    """,
                    (
                        utc_now(),
                        metadata.get("student_email"),
                        metadata.get("course_code", "ai-productivity-101"),
                    ),
                )
            elif action_type == "unlock_module_access":
                connection.execute(
                    """
                    UPDATE module_access
                    SET is_locked = 0, reason = NULL, updated_at = ?
                    WHERE student_id = (
                      SELECT id FROM students WHERE email = ?
                    )
                    AND course_id = (
                      SELECT id FROM courses WHERE course_code = ?
                    )
                    AND module_number = ?
                    """,
                    (
                        utc_now(),
                        metadata.get("student_email"),
                        metadata.get("course_code", "ai-productivity-101"),
                        metadata.get("module_number"),
                    ),
                )
    log_event(
        ticket_id,
        "agent_action_created",
        f"Agent action recorded: {action_type}.",
        metadata={"action_type": action_type, "input": metadata, "result": result},
    )
    return result


def create_human_ticket(
    ticket_id: int,
    reason: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    with connect() as connection:
        human_ticket_id = connection.execute(
            """
            INSERT INTO human_tickets (ticket_id, reason, status, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (ticket_id, reason, "open", utc_now()),
        ).lastrowid
    result = {"human_ticket_id": human_ticket_id, "status": "open", "metadata": metadata or {}}
    log_event(
        ticket_id,
        "human_ticket_created",
        "Human review ticket created.",
        metadata={"reason": reason, "details": metadata or {}},
    )
    return result


def write_reply_email(ticket_id: int, to_email: str, subject: str, body: str) -> dict[str, Any]:
    now = utc_now()
    with connect() as connection:
        email_id = connection.execute(
            """
            INSERT INTO email_outbox (
              ticket_id, to_email, subject, body, status, created_at, sent_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (ticket_id, to_email, subject, body, "sent", now, now),
        ).lastrowid
    result = {"email_id": email_id, "to_email": to_email, "subject": subject, "status": "sent"}
    log_event(
        ticket_id,
        "reply_email_sent",
        "Simulated reply email sent.",
        metadata={"to_email": to_email, "subject": subject},
    )
    return result


def execute_tool(ticket_id: int, name: str, args: dict[str, Any]) -> dict[str, Any]:
    log_event(
        ticket_id,
        "tool_call",
        f"Agent called {name}.",
        metadata={"tool_name": name, "input": args},
    )
    functions = {
        "list_domain_docs": lambda: list_domain_docs(ticket_id),
        "read_domain_doc": lambda: read_domain_doc(ticket_id, args["doc_name"]),
        "list_database_tables": lambda: list_database_tables(ticket_id),
        "describe_table": lambda: describe_table(ticket_id, args["table_name"]),
        "query_database": lambda: query_database(ticket_id, args["sql"], args.get("params", [])),
        "create_agent_action": lambda: create_agent_action(
            ticket_id, args["action_type"], args.get("metadata", {})
        ),
        "create_human_ticket": lambda: create_human_ticket(
            ticket_id, args["reason"], args.get("metadata", {})
        ),
        "write_reply_email": lambda: write_reply_email(
            ticket_id, args["to_email"], args["subject"], args["body"]
        ),
    }
    if name not in functions:
        return {"error": f"Unknown tool: {name}"}
    return functions[name]()
