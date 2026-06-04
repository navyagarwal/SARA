from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Literal

import requests
from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError

from db import connect, get_ticket, log_event, update_ticket
from prompts import FINAL_DECISION_INSTRUCTIONS, SYSTEM_PROMPT
from tools import execute_tool

load_dotenv(Path(__file__).resolve().parent / ".env", override=True)

MAX_AGENT_STEPS = 15
DEFAULT_PROVIDER = "groq"
DEFAULT_AGENT_MODE = "staged_llm"
DEFAULT_GEMINI_MODEL = "gemini-2.0-flash-lite"
DEFAULT_GROQ_MODEL = "llama-3.1-8b-instant"
DEFAULT_MIN_REQUEST_INTERVAL_SECONDS = 0.0
DEFAULT_MAX_QUOTA_RETRIES = 3
RETRY_DELAY_RE = re.compile(r"retryDelay['\"]?\s*:\s*['\"]?(\d+(?:\.\d+)?)s|retry in (\d+(?:\.\d+)?)s", re.I)
GROQ_RETRY_RE = re.compile(r"try again in (\d+(?:\.\d+)?)\s*(ms|s)", re.I)


class ActionTaken(BaseModel):
    action_type: str
    status: str
    details: str


class ReplyEmail(BaseModel):
    subject: str
    body: str


class FinalDecision(BaseModel):
    classification: Literal[
        "certificate_issue",
        "refund_billing_issue",
        "course_access_issue",
        "unknown",
    ]
    root_cause: str
    resolution_type: Literal["auto_resolved", "escalated", "unable_to_resolve"]
    actions_taken: list[ActionTaken] = Field(default_factory=list)
    reply_email: ReplyEmail
    confidence: float = Field(ge=0, le=1)


class TriageResult(BaseModel):
    classification_candidates: list[str] = Field(default_factory=list)
    likely_classification: Literal[
        "certificate_issue",
        "refund_billing_issue",
        "course_access_issue",
        "unknown",
    ]
    relevant_evidence_areas: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)


class InvestigationPlan(BaseModel):
    docs_to_read: list[str] = Field(default_factory=list)
    tables_to_inspect: list[str] = Field(default_factory=list)
    query_intents: list[str] = Field(default_factory=list)


class EvidenceReasoning(BaseModel):
    root_cause_summary: str
    resolution_recommendation: Literal["auto_resolve", "escalate", "unable_to_resolve"]
    required_actions: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)


