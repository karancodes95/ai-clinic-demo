"""Chat orchestration: question -> SQL -> rows -> NL answer.
Streams progress as SSE events for the Flutter client to render live."""

from __future__ import annotations

import json
import re
import time
from typing import AsyncIterator
from uuid import UUID

from app.core import events, history, sessions
from app.config import settings
from app.llm import get_llm
from app.core.sql_runner import UnsafeSQL, run_scoped
from app.vertical.healthcare import prompt as healthcare_prompt


async def _maybe_log_slow(session_id: UUID, stage: str, ms: int) -> None:
    """Closes review L5 - fire a `slow_query` event when latency exceeds
    the configured threshold so weekly mining can spot regressions."""
    if ms > settings.slow_query_threshold_ms:
        await events.log(session_id, "slow_query", {"stage": stage, "ms": ms})


def _prompt_for(vertical: str | None):
    """Return the prompt module. This backend serves the healthcare vertical only."""
    return healthcare_prompt


def _sse(event: str, payload: dict | str) -> str:
    data = payload if isinstance(payload, str) else json.dumps(payload)
    # SSE: split data on newlines so multiline values stay valid
    data_lines = "\n".join(f"data: {line}" for line in data.splitlines() or [""])
    return f"event: {event}\n{data_lines}\n\n"


# Closes review H2 - public client never sees the raw LLM exception text
# (Gemini SDK errors leak project IDs, model URLs, sometimes key fragments).
# Full text still goes to the events table for backend debugging.
_PUBLIC_LLM_ERROR_MESSAGES = {
    "llm_sql": "couldn't generate SQL - try again or rephrase",
    "llm_oos": "couldn't write the refusal - try again",
    "llm_answer": "couldn't write the answer - try again",
}


# A "greeting" out-of-scope is any short social message - hi, thanks, ok cool.
# Word-count heuristic so analytics can split greetings from real off-topic
# questions without a second LLM call. Tuned to the prompt's ≤10-words rule.
_GREETING_WORD_THRESHOLD = 10


def _oos_kind(question: str) -> str:
    return "greeting" if len(question.split()) <= _GREETING_WORD_THRESHOLD else "off_topic"


def _public_error(stage: str, raw: str) -> str:
    """Return a sanitized client-facing error message for a given stage."""
    return _PUBLIC_LLM_ERROR_MESSAGES.get(stage, "request failed - try again")


_CODE_FENCE = re.compile(r"^\s*```(?:sql)?\s*|\s*```\s*$", re.IGNORECASE | re.MULTILINE)


def _strip_fences(sql: str) -> str:
    return _CODE_FENCE.sub("", sql).strip()


def _add_usage(acc: dict, u: dict) -> None:
    for k in ("prompt", "completion", "total"):
        acc[k] = acc.get(k, 0) + int(u.get(k, 0) or 0)


def _err_payload(stage: str, msg: str, exc: BaseException | None, debug_mode: bool) -> dict:
    """Build the error event payload. In debug mode the raw exception text +
    type are included under `debug` so the UI can show / copy them. In
    normal mode the public sanitized message is the only thing the client
    ever sees - closes review H2."""
    payload: dict = {"stage": stage, "message": _public_error(stage, msg)}
    if debug_mode:
        payload["debug"] = {
            "raw": msg,
            "type": type(exc).__name__ if exc is not None else "Exception",
        }
    return payload


