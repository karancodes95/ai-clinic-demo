from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator

from google import genai
from google.genai import types
from google.genai.errors import APIError, ServerError


# Retry on transient Gemini failures (503 model overloaded, 429 throttling).
# Total max wait = 0.5 + 1.0 = 1.5s before surfacing the error to the user.
# Mirrors the test harness in tests/healthcare/test_questions.py - production
# was the only path missing it, which is how a single 503 reached the chat UI.
_RETRY_STATUS = {429, 503}
_MAX_RETRIES = 2
_BACKOFF_BASE_SECONDS = 0.5

_log = logging.getLogger("llm.gemini")


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, (ServerError, APIError)):
        code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
        if code in _RETRY_STATUS:
            return True
    msg = str(exc).lower()
    return "503" in msg or "unavailable" in msg or "throttl" in msg


def _usage_dict(meta) -> dict:
    """Extract token counts from a response's usage_metadata (best-effort)."""
    if meta is None:
        return {"prompt": 0, "completion": 0, "total": 0}
    return {
        "prompt": getattr(meta, "prompt_token_count", 0) or 0,
        "completion": getattr(meta, "candidates_token_count", 0) or 0,
        "total": getattr(meta, "total_token_count", 0) or 0,
    }


class GeminiClient:
    def __init__(
        self, *, api_key: str, model: str, max_output_tokens: int | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("GEMINI_API_KEY is empty")
        self._client = genai.Client(api_key=api_key)
        self._model = model
        # Hard ceiling on tokens emitted per call. None = no cap (legacy).
        self._max_output_tokens = max_output_tokens
        # Public identifier for the model in use - written into events for run comparison.
        self.model_id: str = f"gemini:{model}"
        # Token usage from the most recent complete()/stream() call.
        self.last_usage: dict = {"prompt": 0, "completion": 0, "total": 0}

    async def complete(self, prompt: str, *, system: str | None = None) -> str:
        for attempt in range(_MAX_RETRIES + 1):
            try:
                res = await self._client.aio.models.generate_content(
                    model=self._model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=0.0,
                        max_output_tokens=self._max_output_tokens,
                        system_instruction=system,
                    ),
                )
                self.last_usage = _usage_dict(getattr(res, "usage_metadata", None))
                return (res.text or "").strip()
            except Exception as e:
                if attempt < _MAX_RETRIES and _is_retryable(e):
                    delay = _BACKOFF_BASE_SECONDS * (2 ** attempt)
                    _log.warning("complete() retry %d after %.2fs (%s)", attempt + 1, delay, e)
                    await asyncio.sleep(delay)
                    continue
                raise
        raise RuntimeError("unreachable")

    async def stream(self, prompt: str, *, system: str | None = None) -> AsyncIterator[str]:
        # Reset; final chunk carries cumulative usage_metadata.
        self.last_usage = {"prompt": 0, "completion": 0, "total": 0}
        for attempt in range(_MAX_RETRIES + 1):
            chunks_yielded = False
            try:
                async for chunk in self._client.aio.models.generate_content_stream(
                    model=self._model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=0.2,
                        max_output_tokens=self._max_output_tokens,
                        system_instruction=system,
                    ),
                ):
                    meta = getattr(chunk, "usage_metadata", None)
                    if meta is not None:
                        self.last_usage = _usage_dict(meta)
                    if chunk.text:
                        chunks_yielded = True
                        yield chunk.text
                return
            except Exception as e:
                # Only retry if nothing was sent to the user yet - otherwise
                # the retry would duplicate text in the bubble.
                if not chunks_yielded and attempt < _MAX_RETRIES and _is_retryable(e):
                    delay = _BACKOFF_BASE_SECONDS * (2 ** attempt)
                    _log.warning("stream() retry %d after %.2fs (%s)", attempt + 1, delay, e)
                    await asyncio.sleep(delay)
                    continue
                raise