TOOL_DECLARATIONS: list[dict[str, Any]] = [
    {
        "name": "list_domain_docs",
        "description": "List available support domain knowledge documents.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "read_domain_doc",
        "description": "Read one domain knowledge document by filename.",
        "parameters": {
            "type": "object",
            "properties": {"doc_name": {"type": "string"}},
            "required": ["doc_name"],
        },
    },
    {
        "name": "list_database_tables",
        "description": "List support database tables available for safe inspection.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "describe_table",
        "description": "Return column metadata for an exposed table.",
        "parameters": {
            "type": "object",
            "properties": {"table_name": {"type": "string"}},
            "required": ["table_name"],
        },
    },
    {
        "name": "query_database",
        "description": "Run a guarded read-only SELECT query with optional parameters.",
        "parameters": {
            "type": "object",
            "properties": {
                "sql": {"type": "string"},
                "params": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["sql"],
        },
    },
    {
        "name": "create_agent_action",
        "description": "Create an approved operational action such as certificate retry.",
        "parameters": {
            "type": "object",
            "properties": {
                "action_type": {"type": "string"},
                "metadata": {"type": "object"},
            },
            "required": ["action_type"],
        },
    },
    {
        "name": "create_human_ticket",
        "description": "Create a human support review ticket.",
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {"type": "string"},
                "metadata": {"type": "object"},
            },
            "required": ["reason"],
        },
    },
    {
        "name": "write_reply_email",
        "description": "Write and send a simulated student reply email.",
        "parameters": {
            "type": "object",
            "properties": {
                "to_email": {"type": "string"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["to_email", "subject", "body"],
        },
    },
]


def process_ticket_with_agent(ticket_id: int) -> None:
    ticket = get_ticket(ticket_id)
    if ticket is None:
        return
    update_ticket(ticket_id, status="processing")
    log_event(ticket_id, "agent_started", "Agent started processing the ticket.")
    try:
        decision = call_llm_agent(ticket_id, ticket)
        finalize_ticket(ticket_id, ticket, decision)
    except Exception as error:
        update_ticket(ticket_id, status="failed", root_cause=str(error), confidence=0)
        log_event(
            ticket_id,
            "ticket_failed",
            "Agent processing failed.",
            level="ERROR",
            metadata={"error": str(error)},
        )


def call_llm_agent(ticket_id: int, ticket: dict[str, Any]) -> FinalDecision:
    provider = os.getenv("LLM_PROVIDER", DEFAULT_PROVIDER).strip().lower()
    log_event(
        ticket_id,
        "llm_provider_selected",
        f"Using {provider} as the LLM provider.",
        metadata={"provider": provider},
    )
    if provider == "groq":
        return call_groq_agent(ticket_id, ticket)
    if provider == "gemini":
        return call_gemini_agent(ticket_id, ticket)
    raise RuntimeError("LLM_PROVIDER must be either 'groq' or 'gemini'.")


def call_groq_agent(ticket_id: int, ticket: dict[str, Any]) -> FinalDecision:
    api_key = os.getenv("GROQ_API_KEY", "").strip()
    if not api_key or api_key == "your_groq_api_key_here":
        log_event(
            ticket_id,
            "ticket_failed",
            "Groq API key is missing; agent cannot run without the LLM.",
            level="ERROR",
            metadata={"required_env": "GROQ_API_KEY"},
        )
        raise RuntimeError("GROQ_API_KEY is required when LLM_PROVIDER=groq.")
    agent_mode = os.getenv("AGENT_MODE", DEFAULT_AGENT_MODE).strip().lower()
    log_event(
        ticket_id,
        "agent_mode_selected",
        f"Using {agent_mode} agent mode.",
        metadata={"agent_mode": agent_mode},
    )
    if agent_mode == "staged_llm":
        return run_groq_staged_agent(ticket_id, ticket, api_key)
    if agent_mode == "tool_loop":
        return run_groq_tool_loop(ticket_id, ticket, api_key)
    raise RuntimeError("AGENT_MODE must be either 'staged_llm' or 'tool_loop'.")


def call_gemini_agent(ticket_id: int, ticket: dict[str, Any]) -> FinalDecision:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key or api_key == "your_api_key_here":
        log_event(
            ticket_id,
            "ticket_failed",
            "Gemini API key is missing; agent cannot run without the LLM.",
            level="ERROR",
            metadata={"required_env": "GEMINI_API_KEY"},
        )
        raise RuntimeError("GEMINI_API_KEY is required. No fallback agent is configured.")
    if not api_key.startswith("AIza"):
        log_event(
            ticket_id,
            "ticket_failed",
            "Gemini credential does not look like an AI Studio API key.",
            level="ERROR",
            metadata={
                "expected": "AI Studio API key beginning with AIza",
                "actual_prefix": api_key[:4],
            },
        )
        raise RuntimeError(
            "GEMINI_API_KEY must be a Google AI Studio Gemini API key, not an OAuth access token."
        )
    return run_gemini_tool_loop(ticket_id, ticket)


def run_gemini_tool_loop(ticket_id: int, ticket: dict[str, Any]) -> FinalDecision:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    model = os.getenv("GEMINI_MODEL", DEFAULT_GEMINI_MODEL)
    contents: list[Any] = [
        types.Content(
            role="user",
            parts=[
                types.Part.from_text(
                    text=json.dumps(
                        {
                            "ticket": ticket,
                            "instructions": FINAL_DECISION_INSTRUCTIONS,
                        }
                    )
                )
            ],
        )
    ]
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        tools=[types.Tool(function_declarations=TOOL_DECLARATIONS)],
    )

    for _ in range(MAX_AGENT_STEPS):
        response = generate_content_with_quota_retry(
            ticket_id,
            client,
            model=model,
            contents=contents,
            config=config,
        )
        if response.candidates and response.candidates[0].content:
            contents.append(response.candidates[0].content)

        function_calls = getattr(response, "function_calls", None) or getattr(
            response, "functionCalls", None
        )
        if function_calls:
            response_parts = []
            for function_call in function_calls:
                args = dict(function_call.args or {})
                result = execute_tool(ticket_id, function_call.name, args)
                response_parts.append(
                    types.Part.from_function_response(
                        name=function_call.name,
                        response={"result": result},
                    )
                )
                time.sleep(0.4)
            contents.append(types.Content(role="user", parts=response_parts))
            continue

        text = getattr(response, "text", "") or ""
        return parse_final_decision(text)

    raise RuntimeError("max_agent_steps_exceeded")


