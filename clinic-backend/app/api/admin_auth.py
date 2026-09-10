"""Bearer-token auth for the admin panel.

Replaces the bake-the-key-into-the-client model used by the (archived) React
admin. The Flutter admin (mobile browser + Android APK) cannot safely embed
a static admin key - anyone with the bundle can extract it. So we add:

    POST /admin/login {password}          → {token, expires_at}
    All /admin/* accept either header:
      Authorization: Bearer <jwt>         ← new path (Flutter admin)
      X-Admin-Key: <key>                  ← legacy path (curl, CI, archived React)

The token is a stateless HMAC-signed JWT (HS256). Stateless because the only
"user" is the operator; per-token revocation isn't needed (rotate
ADMIN_JWT_SECRET to invalidate every live session at once).

Login is rate-limited in-process - 5 attempts / 15 min / IP - to bound
brute-force without adding a DB table or Redis dep.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from secrets import compare_digest
from typing import Annotated

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHashError
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.config import settings
from app.core.rate_limit import client_ip

router = APIRouter(prefix="/admin", tags=["admin"])

_JWT_ALG = "HS256"

# In-process login rate limit. Keyed by IP, value is a deque of recent attempt
# timestamps (Unix seconds). Resets on process restart - acceptable for a
# brute-force throttle since argon2 verification is already ~100ms each.
_LOGIN_WINDOW_SECONDS = 15 * 60  # 15 minutes
_LOGIN_MAX_ATTEMPTS = 5
_login_attempts: dict[str, deque[float]] = defaultdict(deque)

# Single shared hasher with library defaults (argon2id, ~50ms on modern HW).
_ph = PasswordHasher()


def _prune_attempts(ip: str, now: float) -> None:
    """Drop attempt timestamps older than the rolling window."""
    bucket = _login_attempts[ip]
    cutoff = now - _LOGIN_WINDOW_SECONDS
    while bucket and bucket[0] < cutoff:
        bucket.popleft()


def _check_login_rate_limit(ip: str) -> None:
    """Reject the login attempt if this IP has tried too many times recently."""
    now = time.time()
    _prune_attempts(ip, now)
    if len(_login_attempts[ip]) >= _LOGIN_MAX_ATTEMPTS:
        raise HTTPException(
            status_code=429,
            detail={
                "error": "too_many_login_attempts",
                "message": (
                    f"Too many failed logins. Try again in "
                    f"{_LOGIN_WINDOW_SECONDS // 60} min."
                ),
            },
        )


def _record_login_attempt(ip: str) -> None:
    """Append a timestamp. Caller decides whether to clear on success."""
    _login_attempts[ip].append(time.time())


def _clear_login_attempts(ip: str) -> None:
    """Successful login clears the IP's failure bucket."""
    _login_attempts.pop(ip, None)


def _issue_token() -> tuple[str, int]:
    """Mint a new admin JWT. Returns (token, expires_at_unix)."""
    if not settings.admin_jwt_secret:
        raise HTTPException(
            status_code=500,
            detail={
                "error": "auth_misconfigured",
                "message": "ADMIN_JWT_SECRET not set in backend .env",
            },
        )
    iat = int(time.time())
    exp = iat + settings.admin_session_ttl_hours * 3600
    token = jwt.encode(
        {"iat": iat, "exp": exp, "scope": "admin"},
        settings.admin_jwt_secret,
        algorithm=_JWT_ALG,
    )
    return token, exp


def _decode_token(token: str) -> dict:
    """Decode + verify a bearer token. Raises HTTPException(401) on any failure."""
    if not settings.admin_jwt_secret:
        raise HTTPException(status_code=401, detail={"error": "unauthorized"})
    try:
        return jwt.decode(
            token, settings.admin_jwt_secret, algorithms=[_JWT_ALG],
            options={"require": ["exp", "iat"]},
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=401,
            detail={"error": "token_expired", "message": "session expired"},
        )
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=401,
            detail={"error": "unauthorized", "message": "invalid token"},
        )


def verify_admin(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    x_admin_key: Annotated[str | None, Header()] = None,
) -> None:
    """Dependency that gates every /admin/* endpoint.

    Accepts either:
      • Authorization: Bearer <jwt>  - issued by /admin/login
      • X-Admin-Key: <key>           - back-compat static key (settings.admin_api_key).
                                        Distinct from settings.api_key (chat gate)
                                        so a leak of the chat key cannot escalate.

    Both paths grant full admin. The bearer path is preferred for the Flutter
    admin (no embedded credential). The key path is kept for curl, CI, and
    the archived React admin - disabled entirely when ADMIN_API_KEY is unset.
    """
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer" and value:
            payload = _decode_token(value)
            if payload.get("scope") != "admin":
                raise HTTPException(
                    status_code=401,
                    detail={"error": "unauthorized", "message": "invalid token scope"},
                )
            return
    if (
        x_admin_key
        and settings.admin_api_key
        and compare_digest(x_admin_key, settings.admin_api_key)
    ):
        return
    raise HTTPException(
        status_code=401,
        detail={"error": "unauthorized", "message": "missing or invalid credentials"},
    )


class LoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class LoginResponse(BaseModel):
    token: str
    expires_at: int  # Unix seconds


@router.post("/login", response_model=LoginResponse)
async def login(req: LoginRequest, request: Request) -> LoginResponse:
    ip = client_ip(request)
    _check_login_rate_limit(ip)

    if not settings.admin_password_hash:
        # No hash set → login is disabled (operator hasn't bootstrapped).
        # Don't reveal that vs. wrong-password to clients; just 401.
        _record_login_attempt(ip)
        raise HTTPException(
            status_code=401,
            detail={"error": "unauthorized", "message": "invalid password"},
        )

    try:
        _ph.verify(settings.admin_password_hash, req.password)
    except (VerifyMismatchError, InvalidHashError):
        _record_login_attempt(ip)
        raise HTTPException(
            status_code=401,
            detail={"error": "unauthorized", "message": "invalid password"},
        )

    # Successful login: clear the failure bucket so the operator isn't
    # rate-limited by their own earlier typos.
    _clear_login_attempts(ip)
    token, exp = _issue_token()
    return LoginResponse(token=token, expires_at=exp)


@router.post("/logout")
async def logout() -> Response:
    """No-op on a stateless JWT design. The client discards the token; the
    server has nothing to delete. Exposed so the Flutter app's logout button
    has a well-named endpoint to call (and so an audit log entry can be added
    here later without an API change)."""
    return Response(status_code=204)
