"""Conversation history for multi-turn chat.

Pulls the last N (question, answer) pairs from the `events` table for a session
and formats them for prompt injection. Lets the LLM resolve references like
"those members" or "and what about Q1?" against prior turns.
"""

from __future__ import annotations

import json
from uuid import UUID

from app.db import pool

# Default window: 15 turns - matches the per-IP daily cap, so users never
# lose context within their allowed quota. Cost overhead at turn 15 is
# ~3000 prompt tokens (~₹0.40/turn at Pro rates) - trivial.
DEFAULT_TURNS = 15


async def recent_turns(session_id: UUID, *, limit: int = DEFAULT_TURNS) -> list[dict]:
    """Return up to `limit` most-recent (question, answer) pairs, oldest-first.

    Pairs are built by walking events in time order and matching each
    `question_asked` to the next `answer_streamed`. Errored turns (no answer)
    are skipped - including them would feed the LLM a mid-failure state.
    """
    rows = await pool().fetch(
        "SELECT type, payload "
        "FROM events "
        "WHERE session_id = $1 "
        "  AND type IN ('question_asked', 'answer_streamed') "
        "ORDER BY ts ASC, id ASC",
        session_id,
    )

    pairs: list[dict] = []
    pending_q: str | None = None
    for r in rows:
        # asyncpg returns jsonb as str - parse here.
        raw = r["payload"]
        payload = json.loads(raw) if isinstance(raw, str) else (raw or {})
        if r["type"] == "question_asked":
            pending_q = payload.get("text")
        elif r["type"] == "answer_streamed" and pending_q is not None:
            pairs.append({"question": pending_q, "answer": payload.get("text", "")})
            pending_q = None

    # Drop the last pair: it's the current turn we're answering right now
    # (the question_asked event for the in-flight turn was already logged
    # before history is fetched, but its answer_streamed hasn't been written
    # yet, so it won't pair - the unpaired question is in `pending_q` which
    # we ignore. So pairs already excludes the in-flight turn.)
    return pairs[-limit:]


def format_for_prompt(turns: list[dict]) -> str:
    """Format turns as 'User: ... / Assistant: ...' lines for prompt injection.

    Includes a directive so the LLM uses prior context to resolve references
    like 'those', 'them', 'that release', or 'and what about Q1?' against the
    last few turns instead of asking the user to repeat themselves.
    """
    if not turns:
        return ""
    lines = [
        "Conversation so far (oldest -> newest). Use it to resolve references "
        "like 'those', 'them', 'that release', 'last quarter' in the next "
        "user question. Do NOT re-answer prior questions."
    ]
    for t in turns:
        lines.append(f"User: {t['question']}")
        lines.append(f"Assistant: {t['answer']}")
    return "\n".join(lines)