def run_groq_tool_loop(ticket_id: int, ticket: dict[str, Any], api_key: str) -> FinalDecision:
    model = os.getenv("GROQ_MODEL", DEFAULT_GROQ_MODEL)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "ticket": ticket,
                    "instructions": FINAL_DECISION_INSTRUCTIONS,
                }
            ),
        },
    ]
    tools = [
        {
            "type": "function",
            "function": {
                "name": declaration["name"],
                "description": declaration["description"],
                "parameters": declaration["parameters"],
            },
        }
        for declaration in TOOL_DECLARATIONS
    ]

    for _ in range(MAX_AGENT_STEPS):
        response = call_groq_chat_completion(
            ticket_id,
            api_key=api_key,
            model=model,
            messages=messages,
            tools=tools,
        )
        message = response["choices"][0]["message"]
        messages.append(message)
        tool_calls = message.get("tool_calls") or []
        if tool_calls:
            for tool_call in tool_calls:
                function = tool_call["function"]
                args = json.loads(function.get("arguments") or "{}")
                result = execute_tool(ticket_id, function["name"], args)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call["id"],
                        "name": function["name"],
                        "content": json.dumps(compact_tool_result(function["name"], result)),
                    }
                )
                time.sleep(0.4)
            continue
        return parse_final_decision(message.get("content") or "")

    raise RuntimeError("max_agent_steps_exceeded")


def run_groq_staged_agent(ticket_id: int, ticket: dict[str, Any], api_key: str) -> FinalDecision:
    model = os.getenv("GROQ_MODEL", DEFAULT_GROQ_MODEL)
    triage = run_llm_stage(
        ticket_id,
        api_key=api_key,
        model=model,
        event="llm_triage_requested",
        message="LLM triage requested.",
        prompt=build_triage_prompt(ticket),
        response_model=TriageResult,
    )
    log_event(
        ticket_id,
        "llm_triage_completed",
        "LLM triage completed.",
        metadata=triage.model_dump(),
    )

    plan = run_llm_stage(
        ticket_id,
        api_key=api_key,
        model=model,
        event="llm_investigation_plan_requested",
        message="LLM investigation plan requested.",
        prompt=build_investigation_plan_prompt(ticket, triage),
        response_model=InvestigationPlan,
    )
    log_event(
        ticket_id,
        "llm_investigation_plan_completed",
        "LLM investigation plan completed.",
        metadata=plan.model_dump(),
    )

    evidence = build_evidence_bundle(ticket_id, ticket, triage, plan)
    reasoning = run_llm_stage(
        ticket_id,
        api_key=api_key,
        model=model,
        event="llm_evidence_reasoning_requested",
        message="LLM evidence reasoning requested.",
        prompt=build_evidence_reasoning_prompt(ticket, triage, plan, evidence),
        response_model=EvidenceReasoning,
    )
    log_event(
        ticket_id,
        "llm_evidence_reasoning_completed",
        "LLM evidence reasoning completed.",
        metadata=reasoning.model_dump(),
    )

    decision = run_llm_stage(
        ticket_id,
        api_key=api_key,
        model=model,
        event="llm_final_decision_requested",
        message="LLM final decision and email requested.",
        prompt=build_final_decision_prompt(ticket, triage, plan, evidence, reasoning),
        response_model=FinalDecision,
    )
    validate_decision_consistency(evidence, decision)
    execute_decision_actions(ticket_id, ticket, evidence, decision)
    return decision


