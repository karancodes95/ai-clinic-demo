from __future__ import annotations

import json
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app import db
from app.api.admin_auth import verify_admin

router = APIRouter(prefix="/admin", tags=["admin"])


class Metrics(BaseModel):
    # Field names retain the "_today" suffix for back-compat with the archived
    # React admin. When scope=all is requested they carry all-time values; the
    # `scope` field below tells clients which is in the payload.
    scope: str  # "today" | "all"
    sessions_today: int
    messages_today: int
    active_sessions: int  # always last 15 min (live ops, scope-independent)
    errors_24h: int  # always last 24h (live ops, scope-independent)
    unique_visitors_today: int
    engaged_visitors_today: int
    bounce_rate_today: float  # 0..1, computed over unique IPs
    avg_messages_per_engaged: float


class SessionRow(BaseModel):
    id: str
    created_at: str
    last_active: str
    vertical: str
    ip: str | None = None
    referrer_tag: str | None = None
    country: str | None = None
    events: int
    messages: int


@router.get("/metrics", response_model=Metrics, dependencies=[Depends(verify_admin)])
async def metrics(
    scope: str = Query(default="today", pattern="^(today|all)$"),
) -> Metrics:
    """KPIs for the admin dashboard.

    scope="today" (default): metrics are filtered to today's activity.
      Visitor = distinct IP with any activity today (session created today
      OR any event timestamped today). Returning visitors whose session was
      created on a prior day are still counted, because the backend reuses
      sessions per IP for 24h.

    scope="all": same metrics computed across the entire retained history
      (sessions and events are kept forever per data-retention.md). The
      `scope` field in the response tells the client which is in the payload.

    `active_sessions` (last 15 min) and `errors_24h` (last 24h) are always
    windowed regardless of scope - they're live-ops signals, not totals.
    """
    if scope == "today":
        ip_filter = (
            "AND (s.created_at >= date_trunc('day', NOW()) "
            "     OR e.ts        >= date_trunc('day', NOW()))"
        )
        msg_filter = "AND e.ts >= date_trunc('day', NOW())"
        sessions_filter = "WHERE created_at >= date_trunc('day', NOW())"
        events_filter = "AND ts >= date_trunc('day', NOW())"
    else:
        ip_filter = ""
        msg_filter = ""
        sessions_filter = ""
        events_filter = ""

    sql = f"""
        WITH visitor_ips AS (
          SELECT DISTINCT s.ip
          FROM sessions s
          LEFT JOIN events e ON e.session_id = s.id
          WHERE s.ip IS NOT NULL
            {ip_filter}
        ),
        msgs_by_ip AS (
          SELECT s.ip, COUNT(*)::int AS msgs
          FROM events e
          JOIN sessions s ON s.id = e.session_id
          WHERE e.type = 'question_asked'
            AND s.ip IS NOT NULL
            {msg_filter}
          GROUP BY s.ip
        )
        SELECT
          (SELECT COUNT(*) FROM sessions {sessions_filter}) AS sessions_today,
          (SELECT COUNT(*) FROM events
             WHERE type = 'question_asked' {events_filter}) AS messages_today,
          (SELECT COUNT(*) FROM visitor_ips) AS unique_visitors_today,
          (SELECT COUNT(*) FROM msgs_by_ip) AS engaged_visitors_today,
          (SELECT COALESCE(AVG(msgs)::float, 0) FROM msgs_by_ip)
            AS avg_messages_per_engaged,
          (SELECT COUNT(*) FROM sessions
             WHERE last_active >= NOW() - INTERVAL '15 minutes'
               AND expires_at > NOW()) AS active_sessions,
          (SELECT COUNT(*) FROM events
             WHERE type = 'error'
               AND ts >= NOW() - INTERVAL '24 hours') AS errors_24h
    """
    async with db.pool().acquire() as conn:
        row = await conn.fetchrow(sql)

    visitors = row["unique_visitors_today"] or 0
    engaged = row["engaged_visitors_today"] or 0
    bounce_rate = (1.0 - (engaged / visitors)) if visitors > 0 else 0.0

    return Metrics(
        scope=scope,
        sessions_today=row["sessions_today"] or 0,
        messages_today=row["messages_today"] or 0,
        active_sessions=row["active_sessions"] or 0,
        errors_24h=row["errors_24h"] or 0,
        unique_visitors_today=visitors,
        engaged_visitors_today=engaged,
        bounce_rate_today=round(bounce_rate, 4),
        avg_messages_per_engaged=round(row["avg_messages_per_engaged"] or 0.0, 2),
    )


