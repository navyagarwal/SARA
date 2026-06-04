from __future__ import annotations

import logging
import sys
from typing import Any

import structlog


def configure_logging() -> None:
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.add_log_level,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        cache_logger_on_first_use=True,
    )

    root_logger = logging.getLogger()
    if not root_logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        root_logger.addHandler(handler)
    root_logger.setLevel(logging.INFO)


def get_logger(name: str) -> structlog.BoundLogger:
    configure_logging()
    return structlog.get_logger(name)


def terminal_line(
    *,
    timestamp: str,
    level: str,
    actor: str,
    event: str,
    message: str,
    metadata: dict[str, Any] | None = None,
) -> str:
    fields = {
        "ts": timestamp,
        "level": level,
        "actor": actor,
        "event": event,
        "message": message,
    }
    if metadata:
        fields["metadata"] = metadata
    return " ".join(f"{key}={value!r}" for key, value in fields.items())
