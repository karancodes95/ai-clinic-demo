# Security

This is a portfolio demo that runs on synthetic, per-session seeded data. The
threat model treats the LLM as untrusted: it may emit any SQL, and the system
is designed so that it still cannot read or modify another session's data.

## Defenses in place

1. **SQL validation** (`app/core/sql_runner.py`): generated SQL is parsed with
   sqlglot; exactly one statement, `SELECT` only, writes/DDL rejected,
   `set_config`/`current_setting`/file/network/system-catalog functions banned,
   and the query is restricted to a per-vertical table allowlist (fail-closed).
2. **Execution sandbox**: every query runs in a `READ ONLY` transaction with
   `statement_timeout = 5s` and a 200-row cap.
3. **Row-level security**: tenant tables (`doctors`, `patients`, `appointments`,
   `prescriptions`, `lab_results`) are `FORCE ROW LEVEL SECURITY`, keyed on the transaction-local
   `app.session_id` GUC. The API connects as a `NOSUPERUSER NOBYPASSRLS` role,
   and boot refuses to start if the role can bypass RLS (`app/db.py`).
4. **Auth**: a static API key (constant-time compared, non-empty enforced at
   load) gates chat/data routes; `/admin/*` uses an HS256 JWT that requires
   `exp` and an `admin` scope (algorithm allow-listed, so `alg:none` is
   rejected), with a per-IP login throttle. Admin is closed by default.
5. **Abuse controls**: per-IP daily caps (15 questions, 5 leads); `/leads/public`
   is gated by Cloudflare Turnstile, a honeypot, and the rate limit, in that
   order. LLM error text is sanitized before it reaches the client; interactive
   docs (`/docs`, `/openapi.json`) are disabled.

## Accepted risks (this demo)

These are acceptable because the data is synthetic and per-session. They are
called out so anyone reusing this code for real data knows to change them:

1. **Session identity is the session UUID** passed in the `X-Session-Id` header.
   Anyone who learns a session's UUID can read that session's (synthetic) data.
   For real data, bind a session to a secret issued only to its creator.
2. **Session reuse by IP**: `POST /session` returns the most recent live session
   for the caller's `(IP, vertical)`, so users behind one shared IP may share a
   session. Disable this reuse for multi-tenant real data.
3. **PII logging is off by design**: `_SCRUB_PII = False` (`app/core/events.py`)
   stores chat/lead text verbatim in the `events` table for demo follow-up. A
   full email/phone scrubber is one flag flip away; enable it for real data.
4. **`/version`** returns the build tag (recon only).

## Deployment requirements

1. **Run behind a trusted reverse proxy.** The app trusts the `X-Real-IP` header
   only when the socket peer is loopback or in `TRUSTED_PROXIES` (CSV of
   CIDRs). Set `TRUSTED_PROXIES` to your proxy's address and have the proxy set
   `X-Real-IP`. Never expose uvicorn directly to the internet, or per-IP caps
   can be bypassed by forging `X-Real-IP`.
2. **Set strong secrets** in `.env` (never committed): a non-empty `API_KEY`,
   a 32+ byte `ADMIN_JWT_SECRET`, and an argon2 `ADMIN_PASSWORD_HASH`.
3. **Set `ALLOWED_ORIGINS`** to your frontend origin(s) in production (empty
   falls back to `*`, which is dev-only).
4. **Add a request body-size limit** at the proxy (e.g. nginx
   `client_max_body_size`) as defense-in-depth.

## Dependencies

Dependencies are pinned in `requirements.txt` (hash-locked). Run `pip-audit`
against the lock in CI as a standing gate; keep security-relevant packages
(e.g. `pyjwt`, `urllib3`) current.

## Reporting

Found something? Open a GitHub issue (no sensitive details) or reach out via the
contact link in the project README.
