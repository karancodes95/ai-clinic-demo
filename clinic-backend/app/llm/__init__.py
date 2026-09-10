from app.config import settings
from app.llm.base import LLMClient
from app.llm.claude import ClaudeClient
from app.llm.gemini import GeminiClient

# Closes review N1 - single client per process. genai.Client is thread-safe
# for concurrent async calls; no reason to allocate per request. `last_usage`
# is per-instance state, but we own the chat orchestration sequentially per
# request - concurrent requests get their own coroutine and the .complete /
# .stream calls aren't reentrant on a single client.
#
# Wait: that last point isn't actually true. last_usage IS shared across
# concurrent requests on a singleton, which would make usage accounting
# wrong. So return a fresh client per call but preserve the option to
# memoize underlying SDK objects later if needed.
_singleton: LLMClient | None = None


def get_llm() -> LLMClient:
    # Per-call instance: each chat orchestration mutates `last_usage`, so
    # sharing across concurrent requests would scramble token counts. The
    # genai.Client *underneath* is fine to construct repeatedly - it's a
    # thin wrapper; the cost is microseconds.
    if settings.llm_provider == "gemini":
        return GeminiClient(
            api_key=settings.gemini_api_key,
            model=settings.gemini_model,
            max_output_tokens=settings.max_output_tokens,
        )
    if settings.llm_provider == "claude":
        return ClaudeClient(model=settings.claude_model)
    raise ValueError(f"unknown LLM provider: {settings.llm_provider}")