def run_llm_stage(
    ticket_id: int,
    *,
    api_key: str,
    model: str,
    event: str,
    message: str,
    prompt: str,
    response_model: type[BaseModel],
) -> Any:
    log_event(ticket_id, event, message, metadata={"model": model})
    response = call_groq_chat_completion(
        ticket_id,
        api_key=api_key,
        model=model,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are one stage in a support automation agent. "
                    "Return only valid JSON matching the requested schema."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        tools=None,
        response_format={"type": "json_object"},
    )
    content = response["choices"][0]["message"].get("content") or "{}"
    return parse_model_json(content, response_model)


def build_triage_prompt(ticket: dict[str, Any]) -> str:
    return json.dumps(
        {
            "task": "Infer the likely support category from the student email. Do not resolve yet.",
            "ticket": ticket,
            "allowed_classifications": [
                "certificate_issue",
                "refund_billing_issue",
                "course_access_issue",
                "unknown",
            ],
            "output_schema": TriageResult.model_json_schema(),
        }
    )


def build_investigation_plan_prompt(ticket: dict[str, Any], triage: TriageResult) -> str:
    return json.dumps(
        {
            "task": "Plan which docs, schemas, and records the agent runtime should inspect.",
            "ticket": ticket,
            "triage": triage.model_dump(),
            "available_docs": [
                "overview.md",
                "certificates.md",
                "refunds_billing.md",
                "course_access.md",
            ],
            "available_tables": [
                "students",
                "courses",
                "enrollments",
                "course_progress",
                "payments",
                "certificates",
                "modules",
                "module_access",
                "refunds",
            ],
            "rules": [
                "Always include overview.md.",
                "Always include students and courses.",
                "Return only docs/table names from the available lists.",
            ],
            "output_schema": InvestigationPlan.model_json_schema(),
        }
    )


def build_evidence_reasoning_prompt(
    ticket: dict[str, Any],
    triage: TriageResult,
    plan: InvestigationPlan,
    evidence: dict[str, Any],
) -> str:
    return json.dumps(
        {
            "task": "Use the evidence to identify root cause and resolution direction. Do not draft the email yet.",
            "ticket": ticket,
            "triage": triage.model_dump(),
            "investigation_plan": plan.model_dump(),
            "evidence": evidence,
            "rules": [
                "Refund and billing requests must be escalated.",
                "Certificate retries may be auto-resolved only when enrollment is active, payment is paid, progress meets the course requirement, and certificate status is failed.",
                "Course access issues may be explained when the module is locked due to prerequisite_incomplete.",
                "Do not mention internal table names in student-facing outputs.",
            ],
            "output_schema": EvidenceReasoning.model_json_schema(),
        }
    )


def build_final_decision_prompt(
    ticket: dict[str, Any],
    triage: TriageResult,
    plan: InvestigationPlan,
    evidence: dict[str, Any],
    reasoning: EvidenceReasoning,
) -> str:
    return json.dumps(
        {
            "task": "Return the final support decision and generate the student-facing reply email yourself.",
            "ticket": ticket,
            "triage": triage.model_dump(),
            "investigation_plan": plan.model_dump(),
            "evidence": evidence,
            "reasoning": reasoning.model_dump(),
            "rules": [
                "Generate a fresh concise email body; do not use a canned template.",
                "Do not mention internal table names, logs, schemas, or implementation details in the email.",
                "Do not approve refunds.",
                "For refund or billing requests, resolution_type must be escalated and actions_taken must include create_human_ticket.",
                "For an eligible failed certificate, resolution_type should be auto_resolved and actions_taken should include retry_certificate_generation.",
                "For prerequisite-based course access, explain the prerequisite and avoid unlock_module_access.",
                "If module_access.reason is prerequisite_incomplete, the email must not say the module is unlocked or has been unlocked.",
                "If no unlock_module_access action is taken, the email must not imply access was changed.",
                "The email must be consistent with required_actions and actions_taken.",
                "Return only JSON matching the schema.",
            ],
            "output_schema": FinalDecision.model_json_schema(),
            "final_decision_contract": FINAL_DECISION_INSTRUCTIONS,
        }
    )


