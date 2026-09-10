"""Per-IP rate limiting for the demo: 15 questions per rolling 24h.

Rolling means the 24h timer starts at the IP's first question of the current
batch - not at midnight. So an IP that hits the cap at 14:00 today is unblocked
at 14:00 tomorrow, not at 00:00 - preventing midnight-burst gaming.

Usage: call `check_and_count(ip)` before serving a chat request. If it returns
(False, count), reject with 429. If True, the request is recorded and may proceed.
"""

from __future__ import annotations

import ipaddress
from datetime import datetime, timedelta, timezone

from fastapi import Request

from app.db import pool

DAILY_CAP = 15
WINDOW_HOURS = 24

# Separate cap for the lead-capture endpoint. Spam surface is different from
# chat (no LLM cost, but DB pollution); 5/day per IP is plenty for a real
# prospect to retry a typo and still bound abuse.
LEADS_DAILY_CAP = 5


def _trusted_proxy_networks() -> list[ipaddress._BaseNetwork]:
    """Parse settings.trusted_proxies (CSV of CIDR / IP) into a network list.
    Cached on first call via module-level memoization."""
    global _trusted_networks_cache
    if _trusted_networks_cache is not None:
        return _trusted_networks_cache
    from app.config import settings as _s
    out: list[ipaddress._BaseNetwork] = []
    if _s.trusted_proxies:
        for entry in _s.trusted_proxies.split(","):
            entry = entry.strip()
            if not entry:
                continue
            try:
                out.append(ipaddress.ip_network(entry, strict=False))
            except ValueError:
                continue
    _trusted_networks_cache = out
    return out


_trusted_networks_cache: list | None = None


def is_test_client(request: Request) -> bool:
    """True when the request should bypass the per-IP daily cap. Two paths:

    1. ASGI in-process tests (httpx ASGITransport sets host="testclient").
    2. HTTP integration tests sending header `X-Test-Mode: <test_mode_key>`,
       where `test_mode_key` is a long random secret in .env. Empty key
       disables this path entirely (production safe)."""
    if request.client is not None and request.client.host == "testclient":
        return True
    # Local import to avoid a circular dep with config at module load.
    from app.config import settings as _s
    if _s.test_mode_key:
        if request.headers.get("x-test-mode") == _s.test_mode_key:
            return True
    return False


def _peer_is_trusted(request: Request) -> bool:
    """True when the immediate socket peer is a trusted proxy, so its X-Real-IP
    header can be believed. If trusted_proxies is configured, the peer must be in
    it; otherwise we trust only a loopback peer (the common nginx-on-localhost
    case). A directly-connected client is never trusted and so cannot forge
    X-Real-IP to evade the per-IP caps."""
    peer = request.client.host if request.client is not None else None
    if peer is None:
        return False
    try:
        addr = ipaddress.ip_address(peer)
    except ValueError:
        return False
    nets = _trusted_proxy_networks()
    if nets:
        return any(addr in n for n in nets)
    return addr.is_loopback


def client_ip(request: Request) -> str:
    """Resolve the real visitor IP. Production topology:

        client -> Cloudflare -> nginx -> uvicorn (127.0.0.1, --proxy-headers)

    X-Real-IP is trusted ONLY when the socket peer is a trusted proxy
    (see _peer_is_trusted); a direct client cannot forge it. X-Forwarded-For is
    never read (its leftmost entry is attacker-controlled). Non-parseable hosts
    normalize to 0.0.0.0 so Postgres `inet` accepts the value."""
    if _peer_is_trusted(request):
        real_ip = (request.headers.get("x-real-ip") or "").strip()
        if real_ip:
            try:
                ipaddress.ip_address(real_ip)
                return real_ip
            except ValueError:
                # Header present but garbage: fall through to socket peer rather
                # than "0.0.0.0" so malformed clients don't share one bucket.
                pass

    socket_host = request.client.host if request.client is not None else "0.0.0.0"
    try:
        ipaddress.ip_address(socket_host)
        return socket_host
    except ValueError:
        return "0.0.0.0"


def _resets_at_iso(window_start: datetime) -> str:
    """ISO-8601 UTC timestamp of when the IP's current 24h window rolls over."""
    return (window_start + timedelta(hours=WINDOW_HOURS)).astimezone(timezone.utc).isoformat()


