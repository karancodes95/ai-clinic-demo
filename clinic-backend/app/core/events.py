"""Structured event logging. Writes to the `events` table per session
and prints a one-line summary to stdout for live tail monitoring.

PII scrubbing is currently DISABLED for the public demo: a lead may
type their email or phone into the chat to ask about a real product,
and we want that to land verbatim in the events log so we can follow
up. The original M3 scrubber (regexes + _scrub) is kept below behind
a feature flag - flip `_SCRUB_PII = True` to re-enable for any future
deployment that holds real customer data.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from uuid import UUID

from app.db import pool

logger = logging.getLogger("events")

# Feature flag: scrubbing is OFF for the public demo (we want lead
# contact info to flow into the events table verbatim). Set to True
# when deploying to any context that holds real customer data.
_SCRUB_PII = False

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
# Phone matcher - 7+ digits with optional separators / country prefix.
# The (?<![\d-]) / (?![\d-]) guards stop it eating UUID segments like
# `0000-0000-0000` from session_ids logged in payloads.
_PHONE_RE = re.compile(
    r"(?<![\d-])(?:\+?\d{1,3}[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?![\d-])"
)

# Fields whose values get scrubbed when _SCRUB_PII is True. Other fields
# (sql, ms, count, status) are not user-typed text and are kept verbatim
# for debuggability regardless.
_REDACT_FIELDS = {"text", "question", "answer", "notes", "message"}


def _redact(s: str) -> str:
    s = _EMAIL_RE.sub("[redacted-email]", s)
    s = _PHONE_RE.sub("[redacted-phone]", s)
    return s


def _scrub(payload: dict[str, Any]) -> dict[str, Any]:
    if not _SCRUB_PII:
        return payload
    out: dict[str, Any] = {}
    for k, v in payload.items():
        if k in _REDACT_FIELDS and isinstance(v, str):
            out[k] = _redact(v)
        else:
            out[k] = v
    return out


async def log(
    session_id: UUID,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> None:
    payload = _scrub(payload or {})
    try:
        async with pool().acquire() as conn:
            await conn.execute(
                "INSERT INTO events(session_id, type, payload) "
                "VALUES ($1, $2, $3::jsonb)",
                session_id, event_type, json.dumps(payload),
            )
    except Exception:  # never let logging crash the request
        # Closes review L6 - full traceback so silent log-fail isn't invisible.
        logger.exception("event log failed (event_type=%s)", event_type)

    short = {k: _short(v) for k, v in payload.items()}
    logger.info("[%s] %s %s", str(session_id)[:8], event_type, short)


def _short(v: Any) -> Any:
    if isinstance(v, str) and len(v) > 120:
        return v[:117] + "..."
    return v