def build_evidence_bundle(
    ticket_id: int,
    ticket: dict[str, Any],
    triage: TriageResult,
    plan: InvestigationPlan,
) -> dict[str, Any]:
    log_event(
        ticket_id,
        "evidence_bundle_started",
        "Agent runtime started deterministic evidence retrieval.",
        actor="agent_runtime",
        metadata={"triage": triage.model_dump(), "plan": plan.model_dump()},
    )
    docs = read_planned_docs(ticket_id, plan)
    schemas = describe_planned_tables(ticket_id, plan)
    records = query_student_records(ticket_id, ticket["student_email"])
    evidence = {"docs": docs, "schemas": schemas, "records": records}
    log_event(
        ticket_id,
        "evidence_bundle_created",
        "Agent runtime assembled compact evidence bundle.",
        actor="agent_runtime",
        metadata={
            "doc_count": len(docs),
            "schema_count": len(schemas),
            "record_sections": list(records),
        },
    )
    return evidence


def read_planned_docs(ticket_id: int, plan: InvestigationPlan) -> dict[str, str]:
    available_docs = {"overview.md", "certificates.md", "refunds_billing.md", "course_access.md"}
    docs_to_read = ["overview.md", *plan.docs_to_read]
    docs: dict[str, str] = {}
    for doc_name in docs_to_read:
        if doc_name not in available_docs or doc_name in docs:
            continue
        result = execute_tool(ticket_id, "read_domain_doc", {"doc_name": doc_name})
        if "content" in result:
            docs[doc_name] = result["content"][:1600]
    return docs


def describe_planned_tables(ticket_id: int, plan: InvestigationPlan) -> dict[str, Any]:
    available_tables = {
        "students",
        "courses",
        "enrollments",
        "course_progress",
        "payments",
        "certificates",
        "modules",
        "module_access",
        "refunds",
    }
    tables_to_inspect = [
        "students",
        "courses",
        *plan.tables_to_inspect,
        "enrollments",
        "course_progress",
        "payments",
        "certificates",
        "module_access",
        "refunds",
    ]
    schemas: dict[str, Any] = {}
    for table_name in tables_to_inspect:
        if table_name not in available_tables or table_name in schemas:
            continue
        result = execute_tool(ticket_id, "describe_table", {"table_name": table_name})
        if "columns" in result:
            schemas[table_name] = compact_tool_result("describe_table", result)
    return schemas


def query_student_records(ticket_id: int, student_email: str) -> dict[str, Any]:
    queries = {
        "student": {
            "sql": "SELECT * FROM students WHERE email = ?",
            "params": [student_email],
        },
        "course_context": {
            "sql": """
            SELECT
              s.email,
              s.name,
              s.support_tier,
              c.course_code,
              c.title AS course_title,
              c.certificate_required_progress,
              e.status AS enrollment_status,
              cp.progress_percent,
              cp.last_completed_module,
              p.status AS payment_status,
              cert.status AS certificate_status,
              cert.retry_count AS certificate_retry_count,
              cert.last_error AS certificate_last_error,
              r.status AS refund_status
            FROM students s
            JOIN enrollments e ON e.student_id = s.id
            JOIN courses c ON c.id = e.course_id
            LEFT JOIN course_progress cp ON cp.student_id = s.id AND cp.course_id = c.id
            LEFT JOIN payments p ON p.student_id = s.id AND p.course_id = c.id
            LEFT JOIN certificates cert ON cert.student_id = s.id AND cert.course_id = c.id
            LEFT JOIN refunds r ON r.student_id = s.id AND r.course_id = c.id
            WHERE s.email = ?
            """,
            "params": [student_email],
        },
        "module_access": {
            "sql": """
            SELECT ma.module_number, ma.is_locked, ma.reason, m.title, m.prerequisite_module_number
            FROM module_access ma
            JOIN modules m ON m.course_id = ma.course_id AND m.module_number = ma.module_number
            WHERE ma.student_id = (SELECT id FROM students WHERE email = ?)
            ORDER BY ma.module_number
            """,
            "params": [student_email],
        },
    }
    records = {}
    for name, query in queries.items():
        result = execute_tool(ticket_id, "query_database", query)
        records[name] = compact_tool_result("query_database", result)
    return records


