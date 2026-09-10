"""Full-feature integration test for the healthcare vertical.

Covers the complete public surface a buyer-facing demo touches:

  1. /health           - service is up
  2. /version          - build identifier exposed
  3. /session          - RLS-scoped session created with vertical+seed
  4. /schema           - returns the 5 expected tables + session-scoped row
                         counts (session_id column excluded) + sample prompts
  5. /chat x 12        - curated reliable questions with strict SQL+answer
                         assertions (real Gemini, retry on throttle)
  6. /history          - the chat turns persist and read back
  7. /quota            - quota endpoint shape (test client is exempt)
  8. cross-session     - two sessions each see ONLY their own seeded rows
                         (per-session counts, proven via /schema)
  9. /leads            - lead capture

Strict by design: every assertion is a hard bar - no soft-asserts, no fuzzy
"contains anything reasonable". LLM endpoints retry on 503 and skip on
quota exhaustion (so CI doesn't flap), but on success the answer MUST
contain the seeded ground-truth tokens.

Per-table data routes (e.g. /alcohol/club_members in the old vertical) are
intentionally NOT part of this vertical: it is patient-scoped (one patient per
session) and the UI reads everything through /chat and /schema. So the schema
pane + chat are the surfaces tested here, not per-table list endpoints.

Run:
    pytest tests/healthcare/test_full_feature.py -v -s
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from app.config import settings

_LLM_RETRIES = 6
_LLM_BACKOFF_BASE = 3.0

# The single-patient seed is deterministic, so the per-table row counts a
# session sees through /schema are fixed. (session_id is excluded from the
# schema view; these are the visible tenant rows.)
EXPECTED_ROW_COUNTS = {
    "doctors": 2,
    "patients": 1,
    "appointments": 5,
    "prescriptions": 3,
    "lab_results": 4,
}


# ============================================================
# Curated 12 questions - picked for high reliability (clear ground
# truth in the seed, unambiguous phrasing, single-table or simple-join
# SQL, low chance of LLM creativity drift).
# ============================================================

CURATED_QUESTIONS: list[dict] = [
    {
        "id": "ff-01",
        "question": "How many active prescriptions do I have?",
        "sql_contains": ["prescriptions", "active"],
        "answer_contains": [["3", "three"]],
    },
    {
        "id": "ff-02",
        "question": "How many refills are left on my Lisinopril?",
        "sql_contains": ["prescriptions", "Lisinopril"],
        "answer_contains": [["2", "two"]],
    },
    {
        "id": "ff-03",
        "question": "What was my most recent Hemoglobin A1C result?",
        "sql_contains": ["lab_results"],
        "answer_contains": ["6.8"],
    },
    {
        "id": "ff-04",
        "question": "What is my LDL cholesterol value?",
        "sql_contains": ["lab_results"],
        "answer_contains": ["138"],
    },
    {
        "id": "ff-05",
        "question": "Which of my lab results is still preliminary?",
        "sql_contains": ["lab_results", "preliminary"],
        "answer_contains": ["Triglycerides"],
    },
    {
        "id": "ff-06",
        "question": "How many upcoming appointments do I have?",
        "sql_contains": ["appointments"],
        "answer_contains": [["2", "two"]],
    },
    {
        "id": "ff-07",
        "question": "Who is the doctor for my next appointment?",
        "sql_contains": ["appointments", "doctors"],
        "answer_contains": ["Torres"],
    },
    {
        "id": "ff-08",
        "question": "What specialties do my doctors cover?",
        "sql_contains": ["doctors"],
        "answer_contains": ["Cardiology", "Family Medicine"],
    },
    {
        "id": "ff-09",
        "question": "List all my active medications.",
        "sql_contains": ["prescriptions"],
        "answer_contains": ["Lisinopril", "Atorvastatin", "Metformin"],
    },
    {
        "id": "ff-10",
        "question": "Have I had any no-show appointments?",
        "sql_contains": ["appointments", "no_show"],
        "answer_contains": ["Flu"],
    },
    {
        "id": "ff-11",
        "question": "How many lab results do I have in total?",
        "sql_contains": ["lab_results"],
        "answer_contains": [["4", "four"]],
    },
    {
        "id": "ff-12",
        "question": "Which of my medications has the most refills remaining?",
        "sql_contains": ["prescriptions", "refills_remaining"],
        "answer_contains": ["Atorvastatin"],
    },
]


# ============================================================
# SSE helpers (copy of test_questions.py utilities - kept local so
# this file is self-contained and can be deleted without affecting
# the existing harness).
# ============================================================

def _normalize_sql(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


async def _chat_once(
    client: httpx.AsyncClient, session_id: str, question: str
) -> tuple[str, str, dict | None]:
    sql = ""
    answer: list[str] = []
    err: dict | None = None
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
                    answer.append(data)
                elif current_event == "error":
                    try:
                        err = json.loads(data)
                    except Exception:
                        err = {"raw": data}
                current_event = None
                buffer = []
                continue
            if line.startswith("event:"):
                current_event = line[len("event:"):].strip()
            elif line.startswith("data:"):
                buffer.append(line[len("data:"):].lstrip())
    return sql, "".join(answer), err


async def _chat_with_retry(
    client: httpx.AsyncClient, session_id: str, question: str
) -> tuple[str, str]:
    last_err: dict | None = None
    for attempt in range(_LLM_RETRIES):
        sql, answer, err = await _chat_once(client, session_id, question)
        if err is None:
            return sql, answer
        msg = str(err.get("message", ""))
        low = msg.lower()
        if "429" in msg or "resource_exhausted" in low or "spending cap" in low or "quota" in low:
            pytest.skip(f"LLM quota exhausted: {msg[:200]}")
        if "503" in msg or "unavailable" in low or "throttl" in low:
            last_err = err
            await asyncio.sleep(_LLM_BACKOFF_BASE * (2 ** attempt))
            continue
        raise AssertionError(f"chat error: stage={err.get('stage')} message={msg[:300]}")
    pytest.skip(f"LLM throttled after {_LLM_RETRIES} retries: {last_err}")


# ============================================================
# Per-module session - shared across all tests in this file.
# ============================================================

@pytest_asyncio.fixture(scope="module")
async def feature_session(client) -> str:
    res = await client.post(
        "/session",
        params={"vertical": "healthcare", "referrer_tag": "test:full_feature"},
        headers={"x-api-key": settings.api_key},
    )
    assert res.status_code == 200, res.text
    sid = res.json()["session_id"]
    print(f"\n=== test_full_feature session_id: {sid} ===")
    return sid


# ============================================================
# 1. Health
# ============================================================

async def test_health_endpoint(client):
    res = await client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


# ============================================================
# 2. Version
# ============================================================

async def test_version_endpoint(client):
    res = await client.get("/version")
    assert res.status_code == 200
    body = res.json()
    assert "version" in body and isinstance(body["version"], str)


# ============================================================
# 3. Session create
# ============================================================

async def test_session_create_returns_uuid_and_vertical(client):
    res = await client.post(
        "/session",
        params={"vertical": "healthcare"},
        headers={"x-api-key": settings.api_key},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["vertical"] == "healthcare"
    # Must be a valid UUID - backend uses UUIDs as the tenant key.
    uuid.UUID(body["session_id"])


async def test_session_create_rejects_unknown_vertical(client):
    res = await client.post(
        "/session",
        params={"vertical": "not_a_vertical"},
        headers={"x-api-key": settings.api_key},
    )
    assert res.status_code == 400


async def test_session_create_rejects_missing_api_key(client):
    res = await client.post("/session", params={"vertical": "healthcare"})
    assert res.status_code == 401


# ============================================================
# 4. Schema - 5 tables + session-scoped row counts + sample prompts.
# session_id is excluded from the user-facing column list.
# ============================================================

async def test_schema_returns_five_healthcare_tables(client, feature_session):
    res = await client.get(
        "/schema",
        headers={
            "x-api-key": settings.api_key,
            "x-session-id": feature_session,
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["vertical"] == "healthcare"
    table_names = {t["name"] for t in body["tables"]}
    assert table_names == {
        "doctors", "patients", "appointments", "prescriptions", "lab_results",
    }, f"unexpected tables: {table_names}"
    # Each table must have columns + the seeded row count, and the tenant
    # column (session_id) must never be exposed in the schema view.
    for t in body["tables"]:
        assert t["row_count"] == EXPECTED_ROW_COUNTS[t["name"]], (
            f"{t['name']} row_count={t['row_count']}, "
            f"expected {EXPECTED_ROW_COUNTS[t['name']]}"
        )
        assert t["row_count"] > 0, f"{t['name']} has 0 rows - seed broken"
        col_names = {c["name"] for c in t["columns"]}
        assert col_names, f"{t['name']} has no columns"
        assert "session_id" not in col_names, (
            f"{t['name']} leaked the session_id tenant column"
        )
    # Sample prompts feed the chip strip in the UI - must be non-empty.
    assert len(body["sample_prompts"]) >= 3


# ============================================================
# 5. /chat x 12 - strict SQL + answer assertions on curated questions
# ============================================================

@pytest.mark.parametrize(
    "q",
    CURATED_QUESTIONS,
    ids=[q["id"] for q in CURATED_QUESTIONS],
)
async def test_curated_chat_question(client, feature_session, q):
    sql, answer = await _chat_with_retry(client, feature_session, q["question"])
    assert sql, f"[{q['id']}] empty SQL"
    assert answer, f"[{q['id']}] empty answer"

    norm_sql = _normalize_sql(sql)
    for tok in q["sql_contains"]:
        assert tok.lower() in norm_sql, (
            f"[{q['id']}] SQL missing {tok!r}\nQ: {q['question']}\nSQL: {sql}"
        )

    low_answer = answer.lower()
    for tok in q["answer_contains"]:
        if isinstance(tok, list):
            assert any(alt.lower() in low_answer for alt in tok), (
                f"[{q['id']}] answer missing any of {tok}\nA: {answer}"
            )
        else:
            assert tok.lower() in low_answer, (
                f"[{q['id']}] answer missing {tok!r}\nA: {answer}"
            )


# ============================================================
# 6. History - a chat turn persists and reads back on the same session.
# ============================================================

async def test_history_persists_chat_turn(client):
    """A fresh session, one /chat, then /history returns that turn so we know
    exactly which row to inspect (module session is shared by many tests)."""
    res = await client.post(
        "/session",
        params={"vertical": "healthcare"},
        headers={"x-api-key": settings.api_key},
    )
    assert res.status_code == 200, res.text
    sid = res.json()["session_id"]

    question = "How many active prescriptions do I have?"
    _sql, answer = await _chat_with_retry(client, sid, question)

    res = await client.get(
        "/history",
        headers={
            "x-api-key": settings.api_key,
            "x-session-id": sid,
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["vertical"] == "healthcare"
    turns = body["turns"]
    assert isinstance(turns, list) and len(turns) >= 1
    last = turns[-1]
    assert last["question"] == question, f"history question mismatch: {last}"
    assert last["answer"], "history turn has empty answer"


async def test_history_unknown_session_404(client):
    res = await client.get(
        "/history",
        headers={
            "x-api-key": settings.api_key,
            "x-session-id": str(uuid.uuid4()),
        },
    )
    assert res.status_code == 404


# ============================================================
# 7. Quota - endpoint shape (the ASGI test client is exempt from the cap).
# ============================================================

async def test_quota_endpoint_shape(client):
    res = await client.get("/quota", headers={"x-api-key": settings.api_key})
    assert res.status_code == 200, res.text
    body = res.json()
    assert set(body.keys()) == {"used", "cap", "resets_at"}, body
    assert isinstance(body["cap"], int)


# ============================================================
# 8. Tenant fence - each session sees ONLY its own seeded rows.
# The old vertical proved this with a per-table REST route; that route is gone,
# so we prove it via /schema's session-scoped counts: two independent sessions
# each see the same fixed per-patient counts (NOT the sum), which only holds if
# RLS is filtering every table to the caller's session.
# ============================================================

async def _schema_counts(client, sid: str) -> dict[str, int]:
    res = await client.get(
        "/schema",
        headers={
            "x-api-key": settings.api_key,
            "x-session-id": sid,
        },
    )
    assert res.status_code == 200, res.text
    return {t["name"]: t["row_count"] for t in res.json()["tables"]}


async def test_session_isolation_via_schema_counts(client, feature_session):
    """Critical: the RLS / app.session_id fence must scope every table to the
    caller. Seed a second, independent session and confirm both see exactly the
    per-patient counts - if RLS leaked, a session would see the other's rows too
    and the counts would be doubled."""
    # The HTTP /session route reuses the most-recent live session per IP, so a
    # second POST from the test client would collapse to feature_session. Create
    # the second session directly to get a genuinely independent scope.
    from app.core import sessions as sessions_mod
    other_sid = str(await sessions_mod.create_session(
        vertical="healthcare", referrer_tag=None, country=None, ip="10.20.0.1"
    ))
    assert other_sid != feature_session, "expected two distinct sessions"

    a_counts = await _schema_counts(client, feature_session)
    b_counts = await _schema_counts(client, other_sid)

    # Each session is seeded with its own single patient, so both see the same
    # fixed counts (equal), yet they are independent scopes.
    assert a_counts == EXPECTED_ROW_COUNTS, f"session A counts: {a_counts}"
    assert b_counts == EXPECTED_ROW_COUNTS, f"session B counts: {b_counts}"
    assert a_counts == b_counts, (
        "RLS LEAK or seed drift: per-session counts differ across sessions"
    )


# ============================================================
# 9. Lead capture
# ============================================================

async def test_lead_capture_persists(client):
    payload = {
        "email": f"strict-test-{uuid.uuid4().hex[:8]}@example.com",
        "source": "test:full_feature",
        "notes": "automated integration test",
    }
    res = await client.post(
        "/leads",
        json=payload,
        headers={"x-api-key": settings.api_key},
    )
    assert res.status_code == 200, res.text
    assert res.json() == {"status": "captured"}


async def test_lead_capture_rejects_invalid_email(client):
    res = await client.post(
        "/leads",
        json={"email": "not-an-email"},
        headers={"x-api-key": settings.api_key},
    )
    assert res.status_code == 422
