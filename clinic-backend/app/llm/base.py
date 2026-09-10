from __future__ import annotations

from typing import AsyncIterator, Protocol


class LLMClient(Protocol):
    """Provider-agnostic LLM interface. Feature code must use this only -
    never import openai/google-genai/anthropic outside an adapter file."""

    # Public identifier for the model in use, e.g. "gemini:gemini-2.5-pro".
    model_id: str

    # Tokens used by the most recent complete()/stream() call:
    # {"prompt": int, "completion": int, "total": int}.
    last_usage: dict

    async def complete(self, prompt: str, *, system: str | None = None) -> str:
        """Return a complete response (used for SQL generation step).

        ``system`` is passed as a provider system instruction (rules/persona),
        which models follow more strongly than rules embedded in ``prompt``."""
        ...

    async def stream(self, prompt: str, *, system: str | None = None) -> AsyncIterator[str]:
        """Yield text chunks as they arrive (used for NL answer step)."""
        ...
