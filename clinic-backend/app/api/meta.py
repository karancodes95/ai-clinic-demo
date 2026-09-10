"""Liveness and build-info endpoints (public, no auth)."""

from __future__ import annotations

from fastapi import APIRouter

from app.config import settings

router = APIRouter(tags=["meta"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/version")
async def version() -> dict[str, str]:
    """Identify the deployed instance by commit/tag."""
    return {"version": settings.app_version}