def execute_decision_actions(
    ticket_id: int,
    ticket: dict[str, Any],
    evidence: dict[str, Any],
    decision: FinalDecision,
) -> None:
    course_code = infer_course_code(evidence)
    action_types = [action.action_type for action in decision.actions_taken]
    for action in decision.actions_taken:
        if action.action_type in {"retry_certificate_generation", "unlock_module_access"}:
            execute_tool(
                ticket_id,
                "create_agent_action",
                {
                    "action_type": action.action_type,
                    "metadata": {
                        "student_email": ticket["student_email"],
                        "course_code": course_code,
                        "module_number": 3,
                    },
                },
            )
        elif action.action_type == "create_human_ticket":
            execute_tool(
                ticket_id,
                "create_human_ticket",
                {
                    "reason": action.details or decision.root_cause,
                    "metadata": {
                        "student_email": ticket["student_email"],
                        "classification": decision.classification,
                    },
                },
            )
    if decision.resolution_type == "escalated" and "create_human_ticket" not in action_types:
        execute_tool(
            ticket_id,
            "create_human_ticket",
            {
                "reason": decision.root_cause,
                "metadata": {
                    "student_email": ticket["student_email"],
                    "classification": decision.classification,
                },
            },
        )


def validate_decision_consistency(evidence: dict[str, Any], decision: FinalDecision) -> None:
    email_text = f"{decision.reply_email.subject}\n{decision.reply_email.body}".lower()
    action_types = {action.action_type for action in decision.actions_taken}
    module_rows = evidence.get("records", {}).get("module_access", {}).get("rows", [])
    prerequisite_locked = any(
        row.get("module_number") == 3
        and row.get("is_locked") == 1
        and row.get("reason") == "prerequisite_incomplete"
        for row in module_rows
    )
    if prerequisite_locked and "unlock_module_access" not in action_types:
        forbidden = ["now unlocked", "has been unlocked", "unlocked module 3", "module 3 is now unlocked"]
        if any(phrase in email_text for phrase in forbidden):
            raise RuntimeError("Generated reply contradicted module access evidence.")


def infer_course_code(evidence: dict[str, Any]) -> str:
    rows = evidence.get("records", {}).get("course_context", {}).get("rows", [])
    if rows:
        return rows[0].get("course_code") or "ai-productivity-101"
    return "ai-productivity-101"


def call_groq_chat_completion(
    ticket_id: int,
    *,
    api_key: str,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    response_format: dict[str, Any] | None = None,
) -> dict[str, Any]:
    max_retries = int(os.getenv("GROQ_MAX_RATE_LIMIT_RETRIES", DEFAULT_MAX_QUOTA_RETRIES))
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": 0.1,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    if response_format:
        payload["response_format"] = response_format
    for attempt in range(max_retries + 1):
        response = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
            timeout=60,
        )
        if response.status_code != 429:
            break
        if attempt >= max_retries:
            break
        wait_seconds = extract_groq_retry_seconds(response.text) or 2.0
        log_event(
            ticket_id,
            "groq_rate_limit_retry",
            "Groq rate limit reached; waiting before retrying.",
            level="WARN",
            metadata={
                "attempt": attempt + 1,
                "max_retries": max_retries,
                "retry_after_seconds": wait_seconds,
                "model": model,
            },
        )
        time.sleep(wait_seconds + 0.25)
    if response.status_code >= 400:
        log_event(
            ticket_id,
            "llm_request_failed",
            "Groq chat completion request failed.",
            level="ERROR",
            metadata={"status_code": response.status_code, "response": response.text[:1000]},
        )
        response.raise_for_status()
    return response.json()


def parse_model_json(text: str, response_model: type[BaseModel]) -> Any:
    clean = text.strip()
    if clean.startswith("```"):
        clean = clean.strip("`")
        clean = clean.removeprefix("json").strip()
    data = json.loads(clean)
    data = normalize_model_payload(data)
    try:
        return response_model.model_validate(data)
    except ValidationError:
        return response_model.model_validate(data)


def compact_tool_result(tool_name: str, result: dict[str, Any]) -> dict[str, Any]:
    if "error" in result:
        return result
    if tool_name == "read_domain_doc":
        return {
            "doc_name": result.get("doc_name"),
            "content": result.get("content", "")[:1200],
        }
    if tool_name == "describe_table":
        return {
            "table_name": result.get("table_name"),
            "columns": result.get("columns", []),
            "description": result.get("description"),
        }
    if tool_name == "query_database":
        return {
            "sql": result.get("sql"),
            "rows": result.get("rows", [])[:10],
        }
    return result


