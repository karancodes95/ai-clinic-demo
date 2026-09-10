"""Input/output cap regression tests.

These guard the two cost-control wires:
  - `settings.max_input_chars` - enforced by ChatRequest validator (422 on too-long).
  - `settings.max_output_tokens` - passed to Gemini's GenerateContentConfig
    so a single call cannot exceed the budget.

The existing test_attacks.test_chat_question_max_length_enforced asserts
the 422 behavior with a hardcoded 5000-char payload. These tests stay
correct when the cap moves (read from settings) and add coverage for
the output side, which test_attacks does not touch.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from app.config import settings


@pytest_asyncio.fixture
async def attack_session(client) -> str:
    """Fresh seeded healthcare session - limit tests don't care about content."""
    res = await client.post(
        "/session",
        params={"vertical": "healthcare"},
        headers={"x-api-key": settings.api_key},
    )
    assert res.status_code == 200, res.text
    return res.json()["session_id"]


# ============================================================
# /session - caps piggybacked on the boot call (no separate /limits).
# ============================================================

async def test_session_response_includes_caps(client):
    """First call the Flutter app makes on boot must already carry the
    caps so TextField maxLength is populated before user input."""
    res = await client.post(
        "/session",
        params={"vertical": "healthcare"},
        headers={"x-api-key": settings.api_key},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["max_input_chars"] == settings.max_input_chars
    assert body["max_output_tokens"] == settings.max_output_tokens


async def test_limits_endpoint_is_gone(client):
    """Guard against accidental reintroduction - the endpoint was removed
    in favor of folding the caps into /session. If anyone re-adds /limits,
    they should also update the Flutter client to match (don't keep both)."""
    res = await client.get("/limits")
    assert res.status_code == 404


# ============================================================
# Input cap - validator reads settings, not a hardcoded literal.
# ============================================================

async def test_input_cap_one_over_rejected(client, attack_session):
    """Validator rejects at cap + 1 char. Pairs with the +5000 case in
    test_attacks to lock both 'way over' and the exact off-by-one edge."""
    payload = "a" * (settings.max_input_chars + 1)
    res = await client.post(
        "/chat",
        json={"question": payload},
        headers={
            "x-api-key": settings.api_key,
            "x-session-id": attack_session,
        },
    )
    assert res.status_code == 422, res.text
    # Error mentions the cap so a client can show a useful message without
    # parsing pydantic internals.
    assert str(settings.max_input_chars) in res.text


# ============================================================
# Output cap - Gemini config carries max_output_tokens from settings.
# ============================================================

@pytest.mark.asyncio
async def test_gemini_complete_passes_max_output_tokens():
    """GeminiClient.complete() must forward max_output_tokens into the
    GenerateContentConfig sent to the SDK - otherwise a runaway response
    could blow past the budget regardless of any prompt instruction."""
    with patch("app.llm.gemini.genai.Client") as mock_genai:
        fake_response = MagicMock()
        fake_response.text = "ok"
        fake_response.usage_metadata = None
        fake_client = MagicMock()
        fake_client.aio.models.generate_content = AsyncMock(return_value=fake_response)
        mock_genai.return_value = fake_client

        from app.llm.gemini import GeminiClient

        c = GeminiClient(api_key="x", model="m", max_output_tokens=600)
        await c.complete("hi")

        kwargs = fake_client.aio.models.generate_content.call_args.kwargs
        assert kwargs["config"].max_output_tokens == 600


@pytest.mark.asyncio
async def test_gemini_stream_passes_max_output_tokens():
    """Same guarantee on the streaming path - this is the answer/smalltalk
    code path, so it's the one that actually emits long replies in prod."""

    class _EmptyAsyncIter:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    with patch("app.llm.gemini.genai.Client") as mock_genai:
        fake_client = MagicMock()
        fake_client.aio.models.generate_content_stream = MagicMock(
            return_value=_EmptyAsyncIter()
        )
        mock_genai.return_value = fake_client

        from app.llm.gemini import GeminiClient

        c = GeminiClient(api_key="x", model="m", max_output_tokens=600)
        async for _ in c.stream("hi"):
            pass

        kwargs = fake_client.aio.models.generate_content_stream.call_args.kwargs
        assert kwargs["config"].max_output_tokens == 600
