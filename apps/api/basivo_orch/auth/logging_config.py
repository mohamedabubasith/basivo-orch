"""Structured logging.

JSON in production so log aggregators can index fields; human-readable in
development. The processor chain drops known-sensitive keys before rendering,
which matters because logs are retained longer and read more widely than any
other artefact this service produces.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

from basivo_orch.auth.settings import get_settings

SENSITIVE_KEYS = frozenset(
    {
        "password", "new_password", "current_password", "hashed_password",
        "token", "access_token", "refresh_token", "step_up_token",
        "secret", "jwt_secret", "totp_secret", "client_secret",
        "authorization", "cookie", "set-cookie", "code", "otp",
        "recovery_code", "api_key",
    }
)

REDACTED = "[redacted]"


def _scrub(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Last-chance redaction.

    Call sites are supposed to avoid logging secrets; this guarantees it even
    when someone forgets, or when a dict is splatted into a log call wholesale.
    """
    for key in list(event_dict):
        if key.lower() in SENSITIVE_KEYS:
            event_dict[key] = REDACTED
    return event_dict


def configure_logging() -> None:
    settings = get_settings()

    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        _scrub,
    ]

    if settings.log_json:
        processors = [
            *shared,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ]
    else:
        processors = [*shared, structlog.dev.ConsoleRenderer(colors=True)]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, settings.log_level.upper(), logging.INFO)
        ),
        # Writes through the stdlib logger rather than straight to stdout. That
        # is what `add_logger_name` needs (a PrintLogger has no `.name`), and it
        # keeps our output in the same stream and ordering as anything uvicorn
        # or SQLAlchemy emits.
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
    )
    # These are chatty at INFO and duplicate what our own middleware records.
    for noisy in ("uvicorn.access", "sqlalchemy.engine.Engine", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
