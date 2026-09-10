from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from app.core import events
from app.db import pool
from app.vertical.healthcare import seed as healthcare_seed

SESSION_TTL = timedelta(hours=24)


async def get_active_for_ip(*, ip: str, vertical: str) -> UUID | None:
    """Return the most-recent non-expired session for this (ip, vertical)
    pair, or None. Used so the client can resume a chat across browsers /
    devices on the same network without per-tab localStorage."""
    if not ip:
        return None
    async with pool().acquire() as conn:
        return await conn.fetchval(
            """
            SELECT id FROM sessions
            WHERE ip = $1::inet
              AND vertical = $2
              AND expires_at > NOW()
            ORDER BY last_active DESC
            LIMIT 1
            """,
            ip, vertical,
        )


async def create_session(
    *,
    vertical: str,
    referrer_tag: str | None,
    country: str | None,
    ip: str | None = None,
) -> UUID:
    session_id = uuid4()
    expires_at = datetime.now(timezone.utc) + SESSION_TTL

    async with pool().acquire() as conn:
        async with conn.transaction():
            # set GUC so the RLS WITH CHECK clause passes for the seed inserts
            await conn.execute(
                "SELECT set_config('app.session_id', $1, true)",
                str(session_id),
            )
            await conn.execute(
                """
                INSERT INTO sessions
                    (id, vertical, referrer_tag, country, expires_at, ip)
                VALUES ($1, $2, $3, $4, $5, $6::inet)
                """,
                session_id, vertical, referrer_tag, country, expires_at, ip,
            )
            seeded_rows = await healthcare_seed.seed(conn, session_id)
    await events.log(session_id, "session_created", {
        "vertical": vertical,
        "referrer_tag": referrer_tag,
        "country": country,
        "seeded_rows": seeded_rows,
    })
    return session_id


async def touch_session(session_id: UUID) -> None:
    async with pool().acquire() as conn:
        await conn.execute(
            "UPDATE sessions SET last_active = NOW() WHERE id = $1",
            session_id,
        )


async def get_vertical(session_id: UUID) -> str | None:
    async with pool().acquire() as conn:
        return await conn.fetchval(
            "SELECT vertical FROM sessions WHERE id = $1",
            session_id,
        )


async def cleanup_expired() -> int:
    async with pool().acquire() as conn:
        result = await conn.execute(
            "DELETE FROM sessions WHERE expires_at < NOW()"
        )
        # result is like "DELETE 3"
        return int(result.split()[-1]) if result else 0