def extract_groq_retry_seconds(error_text: str) -> float | None:
    match = GROQ_RETRY_RE.search(error_text)
    if not match:
        return None
    value = float(match.group(1))
    return value / 1000 if match.group(2).lower() == "ms" else value


def generate_content_with_quota_retry(
    ticket_id: int,
    client: Any,
    *,
    model: str,
    contents: list[Any],
    config: Any,
) -> Any:
    min_interval = float(
        os.getenv("GEMINI_MIN_REQUEST_INTERVAL_SECONDS", DEFAULT_MIN_REQUEST_INTERVAL_SECONDS)
    )
    max_retries = int(os.getenv("GEMINI_MAX_QUOTA_RETRIES", DEFAULT_MAX_QUOTA_RETRIES))
    for attempt in range(max_retries + 1):
        if min_interval > 0:
            log_event(
                ticket_id,
                "gemini_request_wait",
                "Waiting before the next Gemini request to stay within quota.",
                metadata={"seconds": min_interval, "model": model},
            )
            time.sleep(min_interval)
        try:
            return client.models.generate_content(
                model=model,
                contents=contents,
                config=config,
            )
        except Exception as error:
            retry_seconds = extract_retry_delay_seconds(str(error))
            if retry_seconds is None or attempt >= max_retries:
                raise
            wait_seconds = retry_seconds + 1
            log_event(
                ticket_id,
                "gemini_quota_retry",
                "Gemini quota was exhausted; waiting for the provider retry window.",
                level="WARN",
                metadata={
                    "attempt": attempt + 1,
                    "max_retries": max_retries,
                    "retry_after_seconds": wait_seconds,
                    "model": model,
                },
            )
            time.sleep(wait_seconds)


def extract_retry_delay_seconds(error_text: str) -> float | None:
    match = RETRY_DELAY_RE.search(error_text)
    if not match:
        return None
    value = match.group(1) or match.group(2)
    return float(value) if value else None


def parse_final_decision(text: str) -> FinalDecision:
    clean = text.strip()
    if clean.startswith("```"):
        clean = clean.strip("`")
        clean = clean.removeprefix("json").strip()
    data = normalize_model_payload(json.loads(clean))
    try:
        return FinalDecision.model_validate(data)
    except ValidationError:
        return FinalDecision.model_validate(data)


def normalize_model_payload(data: Any) -> Any:
    if not isinstance(data, dict):
        return data
    if data.get("resolution_type") == "auto_resolve":
        data["resolution_type"] = "auto_resolved"
    if data.get("resolution_recommendation") == "auto_resolved":
        data["resolution_recommendation"] = "auto_resolve"
    return data


def finalize_ticket(ticket_id: int, ticket: dict[str, Any], decision: FinalDecision) -> None:
    if not reply_email_exists(ticket_id):
        execute_tool(
            ticket_id,
            "write_reply_email",
            {
                "to_email": ticket["student_email"],
                "subject": decision.reply_email.subject,
                "body": decision.reply_email.body,
            },
        )
    status = "resolved" if decision.resolution_type == "auto_resolved" else "escalated"
    if decision.resolution_type == "unable_to_resolve":
        status = "failed"
    update_ticket(
        ticket_id,
        status=status,
        classification=decision.classification,
        root_cause=decision.root_cause,
        confidence=decision.confidence,
    )
    log_event(
        ticket_id,
        "agent_decision",
        "Agent returned a structured final decision.",
        metadata=decision.model_dump(),
    )
    log_event(
        ticket_id,
        "ticket_resolved" if status == "resolved" else "ticket_escalated",
        f"Ticket marked {status}.",
        actor="system",
    )


def reply_email_exists(ticket_id: int) -> bool:
    with connect() as connection:
        row = connection.execute(
            "SELECT 1 FROM email_outbox WHERE ticket_id = ? LIMIT 1",
            (ticket_id,),
        ).fetchone()
    return row is not None
