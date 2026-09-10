"""Lead capture. /leads is API-key gated (used by the app); /leads/public is
unauthenticated and defended by Cloudflare Turnstile, a honeypot field, and the
per-IP daily cap. Both notify Slack best-effort."""

from __future__ import annotations

import asyncio
import logging
import re

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app import db
from app.api.deps import require_api_key
from app.api.schemas import LeadRequest, PublicLeadRequest
from app.config import settings
from app.core import rate_limit, slack, turnstile

router = APIRouter(tags=["leads"])

# Strip HTML/script-tag-shaped tokens from free text before storage. Best-effort
# defense, NOT a complete sanitizer: the regex requires a closing `>`, so a
# payload like `<script src=evil` (no closing bracket) passes through unchanged.
# Acceptable because nothing renders these fields as HTML (Slack treats them as
# plain text). Any HTML rendering layer MUST escape output at render time.
_HTML_TAG_RE = re.compile(r"<[^>]*>")


def _sanitize_notes(s: str | None) -> str | None:
    if s is None:
        return None
    return _HTML_TAG_RE.sub("", s).strip() or None


@router.post("/leads", dependencies=[Depends(require_api_key)])
async def capture_lead(
    req: LeadRequest,
    request: Request,
) -> dict[str, str]:
    ip = rate_limit.client_ip(request)

    # Per-IP daily cap on lead capture. Test clients bypass via the same
    # predicate as /chat.
    if not rate_limit.is_test_client(request):
        allowed, count = await rate_limit.check_and_count_leads(ip)
        if not allowed:
            return JSONResponse(
                status_code=429,
                content={
                    "error": "rate_limited",
                    "message": (
                        f"You've already submitted {count} lead "
                        f"requests today. Please try again tomorrow."
                    ),
                    "cap": rate_limit.LEADS_DAILY_CAP,
                    "count": count,
                },
            )

    notes = _sanitize_notes(req.notes)
    name = _sanitize_notes(req.name)  # same HTML strip works for plain text
    webhook_configured = bool((settings.slack_webhook_url or "").strip())
    initial_status = "pending" if webhook_configured else None

    async with db.pool().acquire() as conn:
        # UNIQUE expression index on (email, COALESCE(source, '')) collapses
        # retries. ON CONFLICT uses index-inference matching that expression.
        inserted = await conn.fetchval(
            "INSERT INTO leads (email, name, ip, source, notes, slack_status) "
            "VALUES ($1, $2, $3::inet, $4, $5, $6) "
            "ON CONFLICT (email, (COALESCE(source, ''))) DO NOTHING "
            "RETURNING id",
            str(req.email), name, ip, req.source, notes, initial_status,
        )

        # Only ping Slack for genuinely new leads; duplicates return NULL from
        # RETURNING. Best-effort; failures land in slack_status='failed'.
        if inserted is not None and webhook_configured:
            ok = slack.notify_lead(
                email=str(req.email), name=name, ip=ip,
                source=req.source, notes=notes,
            )
            try:
                await conn.execute(
                    "UPDATE leads SET slack_status = $1 WHERE id = $2",
                    "sent" if ok else "failed", inserted,
                )
            except Exception:
                # Lead is already saved and Slack already pinged (if ok=True).
                # A failed status-write must NOT 500 the user's submission.
                logging.getLogger("leads").exception(
                    "slack_status update failed for lead id=%s", inserted
                )
    return {"status": "captured"}


@router.post("/leads/public")
async def capture_public_lead(
    req: PublicLeadRequest,
    request: Request,
) -> JSONResponse:
    """Order of gates is deliberate:

      1. Turnstile FIRST, so a failed check does not consume rate-limit budget
         (otherwise an attacker could lock out any IP with garbage tokens).
      2. Rate-limit + increment, so only verified humans count against the cap.
      3. Honeypot, with a silent 200 that matches the success shape so bots
         can't distinguish trap-tripped from real-success by response.
      4. Slack notify, always returning 200 regardless of Slack outcome so a
         probing attacker can't time/detect Slack outages.
    """
    ip = rate_limit.client_ip(request)

    # 1. Turnstile verification must succeed before anything stateful. If
    #    TURNSTILE_SECRET_KEY is unset (dev), the verifier returns False.
    ok = await asyncio.to_thread(
        turnstile.verify, req.turnstile_token, remote_ip=ip
    )
    if not ok:
        return JSONResponse(
            status_code=400,
            content={
                "error": "verification_failed",
                "message": "Anti-bot check failed. Please reload and try again.",
            },
        )

    # 2. Per-IP daily cap; counts only against verified-human traffic.
    if not rate_limit.is_test_client(request):
        allowed, count = await rate_limit.check_and_count_leads(ip)
        if not allowed:
            return JSONResponse(
                status_code=429,
                content={
                    "error": "rate_limited",
                    "message": (
                        f"You've already submitted {count} lead "
                        f"requests today. Please try again tomorrow."
                    ),
                    "cap": rate_limit.LEADS_DAILY_CAP,
                    "count": count,
                },
            )

    # 3. Honeypot: silent success mirroring the real-success shape.
    if req.company_website is not None and req.company_website != "":
        logging.getLogger("leads").info(
            "honeypot tripped from ip=%s source=%s", ip, req.source
        )
        return JSONResponse(status_code=200, content={"status": "sent"})

    name = _sanitize_notes(req.name)
    notes = _sanitize_notes(req.notes)

    # 4. Best-effort Slack notification. Failures are logged, not surfaced, so
    #    the 502 path can't leak Slack-down state to probing attackers.
    sent = await asyncio.to_thread(
        slack.notify_lead,
        email=str(req.email), name=name, ip=ip,
        source=req.source, notes=notes,
    )
    if not sent:
        logging.getLogger("leads").warning(
            "public lead slack delivery failed: email=%s source=%s",
            str(req.email), req.source,
        )
    return JSONResponse(status_code=200, content={"status": "sent"})
