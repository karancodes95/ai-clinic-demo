"""Slack notification - fires on /leads success.

Best-effort: logs and swallows failures so a Slack outage never blocks lead
capture (the row is already in Postgres before this runs).
"""

from __future__ import annotations

import json
import logging
import ssl
import time
import urllib.error
import urllib.request

import certifi

from app.config import settings

logger = logging.getLogger("slack")

# Mac stock Python has no CA bundle; certifi ships Mozilla's roots so the
# TLS handshake to hooks.slack.com works in dev (prod uses system trust).
_SSL_CTX = ssl.create_default_context(cafile=certifi.where())

_ATTEMPTS = 3
_TIMEOUT_S = 2.0
_BACKOFF_S = 0.5


def _slack_escape(s: str | None) -> str | None:
    """Defang user-controlled text before it joins the Slack mrkdwn message.

    Closes 2026-05-15 audit F3 - an attacker could otherwise submit a `notes`
    field containing newlines plus `*Urgent:*` rows that render as fabricated
    extra leads / phishing prompts in the Slack channel.

    Strategy:
      - Strip newlines (collapses to space) so a single user field can't fake
        multiple "rows" in the joined message.
      - HTML-escape `&`, `<`, `>` per Slack's documented escape rules for
        special chars.
      - Neutralize formatting marks (`*`, `_`, `~`, `` ` ``) with a zero-width
        space prefix so they don't open bold / italic / code spans.
    """
    if s is None:
        return None
    s = s.replace("\r", "").replace("\n", " ")
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    for ch in ("*", "_", "~", "`"):
        s = s.replace(ch, "​" + ch)
    return s


def _post(url: str, payload: dict) -> None:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=_TIMEOUT_S, context=_SSL_CTX) as resp:
        resp.read()


def notify_lead(
    *,
    email: str,
    name: str | None = None,
    ip: str | None = None,
    source: str | None = None,
    notes: str | None = None,
) -> bool:
    """Return True if Slack accepted the payload on any of _ATTEMPTS tries,
    False if every attempt failed (logged each time)."""
    url = (settings.slack_webhook_url or "").strip()
    if not url:
        return False
    # Each row has a blank line gap; rows with no value are omitted.
    # Slack mrkdwn: single `*` for bold, backticks for inline code.
    # All user-controlled fields (name, email, notes) go through _slack_escape
    # so an attacker-supplied newline or `*` can't fabricate fake rows.
    # `source` and `ip` are server-set and known safe - no escape needed.
    safe_name = _slack_escape(name)
    safe_email = _slack_escape(email)
    safe_notes = _slack_escape(notes)
    rows: list[str] = ["*New lead!*"]
    if safe_name:
        rows.append(f"Name: {safe_name}")
    rows.append(f"Email: {safe_email}")
    if source:
        rows.append(f"source: `{source}`")
    if safe_notes:
        rows.append(f"notes: {safe_notes[:300]}")
    if ip:
        rows.append(f"ip: `{ip}`")
    payload = {"text": "\n\n".join(rows)}

    for attempt in range(1, _ATTEMPTS + 1):
        try:
            _post(url, payload)
            return True
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            logger.warning(
                "slack notify failed (attempt %d/%d): %s", attempt, _ATTEMPTS, e
            )
            if attempt < _ATTEMPTS:
                time.sleep(_BACKOFF_S)
    return False
