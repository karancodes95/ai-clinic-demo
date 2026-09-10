"""Session lifecycle + the schema pane. The schema view runs inside a
read-only transaction with the app.session_id GUC set, so row-level security
filters to the caller's session. Per-table data views are intentionally not
exposed: this vertical is patient-scoped (one patient per session) and the UI
reads everything through /chat and /schema."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request

from app import db
from app.api.deps import require_api_key
from app.api.schemas import SessionResponse
from app.config import settings
from app.core import rate_limit, sessions
from app.vertical.healthcare import prompt as healthcare_prompt

router = APIRouter(tags=["catalog"])


def _session_payload(session_id: str, vertical: str) -> dict[str, str | int]:
    """Shape matches SessionResponse; the caps are piggybacked so the client
    never needs a separate /limits round trip."""
    return {
        "session_id": session_id,
        "vertical": vertical,
        "max_input_chars": settings.max_input_chars,
        "max_output_tokens": settings.max_output_tokens,
    }


def _prompt_module(vertical: str):
    return healthcare_prompt


def _jsonable(v):
    from decimal import Decimal
    if v is None:
        return None
    if hasattr(v, "isoformat"):
        return v.isoformat()
    if isinstance(v, UUID):
        return str(v)
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (dict, list)):
        return v
    if isinstance(v, str):
        # asyncpg may return JSONB as str; leave it, the client parses.
        return v
    return v


@router.post(
    "/session",
    dependencies=[Depends(require_api_key)],
    response_model=SessionResponse,
)
async def create_session(
    request: Request,
    # Bound free-form metadata so an attacker can't store unbounded strings.
    vertical: str | None = Query(default=None, max_length=32),
    referrer_tag: str | None = Query(default=None, max_length=100),
    country: str | None = Query(default=None, max_length=8),
) -> dict[str, str]:
    v = vertical or settings.vertical
    if v != "healthcare":
        raise HTTPException(status_code=400, detail=f"unknown vertical: {v}")
    ip = rate_limit.client_ip(request)
    # Reuse an existing non-expired session for this (ip, vertical) so the chat
    # survives across tabs / devices on the same network. Privacy trade-off:
    # anyone on the same IP shares the thread.
    existing = await sessions.get_active_for_ip(ip=ip, vertical=v)
    if existing is not None:
        await sessions.touch_session(existing)
        return _session_payload(str(existing), v)
    session_id = await sessions.create_session(
        vertical=v,
        referrer_tag=referrer_tag,
        country=country,
        ip=ip,
    )
    return _session_payload(str(session_id), v)


@router.get("/schema", dependencies=[Depends(require_api_key)])
async def get_schema(x_session_id: UUID = Header()) -> dict:
    """Schema view for the UI: per-table columns + session-scoped row counts +
    3 sample rows + sample prompts. Single source of truth shared by the LLM
    (via prompt) and the UI (via this endpoint)."""
    vertical = await sessions.get_vertical(x_session_id) or settings.vertical
    vp = _prompt_module(vertical)
    tables = list(getattr(vp, "TABLES", []))
    sample_prompts = list(getattr(vp, "SAMPLE_PROMPTS", []))

    out_tables: list[dict] = []
    async with db.pool().acquire() as conn:
        async with conn.transaction(readonly=True):
            await conn.execute(
                "SELECT set_config('app.session_id', $1, true)",
                str(x_session_id),
            )
            for t in tables:
                cols = await conn.fetch(
                    """
                    SELECT column_name, data_type
                    FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = $1
                    ORDER BY ordinal_position
                    """,
                    t,
                )
                # Skip the tenant column from the user-facing column list.
                columns = [
                    {"name": c["column_name"], "type": c["data_type"]}
                    for c in cols
                    if c["column_name"] != "session_id"
                ]
                count_row = await conn.fetchrow(f"SELECT COUNT(*) AS c FROM {t}")
                row_count = int(count_row["c"])
                sample_rows_raw = await conn.fetch(f"SELECT * FROM {t} LIMIT 3")
                sample_rows = [
                    {
                        k: _jsonable(v)
                        for k, v in dict(r).items()
                        if k != "session_id"
                    }
                    for r in sample_rows_raw
                ]
                out_tables.append({
                    "name": t,
                    "columns": columns,
                    "row_count": row_count,
                    "sample_rows": sample_rows,
                })

    return {
        "vertical": vertical,
        "tables": out_tables,
        "sample_prompts": sample_prompts,
    }
