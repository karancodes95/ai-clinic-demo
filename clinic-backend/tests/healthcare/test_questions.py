"""Runs every question in questions.yaml against /chat with a seeded healthcare session.

Assertions per question (all optional):
  - sql_contains:     substrings that must appear in the generated SQL
  - answer_contains:  tokens that must appear in the streamed NL answer

Real LLM calls (Gemini by default). Retries on 503 / throttle with exponential
backoff - skip the whole test after N failures rather than fail red.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import httpx
import pytest
import yaml

from app.config import settings

HERE = Path(__file__).resolve().parent
QUESTIONS_FILE = HERE / "questions.yaml"

_LLM_RETRIES = 6
_LLM_BACKOFF_BASE = 3.0  # seconds


def _load_questions() -> list[dict]:
    data = yaml.safe_load(QUESTIONS_FILE.read_text())
    return list(data.get("questions") or [])


def _normalize_sql(s: str) -> str:
    """Collapse whitespace, lowercase - for substring matching."""
    return re.sub(r"\s+", " ", s).strip().lower()


async def _chat_once(client: httpx.AsyncClient, session_id: str, question: str) -> tuple[str, str, dict | None, dict]:
    """Send a /chat request, parse SSE stream, return (sql, answer, error_payload, usage).

    error_payload is the dict from an 'error' SSE event if one occurred, else None.
    usage is {"prompt": int, "completion": int, "total": int} (zeros if missing).
    """
    sql = ""
    answer_chunks: list[str] = []
    err: dict | None = None
    usage: dict = {"prompt": 0, "completion": 0, "total": 0}

    async with client.stream(
        "POST",
        "/chat",
        json={"question": question},
        headers={
            "x-api-key": settings.api_key,
            "x-session-id": session_id,
        },
        timeout=60.0,
    ) as res:
        assert res.status_code == 200, await res.aread()
        current_event: str | None = None
        buffer: list[str] = []
        async for line in res.aiter_lines():
            if line == "":
                data = "\n".join(buffer) if buffer else ""
                if current_event == "sql":
                    sql = data
                elif current_event == "token":
                    answer_chunks.append(data)
                elif current_event == "error":
                    try:
                        err = json.loads(data)
                    except Exception:
                        err = {"raw": data}
                elif current_event == "usage":
                    try:
                        usage = json.loads(data)
                    except Exception:
                        pass
                current_event = None
                buffer = []
                continue
            if line.startswith("event:"):
                current_event = line[len("event:"):].strip()
            elif line.startswith("data:"):
                buffer.append(line[len("data:"):].lstrip())

    return sql, "".join(answer_chunks), err, usage


async def _chat_with_retry(client: httpx.AsyncClient, session_id: str, question: str) -> tuple[str, str, dict]:
    """Wrapper around _chat_once with retry on LLM throttle (503).

    Returns (sql, answer, usage). Raises pytest.skip on persistent throttle, or
    AssertionError on other errors.
    """
    last_err: dict | None = None
    for attempt in range(_LLM_RETRIES):
        sql, answer, err, usage = await _chat_once(client, session_id, question)
        if err is None:
            return sql, answer, usage
        msg = str(err.get("message", ""))
        low = msg.lower()
        # Hard quota / spending cap - retrying won't help; skip fast.
        if "429" in msg or "resource_exhausted" in low or "spending cap" in low or "quota" in low:
            pytest.skip(f"LLM quota exhausted: {msg[:200]}")
        # Transient throttle - back off and retry.
        if "503" in msg or "unavailable" in low or "throttl" in low:
            last_err = err
            await asyncio.sleep(_LLM_BACKOFF_BASE * (2 ** attempt))
            continue
        raise AssertionError(f"chat error: stage={err.get('stage')} message={msg[:300]}")
    pytest.skip(f"LLM throttled after {_LLM_RETRIES} retries: {last_err}")


QUESTIONS = _load_questions()


@pytest.mark.parametrize(
    "q",
    QUESTIONS,
    ids=[q["id"] for q in QUESTIONS] if QUESTIONS else None,
)
async def test_question(client, healthcare_session, q, token_totals):
    sql, answer, usage = await _chat_with_retry(client, healthcare_session, q["question"])

    token_totals["rows"].append({"id": q["id"], **usage})
    for k in ("prompt", "completion", "total"):
        token_totals[k] = token_totals.get(k, 0) + int(usage.get(k, 0) or 0)

    # SQL-shape assertions.
    norm_sql = _normalize_sql(sql)
    for tok in q.get("sql_contains") or []:
        assert tok.lower() in norm_sql, (
            f"[{q['id']}] expected SQL to contain {tok!r}.\nSQL: {sql}"
        )

    # Answer-text assertions. Each item is either:
    #   - a literal string (must appear), or
    #   - a list of alternatives (any one must appear - any-of).
    low_answer = answer.lower()
    for tok in q.get("answer_contains") or []:
        if isinstance(tok, list):
            ok = any(alt.lower() in low_answer for alt in tok)
            assert ok, (
                f"[{q['id']}] expected any of {tok} in answer.\nAnswer: {answer}"
            )
        else:
            assert tok.lower() in low_answer, (
                f"[{q['id']}] expected answer to contain {tok!r}.\nAnswer: {answer}"
            )
