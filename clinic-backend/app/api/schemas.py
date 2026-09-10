"""Request/response models for the API routers. Pydantic validates at the
boundary; the chat input cap is the trusted gate against token-bombing."""

from __future__ import annotations

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.config import settings


class SessionResponse(BaseModel):
    session_id: str
    vertical: str
    # Caps are folded into the session response so the client gets them on its
    # very first request, without a separate /limits round trip. They mirror
    # the server's enforced limits and are treated by the client as UX hints.
    max_input_chars: int
    max_output_tokens: int


class ChatRequest(BaseModel):
    # The validator reads the live setting so config and the client's maxLength
    # share one source of truth; bumping settings.max_input_chars updates the
    # server-side gate without a code change here.
    question: str = Field(min_length=1)

    @field_validator("question")
    @classmethod
    def _within_input_cap(cls, v: str) -> str:
        cap = settings.max_input_chars
        if len(v) > cap:
            raise ValueError(f"question exceeds max length of {cap} characters")
        return v


class HistoryTurn(BaseModel):
    question: str
    answer: str


class HistoryResponse(BaseModel):
    vertical: str
    turns: list[HistoryTurn]


class LeadRequest(BaseModel):
    email: EmailStr
    name: str | None = Field(default=None, max_length=120)
    notes: str | None = Field(default=None, max_length=1000)
    source: str | None = Field(default=None, max_length=100)


class PublicLeadRequest(BaseModel):
    email: EmailStr
    name: str | None = Field(default=None, max_length=120)
    notes: str | None = Field(default=None, max_length=2000)
    source: str | None = Field(default=None, max_length=100)
    # Cloudflare Turnstile widget token; the server verifies it with Cloudflare
    # before trusting the submission. Forged or replayed tokens are rejected.
    turnstile_token: str = Field(min_length=1, max_length=4096)
    # Honeypot: invisible in the rendered form, must come back empty. Any
    # non-empty value is a bot; the endpoint 200s and silently drops it.
    company_website: str | None = Field(default=None, max_length=200)
