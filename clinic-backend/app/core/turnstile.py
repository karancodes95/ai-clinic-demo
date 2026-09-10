"""Cloudflare Turnstile token verification - used by /leads/public.

The frontend renders a Turnstile widget that issues a `cf-turnstile-response`
token on a successful (or silently-passed) challenge. The server must verify
that token against Cloudflare's siteverify endpoint before trusting the
submission, otherwise the token can be replayed/forged.

Best-effort with timeouts so a Cloudflare outage doesn't hang request
handlers. Returns False (== verification failed) on any error.
"""

from __future__ import annotations

import json
import logging
import ssl
import urllib.error
import urllib.parse
import urllib.request

import certifi

from app.config import settings

logger = logging.getLogger("turnstile")

_SSL_CTX = ssl.create_default_context(cafile=certifi.where())
_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
_TIMEOUT_S = 3.0


def verify(token: str, *, remote_ip: str | None = None) -> bool:
    """Return True iff Cloudflare confirms the token is valid."""
    secret = (settings.turnstile_secret_key or "").strip()
    if not secret or not token:
        return False
    payload: dict[str, str] = {"secret": secret, "response": token}
    if remote_ip:
        payload["remoteip"] = remote_ip
    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(
        _VERIFY_URL,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S, context=_SSL_CTX) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as e:
        logger.warning("turnstile verify failed: %s", e)
        return False
    if not body.get("success"):
        logger.info("turnstile rejected token: codes=%s", body.get("error-codes"))
        return False
    return True
