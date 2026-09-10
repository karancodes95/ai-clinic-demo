"""Claude adapter via the Claude Agent SDK (same SDK maya_app / ask_clinic use).

Used purely as a text generator for the NL->SQL flow: ``tools=[]`` disables all
built-in tools, ``permission_mode="dontAsk"`` never prompts, and
``setting_sources=[]`` ignores filesystem CLAUDE.md/settings. The SDK returns the
model's text (the SQL, or the NL answer) - we don't run its agent tool loop.

Auth: the SDK uses the machine's Claude Code login (local dev) or
CLAUDE_CODE_OAUTH_TOKEN (headless/server). No API key.
"""

from __future__ import annotations

from typing import AsyncIterator

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
)


def _options(system: str | None, model: str | None) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        tools=[],
        allowed_tools=[],
        permission_mode="dontAsk",
        setting_sources=[],
        system_prompt=system,
        model=model or None,
    )


def _usage(msg: ResultMessage) -> dict:
    """Best-effort token counts - usage shape varies across SDK versions."""
    u = getattr(msg, "usage", None)
    if not isinstance(u, dict):
        return {"prompt": 0, "completion": 0, "total": 0}
    inp = int(u.get("input_tokens", 0) or 0)
    out = int(u.get("output_tokens", 0) or 0)
    return {"prompt": inp, "completion": out, "total": inp + out}


class ClaudeClient:
    """LLMClient adapter backed by the Claude Agent SDK."""

    def __init__(self, *, model: str | None = None) -> None:
        self._model = model or None
        self.model_id: str = f"claude:{model or 'default'}"
        self.last_usage: dict = {"prompt": 0, "completion": 0, "total": 0}

    async def complete(self, prompt: str, *, system: str | None = None) -> str:
        parts: list[str] = []
        async with ClaudeSDKClient(options=_options(system, self._model)) as client:
            await client.query(prompt)
            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            parts.append(block.text)
                elif isinstance(message, ResultMessage):
                    self.last_usage = _usage(message)
        return "".join(parts).strip()

    async def stream(self, prompt: str, *, system: str | None = None) -> AsyncIterator[str]:
        self.last_usage = {"prompt": 0, "completion": 0, "total": 0}
        async with ClaudeSDKClient(options=_options(system, self._model)) as client:
            await client.query(prompt)
            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock) and block.text:
                            yield block.text
                elif isinstance(message, ResultMessage):
                    self.last_usage = _usage(message)