async def check_and_count(ip: str) -> tuple[bool, int, str]:
    """Atomically: roll the window if expired, increment the count, decide
    allowed/blocked. Returns (allowed, count_after_increment, resets_at_iso).

    Closes review M4 - uses SELECT ... FOR UPDATE row lock + an idempotent
    pre-insert so two concurrent requests from the same IP cannot both pass
    when the pre-state was already at the cap.

    If allowed=False, count is at the cap and the caller should 429 without
    consuming an LLM call. `resets_at_iso` is the UTC timestamp the cap
    clears - exposed so the UI can tell the user when to come back.
    """
    async with pool().acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO ip_quota (ip, day_count, day_window_start, last_seen) "
                "VALUES ($1::inet, 0, NOW(), NOW()) "
                "ON CONFLICT (ip) DO NOTHING",
                ip,
            )
            row = await conn.fetchrow(
                "SELECT day_count, day_window_start FROM ip_quota "
                "WHERE ip = $1::inet FOR UPDATE",
                ip,
            )
            window_expired = await conn.fetchval(
                "SELECT NOW() - $1 > $2 * INTERVAL '1 hour'",
                row["day_window_start"], WINDOW_HOURS,
            )
            if window_expired:
                new_start = await conn.fetchval(
                    "UPDATE ip_quota SET day_count = 1, "
                    "day_window_start = NOW(), last_seen = NOW() "
                    "WHERE ip = $1::inet RETURNING day_window_start",
                    ip,
                )
                return (True, 1, _resets_at_iso(new_start))

            current = int(row["day_count"])
            if current >= DAILY_CAP:
                await conn.execute(
                    "UPDATE ip_quota SET last_seen = NOW() WHERE ip = $1::inet",
                    ip,
                )
                return (False, current, _resets_at_iso(row["day_window_start"]))

            await conn.execute(
                "UPDATE ip_quota SET day_count = day_count + 1, "
                "last_seen = NOW() WHERE ip = $1::inet",
                ip,
            )
            return (True, current + 1, _resets_at_iso(row["day_window_start"]))


async def refund_one(ip: str) -> None:
    """Decrement an IP's day_count by 1, floored at 0. Called by the chat
    route after a product-side error (LLM 503, validate fail, execute fail,
    etc.) so transient failures don't burn the user's daily allowance.

    User-driven outcomes - out-of-scope (including greetings, since smalltalk
    was merged into OOS) and successful answers - are NOT refunded. Only the
    failure modes the user can't do anything about.
    """
    async with pool().acquire() as conn:
        await conn.execute(
            "UPDATE ip_quota SET day_count = GREATEST(day_count - 1, 0) "
            "WHERE ip = $1::inet",
            ip,
        )


async def get_quota(ip: str) -> dict:
    """Read-only quota snapshot for /quota. Does NOT increment.

    Returns: {used, cap, resets_at}. `resets_at` is null when no window has
    started for this IP, or when the window has expired (next /chat will
    start a fresh one). UI uses this on app load to seed the counter
    before the first question is asked.
    """
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT day_count, day_window_start FROM ip_quota WHERE ip = $1::inet",
            ip,
        )
        if row is None:
            return {"used": 0, "cap": DAILY_CAP, "resets_at": None}
        window_start = row["day_window_start"]
        if (datetime.now(timezone.utc) - window_start) >= timedelta(hours=WINDOW_HOURS):
            return {"used": 0, "cap": DAILY_CAP, "resets_at": None}
        return {
            "used": int(row["day_count"]),
            "cap": DAILY_CAP,
            "resets_at": _resets_at_iso(window_start),
        }


async def check_and_count_leads(ip: str) -> tuple[bool, int]:
    """Closes I31 - per-IP daily cap on /leads to bound spam.

    Counts leads captured from this IP in the last 24h directly from the
    `leads` table (avoids a new column / migration). Approximate but
    bounded - the race condition window is tiny vs. spam scale.
    """
    async with pool().acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM leads "
            "WHERE ip = $1::inet AND captured_at > NOW() - $2::interval",
            ip, timedelta(hours=WINDOW_HOURS),
        )
        current = int(count or 0)
        if current >= LEADS_DAILY_CAP:
            return (False, current)
        return (True, current + 1)
