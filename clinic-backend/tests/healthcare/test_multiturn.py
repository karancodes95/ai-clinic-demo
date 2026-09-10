"""Multi-turn conversation: the second question references the first.

Without conversation history wired into the prompt, the second question is
unanswerable (no prior context). With history, the LLM should resolve
references like 'that visit', 'the second one', etc.
"""

from __future__ import annotations

import pytest_asyncio

from tests.healthcare.test_questions import _chat_with_retry


@pytest_asyncio.fixture
async def fresh_healthcare_session(client) -> str:
    """A fresh session per test - multi-turn semantics depend on a clean
    history, so we don't share with the module-scoped fixture."""
    from app.config import settings
    res = await client.post(
        "/session",
        params={"vertical": "healthcare"},
        headers={"x-api-key": settings.api_key},
    )
    assert res.status_code == 200, res.text
    return res.json()["session_id"]


async def test_reference_to_prior_answer(client, fresh_healthcare_session, token_totals):
    """Q1 asks for the next appointment (cardiology follow-up with Dr. Torres).
    Q2 asks the reason for 'that visit' - only resolvable via conversation
    history, since 'that visit' has no meaning without Q1's context."""
    sid = fresh_healthcare_session

    # Turn 1: establish the next-appointment context.
    sql1, ans1, u1 = await _chat_with_retry(
        client, sid, "When is my next appointment?"
    )
    # The next appointment is with Dr. Torres (deterministic seed fact); dates
    # are anchored to NOW() so we assert the stable identifier, not a date.
    assert "torres" in ans1.lower(), f"Q1 should name Dr. Torres.\nAnswer: {ans1}"

    # Turn 2: reference depends on Q1 ("that visit" = the next appointment).
    sql2, ans2, u2 = await _chat_with_retry(
        client, sid, "And what is the reason for that visit?"
    )
    # The SQL should fetch the appointment reason (resolved 'that visit' to the
    # next scheduled appointment).
    low_sql = sql2.lower()
    assert "reason" in low_sql or "appointments" in low_sql, (
        f"Q2 SQL should fetch the appointment reason.\nSQL: {sql2}"
    )
    # The answer must reference the cardiology follow-up (the next visit's reason).
    low_ans = ans2.lower()
    assert "cardiolog" in low_ans, (
        f"Q2 should answer with the cardiology follow-up reason.\n"
        f"Answer: {ans2}\nSQL: {sql2}"
    )

    # Token accounting (so the run summary still totals).
    for u in (u1, u2):
        for k in ("prompt", "completion", "total"):
            token_totals[k] = token_totals.get(k, 0) + int(u.get(k, 0) or 0)
    token_totals["rows"].append({"id": "multiturn-Q1", **u1})
    token_totals["rows"].append({"id": "multiturn-Q2", **u2})
