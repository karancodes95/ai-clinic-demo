from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str
    migrations_database_url: str | None = None
    api_key: str
    vertical: str = "healthcare"

    llm_provider: str = "gemini"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-pro"
    # Claude via the Agent SDK (auth: machine Claude Code login or
    # CLAUDE_CODE_OAUTH_TOKEN). Empty model = SDK default.
    claude_model: str = ""

    # Input cap: max characters accepted in a single /chat question. Enforced
    # server-side via the Pydantic ChatRequest model. Frontend TextField
    # mirrors it as a maxLength for UX, but the server is the trusted gate.
    # 2000 chars ≈ 500 tokens - generous for a demo question, cheap to refuse.
    max_input_chars: int = 2000

    # Output cap: hard ceiling on tokens Gemini may emit per call (passed
    # through GenerateContentConfig.max_output_tokens). Bounds the worst-case
    # cost of any single response - an attacker cannot ask for a 10k-token
    # essay. 2048 leaves headroom for thinking-model reasoning tokens (Gemini
    # 2.5 Pro counts those toward this budget); at ~$10/1M output tokens it
    # caps a single call at ~$0.02. Lower if you switch to a non-thinking
    # model where visible output is the whole budget.
    max_output_tokens: int = 2048

    # Set to a long random string via .env to enable rate-limit bypass for
    # integration test runs. Empty in production. Match in `X-Test-Mode`
    # header on outbound requests to skip the per-IP daily cap.
    test_mode_key: str = ""

    # Comma-separated list of trusted proxy CIDRs (e.g.
    # "10.0.0.0/8,2400:cb00::/32"). Only when the immediate socket peer is
    # in this list do we honor X-Forwarded-For for rate limiting and lead
    # capture. Empty (default) = NEVER trust XFF - closes M2 review finding.
    trusted_proxies: str = ""

    # When false (default), boot logs a critical warning if the runtime
    # DB role is a superuser or has BYPASSRLS - those silently disable
    # row-level security. Set true in dev .env if you knowingly run as
    # the migration owner.
    allow_superuser_db_role: bool = False

    # Comma-separated CORS origins for production. Empty default keeps the
    # current "*" behavior (the static API key is the only gate). Set to
    # your production origin(s), e.g. your GitHub Pages URL, before launch.
    allowed_origins: str = ""

    # Latency threshold (ms) above which a `slow_query` event is logged
    # alongside the regular query_executed / answer_streamed events.
    slow_query_threshold_ms: int = 5000

    # Build/version banner for /version endpoint. Set via deploy pipeline
    # (e.g. `--build-arg VERSION=$(git rev-parse --short HEAD)`).
    app_version: str = "dev"

    # Slack incoming-webhook URL. When set, every successful /leads POST
    # fires a one-line notification to the configured channel. Empty
    # disables the integration silently.
    slack_webhook_url: str = ""

    # Cloudflare Turnstile secret key. Used by /leads/public to verify
    # the human-challenge token issued by the Turnstile widget on your
    # site. Empty disables verification (dev only,
    # the endpoint refuses public submissions when unset in prod).
    turnstile_secret_key: str = ""

    # Admin auth (bearer-token login flow). See app/admin_auth.py.
    # admin_password_hash: argon2id hash. Generate via:
    #   .venv/bin/python tool/hash_admin_password.py
    # admin_jwt_secret: HMAC key signing the session JWT. 32+ random bytes
    #   (hex or base64). Rotate to invalidate every live session at once.
    # admin_session_ttl_hours: how long a freshly-issued token is valid for.
    # All three may be empty in dev; the login endpoint refuses while empty,
    # but the legacy X-Admin-Key path still works (settings.api_key).
    admin_password_hash: str = ""
    admin_jwt_secret: str = ""
    admin_session_ttl_hours: int = 168  # 7 days

    # Static admin key - back-compat path for curl/CI hitting /admin/* with
    # `X-Admin-Key`. Deliberately SEPARATE from `api_key` (which is the chat
    # demo's gate). Splitting these means a leak of the chat key (baked into
    # the chat Flutter bundle, extractable via DevTools) cannot escalate to
    # admin. Empty (default) disables the X-Admin-Key path entirely - all
    # admin auth must then go through the bearer-JWT flow (POST /admin/login).
    admin_api_key: str = ""

    @field_validator("api_key")
    @classmethod
    def _api_key_must_be_set(cls, v: str) -> str:
        # An empty api_key turns `x_api_key == api_key` into an empty-header
        # bypass, so fail closed at load rather than silently disable the gate.
        if not v or not v.strip():
            raise ValueError("API_KEY must be a non-empty value")
        return v

    # extra="ignore": CLAUDE_CODE_OAUTH_TOKEN lives in .env for the Agent SDK to
    # read from the environment, but isn't a Settings field - don't reject it.
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")

    @property
    def migrate_url(self) -> str:
        return self.migrations_database_url or self.database_url


settings = Settings()
