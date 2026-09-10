"""Shared fixtures for the vertical test suites.

Runs the FastAPI app in-process via httpx.ASGITransport - no separate
uvicorn needed. The Postgres pool is initialised/torn down per-session.
"""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

from app import db
from app.config import settings
from app.main import app


@pytest_asyncio.fixture(scope="session")
async def _pool():
    await db.init_pool()
    yield
    await db.close_pool()


@pytest_asyncio.fixture(scope="session")
async def client(_pool):
    # `client=("testclient", 50000)` so app-level rate-limit bypass
    # (`if ip != "testclient"` in main.py) fires under ASGITransport too.
    # Without this, tests share the dev browser's 15-question/day cap.
    transport = ASGITransport(app=app, client=("testclient", 50000))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture(scope="session")
def token_totals():
    """Accumulator for per-question token usage. Tests append rows; the
    finalizer prints a summary at session end."""
    totals: dict = {"prompt": 0, "completion": 0, "total": 0, "rows": []}
    yield totals
    rows = totals["rows"]
    if not rows:
        return
    sid = totals.get("session_id")
    if sid:
        print(f"\n\n=== Test session_id: {sid} ===")
    print("\n=== Token usage (Gemini) ===")
    print(f"{'id':<10} {'prompt':>8} {'completion':>11} {'total':>8}")
    for r in rows:
        print(f"{r['id']:<10} {r['prompt']:>8} {r['completion']:>11} {r['total']:>8}")
    print(f"{'TOTAL':<10} {totals['prompt']:>8} {totals['completion']:>11} {totals['total']:>8}")
    n = len(rows)
    print(f"avg/q       {totals['prompt']//n:>8} {totals['completion']//n:>11} {totals['total']//n:>8}")


@pytest_asyncio.fixture(scope="module")
async def healthcare_session(client, token_totals) -> str:
    """One seeded healthcare session shared across all questions in a module.
    Chat is read-only so sharing is safe and saves ~20 re-seeds per run.

    Prints the session_id so you can later query events by it:
        SELECT name, payload FROM events WHERE session_id = '<uuid>' ORDER BY created_at;
    """
    res = await client.post(
        "/session",
        params={"vertical": "healthcare"},
        headers={"x-api-key": settings.api_key},
    )
    assert res.status_code == 200, res.text
    sid = res.json()["session_id"]
    token_totals["session_id"] = sid
    print(f"\n=== Test session_id: {sid} ===")
    return sid
