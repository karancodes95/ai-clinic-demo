"""Shared FastAPI dependencies for the API routers."""

from __future__ import annotations

from secrets import compare_digest

from fastapi import Header, HTTPException

from app.config import settings


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """Gate the chat + data endpoints behind the static demo API key. A custom
    exception handler in app.main normalizes the 401 to the {error, message}
    contract. Constant-time compare avoids leaking the key via timing."""
    if not compare_digest(x_api_key or "", settings.api_key):
        raise HTTPException(status_code=401, detail="invalid or missing API key")