@router.get(
    "/sessions",
    response_model=list[SessionRow],
    dependencies=[Depends(verify_admin)],
)
async def list_sessions(
    limit: int = Query(default=500, ge=1, le=500),
    since: str = Query(default="all", pattern="^(all|today|24h|7d)$"),
) -> list[SessionRow]:
    # "Today" / "24h" / "7d" mean *active in that window*, not *created in*.
    # With IP-based session dedup, returning visitors keep a session row
    # created on a prior day, so filtering by created_at hides them from
    # the live ops view even when they're actively chatting right now.
    # "all" returns every session (capped by `limit`).
    where = {
        "all": "TRUE",
        "today": "(s.created_at >= date_trunc('day', NOW())"
                 " OR s.last_active >= date_trunc('day', NOW()))",
        "24h": "(s.created_at >= NOW() - INTERVAL '24 hours'"
               " OR s.last_active >= NOW() - INTERVAL '24 hours')",
        "7d": "(s.created_at >= NOW() - INTERVAL '7 days'"
              " OR s.last_active >= NOW() - INTERVAL '7 days')",
    }[since]

    async with db.pool().acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT
              s.id,
              s.created_at,
              s.last_active,
              s.vertical,
              s.ip,
              s.referrer_tag,
              s.country,
              COUNT(e.id) AS events,
              COUNT(e.id) FILTER (WHERE e.type = 'question_asked') AS messages
            FROM sessions s
            LEFT JOIN events e ON e.session_id = s.id
            WHERE {where}
            GROUP BY s.id
            ORDER BY s.last_active DESC
            LIMIT $1
            """,
            limit,
        )
    return [
        SessionRow(
            id=str(r["id"]),
            created_at=r["created_at"].isoformat(),
            last_active=r["last_active"].isoformat(),
            vertical=r["vertical"],
            ip=str(r["ip"]) if r["ip"] is not None else None,
            referrer_tag=r["referrer_tag"],
            country=r["country"],
            events=r["events"],
            messages=r["messages"],
        )
        for r in rows
    ]


class TranscriptTurn(BaseModel):
    ts: str
    role: str  # "user" | "assistant" | "error"
    text: str
    stage: str | None = None


class SessionDetail(BaseModel):
    id: str
    created_at: str
    last_active: str
    expires_at: str
    vertical: str
    ip: str | None = None
    referrer_tag: str | None = None
    country: str | None = None
    turns: list[TranscriptTurn]


@router.get(
    "/sessions/{session_id}",
    response_model=SessionDetail,
    dependencies=[Depends(verify_admin)],
)
async def session_detail(session_id: UUID) -> SessionDetail:
    async with db.pool().acquire() as conn:
        s = await conn.fetchrow(
            "SELECT id, created_at, last_active, expires_at, vertical, "
            "       ip, referrer_tag, country "
            "FROM sessions WHERE id = $1",
            session_id,
        )
        if s is None:
            raise HTTPException(
                status_code=404,
                detail={"error": "session_not_found", "message": "session not found"},
            )
        rows = await conn.fetch(
            "SELECT ts, type, payload "
            "FROM events "
            "WHERE session_id = $1 "
            "  AND type IN ('question_asked', 'answer_streamed', 'error') "
            "ORDER BY ts ASC, id ASC",
            session_id,
        )

    turns: list[TranscriptTurn] = []
    for r in rows:
        raw = r["payload"]
        p = json.loads(raw) if isinstance(raw, str) else (raw or {})
        ts = r["ts"].isoformat()
        if r["type"] == "question_asked":
            turns.append(TranscriptTurn(ts=ts, role="user", text=p.get("text", "")))
        elif r["type"] == "answer_streamed":
            turns.append(
                TranscriptTurn(ts=ts, role="assistant", text=p.get("text", ""))
            )
        elif r["type"] == "error":
            turns.append(
                TranscriptTurn(
                    ts=ts,
                    role="error",
                    text=p.get("message", ""),
                    stage=p.get("stage"),
                )
            )

    return SessionDetail(
        id=str(s["id"]),
        created_at=s["created_at"].isoformat(),
        last_active=s["last_active"].isoformat(),
        expires_at=s["expires_at"].isoformat(),
        vertical=s["vertical"],
        ip=str(s["ip"]) if s["ip"] is not None else None,
        referrer_tag=s["referrer_tag"],
        country=s["country"],
        turns=turns,
    )


class LeadRow(BaseModel):
    id: int
    email: str
    name: str | None = None
    ip: str | None = None
    source: str | None = None
    notes: str | None = None
    captured_at: str
    slack_status: str | None = None


@router.get(
    "/leads",
    response_model=list[LeadRow],
    dependencies=[Depends(verify_admin)],
)
async def list_leads(
    limit: int = Query(default=100, ge=1, le=500),
    since: str = Query(default="all", pattern="^(today|24h|7d|all)$"),
) -> list[LeadRow]:
    where = {
        "today": "WHERE captured_at >= date_trunc('day', NOW())",
        "24h": "WHERE captured_at >= NOW() - INTERVAL '24 hours'",
        "7d": "WHERE captured_at >= NOW() - INTERVAL '7 days'",
        "all": "",
    }[since]

    async with db.pool().acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT id, email, name, ip, source, notes, captured_at, slack_status
            FROM leads
            {where}
            ORDER BY captured_at DESC
            LIMIT $1
            """,
            limit,
        )
    return [
        LeadRow(
            id=r["id"],
            email=r["email"],
            name=r["name"],
            ip=str(r["ip"]) if r["ip"] is not None else None,
            source=r["source"],
            notes=r["notes"],
            captured_at=r["captured_at"].isoformat(),
            slack_status=r["slack_status"],
        )
        for r in rows
    ]
