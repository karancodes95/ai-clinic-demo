"""Chat endpoint (question -> streamed answer over SSE) plus quota and history.
The heavy lifting lives in app.core.chat; this router handles the HTTP surface,
per-IP quota gating, and quota refunds on product-side errors."""

from __future__ import annotations

import logging
from typing import AsyncIterator
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app import db
from app.api.deps import require_api_key
from app.api.schemas import ChatRequest, HistoryResponse, HistoryTurn
from app.core import chat as orchestration
from app.core import history, rate_limit, sessions

router = APIRouter(tags=["chat"])


@router.post("/chat", dependencies=[Depends(require_api_key)])
async def chat_endpoint(
    req: ChatRequest,
    request: Request,
    x_session_id: UUID = Header(),
) -> StreamingResponse:
    # Per-IP daily quota gate. ASGI test transports are exempt. Real clients
    # including local browsers (127.0.0.1) are rate-limited so the cap can be
    # exercised end-to-end during dev.
    ip = rate_limit.client_ip(request)
    quota_headers: dict[str, str] = {}
    is_test = rate_limit.is_test_client(request)
    if not is_test:
        allowed, count, resets_at = await rate_limit.check_and_count(ip)
        if not allowed:
            return JSONResponse(
                status_code=429,
                content={
                    "error": "rate_limited",
                    "message": (
                        "You've reached today's demo limit "
                        f"({rate_limit.DAILY_CAP} questions). "
                        "Come back tomorrow, or drop your email below "
                        "for a personal demo of the real product."
                    ),
                    "cap": rate_limit.DAILY_CAP,
                    "count": count,
                    "resets_at": resets_at,
                },
            )
        quota_headers = {
            "X-Quota-Used": str(count),
            "X-Quota-Cap": str(rate_limit.DAILY_CAP),
            "X-Quota-Resets-At": resets_at,
        }

    await sessions.touch_session(x_session_id)

    # Wrap the SSE stream so a product-side error (LLM 503, validate, execute,
    # etc.) refunds the quota; users shouldn't lose their daily allowance to
    # transient backend failures. Out-of-scope and successful answers don't
    # emit `event: error` so they're not refunded.
    inner = orchestration.chat_stream(
        session_id=x_session_id, question=req.question, debug_mode=is_test,
    )

    async def _wrapped() -> AsyncIterator[str]:
        had_error = False
        async for chunk in inner:
            if not had_error and chunk.startswith("event: error\n"):
                had_error = True
            yield chunk
        if had_error and not is_test and ip:
            try:
                await rate_limit.refund_one(ip)
            except Exception:
                logging.getLogger("rate_limit").exception("refund_one failed")

    return StreamingResponse(
        _wrapped(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            **quota_headers,
        },
    )


@router.get("/quota", dependencies=[Depends(require_api_key)])
async def quota_endpoint(request: Request) -> dict:
    if rate_limit.is_test_client(request):
        return {"used": 0, "cap": rate_limit.DAILY_CAP, "resets_at": None}
    ip = rate_limit.client_ip(request)
    return await rate_limit.get_quota(ip)


@router.get(
    "/history",
    response_model=HistoryResponse,
    dependencies=[Depends(require_api_key)],
)
async def get_history(
    x_session_id: UUID = Header(),
    limit: int = Query(default=50, ge=1, le=200),
) -> HistoryResponse:
    async with db.pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT vertical FROM sessions "
            "WHERE id = $1 AND expires_at > NOW()",
            x_session_id,
        )
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "session_not_found", "message": "session missing or expired"},
        )
    await sessions.touch_session(x_session_id)
    turns = await history.recent_turns(x_session_id, limit=limit)
    return HistoryResponse(
        vertical=row["vertical"],
        turns=[HistoryTurn(**t) for t in turns],
    )