async def chat_stream(
    *, session_id: UUID, question: str, debug_mode: bool = False,
) -> AsyncIterator[str]:
    llm = get_llm()
    vertical = await sessions.get_vertical(session_id)
    vp = _prompt_for(vertical)
    t0 = time.perf_counter()
    usage = {"prompt": 0, "completion": 0, "total": 0}

    # Pull conversation history BEFORE logging the current question, so the
    # in-flight turn isn't included in its own context.
    turns = await history.recent_turns(session_id)
    history_block = history.format_for_prompt(turns)

    await events.log(session_id, "question_asked", {"text": question})

    # 1. Generate SQL.
    yield _sse("status", "generating_sql")
    sql_user = (
        (f"{history_block}\n\n" if history_block else "")
        + f"User question: {question}\n\nSQL:"
    )
    t = time.perf_counter()
    try:
        raw_sql = await llm.complete(sql_user, system=vp.sql_system_prompt())
    except Exception as e:
        msg = str(e)[:600]
        await events.log(session_id, "error", {"stage": "llm_sql", "message": msg})
        yield _sse("error", _err_payload("llm_sql", msg, e, debug_mode))
        return
    sql = _strip_fences(raw_sql)
    _add_usage(usage, getattr(llm, "last_usage", {}) or {})

    # Out-of-scope fast path: every non-SQL classification routes here. The
    # OOS prompt itself handles BOTH a warm greeting tone (for short social
    # messages) and a redirect tone (for off-topic / business-adjacent). A
    # word-count heuristic tags the event so analytics can split greetings
    # from real off-topic questions for weekly topic mining.
    sql_first_line = sql.strip().splitlines()[0] if sql.strip() else ""
    if sql_first_line == getattr(vp, "OUT_OF_SCOPE_SENTINEL", "__never__"):
        kind = _oos_kind(question)
        await events.log(session_id, "out_of_scope", {
            "question": question,
            "kind": kind,
            "ms": int((time.perf_counter() - t) * 1000),
        })
        yield _sse("out_of_scope", {
            "kind": kind,
            "sample_prompts": list(getattr(vp, "SAMPLE_PROMPTS", [])),
        })
        yield _sse("status", "writing_answer")
        oos_user = f"User message: {question}"
        t2 = time.perf_counter()
        buf: list[str] = []
        try:
            async for chunk in llm.stream(oos_user, system=vp.out_of_scope_prompt()):
                buf.append(chunk)
                yield _sse("token", chunk)
        except Exception as e:
            msg = str(e)[:600]
            await events.log(session_id, "error", {"stage": "llm_oos", "message": msg})
            yield _sse("error", _err_payload("llm_oos", msg, e, debug_mode))
            return
        oos_text = "".join(buf)
        _add_usage(usage, getattr(llm, "last_usage", {}) or {})
        await events.log(session_id, "answer_streamed", {
            "text": oos_text,
            "chars": len(oos_text),
            "ms": int((time.perf_counter() - t2) * 1000),
            "kind": "out_of_scope",
            "oos_kind": kind,
        })
        yield _sse("usage", usage)
        await events.log(session_id, "turn_complete", {
            "total_ms": int((time.perf_counter() - t0) * 1000),
            "kind": "out_of_scope",
            "oos_kind": kind,
            "usage": usage,
            "model": getattr(llm, "model_id", "unknown"),
        })
        yield _sse("done", {})
        return

    sql_ms = int((time.perf_counter() - t) * 1000)
    await events.log(session_id, "sql_generated", {"sql": sql, "ms": sql_ms})
    await _maybe_log_slow(session_id, "llm_sql", sql_ms)
    yield _sse("sql", sql)

    # 2. Execute scoped to session.
    yield _sse("status", "running_query")
    t = time.perf_counter()
    try:
        # Fail closed: a missing/empty allowlist must never mean "all tables"
        # (that would expose the non-RLS sessions/events/leads tables).
        allowed_tables = getattr(vp, "TABLES", None)
        if not allowed_tables:
            raise UnsafeSQL("no table allowlist configured for this vertical")
        rows = await run_scoped(
            sql, session_id=session_id, allowed_tables=allowed_tables
        )
    except UnsafeSQL as e:
        await events.log(session_id, "error", {"stage": "validate", "message": str(e), "sql": sql})
        yield _sse("error", _err_payload("validate", str(e), e, debug_mode))
        return
    except Exception as e:  # pg errors, syntax errors, etc.
        await events.log(session_id, "error", {"stage": "execute", "message": str(e), "sql": sql})
        yield _sse("error", _err_payload("execute", str(e), e, debug_mode))
        return
    exec_ms = int((time.perf_counter() - t) * 1000)
    await events.log(session_id, "query_executed", {
        "row_count": len(rows), "ms": exec_ms,
    })
    await _maybe_log_slow(session_id, "execute", exec_ms)
    if not rows:
        # Zero rows is a refusal signal - the answer prompt is told to say
        # "no data" plainly rather than invent. We log separately so weekly
        # mining can spot patterns of seed-data gaps.
        await events.log(session_id, "zero_rows", {
            "question": question, "sql": sql,
        })
    yield _sse("rows", {"count": len(rows), "sample": rows[:5]})

    # 3. Stream natural-language answer.
    yield _sse("status", "writing_answer")
    answer_system = vp.answer_system_prompt() + getattr(vp, "ANSWER_FORMAT_HINT", "")
    answer_user = (
        (f"{history_block}\n\n" if history_block else "")
        + f"User question: {question}\n\n"
        + f"SQL used:\n{sql}\n\n"
        + f"Result rows ({len(rows)} total, first {min(20, len(rows))} shown):\n"
        + json.dumps(rows[:20], indent=2)
    )
    t = time.perf_counter()
    answer_buf = []
    try:
        async for chunk in llm.stream(answer_user, system=answer_system):
            answer_buf.append(chunk)
            yield _sse("token", chunk)
    except Exception as e:
        msg = str(e)[:600]
        await events.log(session_id, "error", {"stage": "llm_answer", "message": msg})
        yield _sse("error", _err_payload("llm_answer", msg, e, debug_mode))
        return

    answer_text = "".join(answer_buf)
    _add_usage(usage, getattr(llm, "last_usage", {}) or {})
    answer_ms = int((time.perf_counter() - t) * 1000)
    await events.log(session_id, "answer_streamed", {
        "text": answer_text,
        "chars": len(answer_text),
        "ms": answer_ms,
    })
    await _maybe_log_slow(session_id, "llm_answer", answer_ms)
    yield _sse("usage", usage)
    await events.log(session_id, "turn_complete", {
        "total_ms": int((time.perf_counter() - t0) * 1000),
        "usage": usage,
        "model": getattr(llm, "model_id", "unknown"),
    })
    yield _sse("done", {})
