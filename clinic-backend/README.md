# Clinic AI Analyst (backend)

Chat with a patient's own clinic records in plain English. Ask about
appointments, prescriptions, lab results, and doctors; the backend turns the
question into SQL, runs it against a live Postgres database under strict
per-session isolation, and streams back a natural-language answer.

This is the API service. It is provider-flexible (Google Gemini or Claude via
the Claude Agent SDK) and built so that a large language model can write SQL
without ever being trusted: every generated query is validated, sandboxed, and
row-level-security scoped before it touches data.

## How it works

```
question --> LLM (natural language to SQL) --> validate: AST parse, SELECT-only, no set_config
                                                   |
                                                   v
                     read-only transaction + RLS (app.session_id) + statement_timeout + row cap
                                                   |
                                                   v
                  rows --> LLM (writes the answer) --> streamed to the client over SSE
```

Each visitor gets a fresh `session_id` and their own isolated set of sample
patient records, seeded on session creation. The demo is patient-scoped: a
session holds exactly one patient's data, so "my appointments" simply means the
rows in that session. Row-level security keys every tenant table to the session,
so one session can never read another's rows, even if the model is tricked into
writing a query that tries.

This is a records-lookup assistant, not clinical advice: the prompts keep it to
what the tables hold and route anything medical back to the clinic.

## Security and isolation

The model is treated as untrusted. Five layers hold the line:

1. **Row-level security.** Tenant tables (`doctors`, `patients`, `appointments`,
   `prescriptions`, `lab_results`) are RLS-isolated by the `app.session_id` GUC,
   with `FORCE ROW LEVEL SECURITY` so it applies even to the table owner.
2. **A non-privileged role.** The API connects as `demo_app`
   (`NOSUPERUSER NOBYPASSRLS`); Postgres would bypass RLS for a superuser or a
   `BYPASSRLS` role, so the app never uses one.
3. **SQL validation.** Generated SQL is parsed with `sqlglot`: exactly one
   statement, must be a `SELECT`, writes and DDL rejected, and `set_config` /
   `current_setting` banned so the session GUC cannot be rewritten mid-query.
4. **Sandboxed execution.** Queries run inside a `READ ONLY` transaction with a
   `statement_timeout` and a hard row cap, so a runaway or cross-join query
   cannot tie up the pool or exfiltrate a large result.
5. **Boot-time proof.** Startup refuses to launch if the connected role can
   bypass RLS, so a misconfigured `DATABASE_URL` fails loud instead of silent.

On top of that: a static API key gates the endpoints, admin uses a bearer-JWT
login, per-IP rate limits apply (15 questions/day, 5 leads/day), LLM error text
is sanitized before it reaches the client, and the interactive docs are
disabled.

> Note: the adversarial test suite (RLS-bypass attempts, injection probes) is
> intentionally withheld from this public repository. It maps exact attack
> vectors, so it stays private; the defenses above stand on their own.

## Stack

- **Python 3.12 + FastAPI** (async), **Pydantic** for validation at every boundary
- **Postgres** with row-level security; **asyncpg** connection pool
- **sqlglot** for SQL parsing / validation
- **LLM**: Google Gemini, or Claude via the Claude Agent SDK
- Streaming over Server-Sent Events (SSE)

## API

| Method | Path | Purpose |
| ------ | ---- | ------- |
| GET  | `/health`, `/version` | Liveness and build info (public) |
| POST | `/session` | Create/resume a session, returns `session_id` + input caps |
| GET  | `/schema` | Table columns + per-session row counts and samples for the UI |
| POST | `/chat` | Natural-language question, streamed answer (SSE) |
| GET  | `/quota`, `/history` | Rate-limit status and conversation turns |
| POST | `/leads`, `/leads/public` | Lead capture (public one is Turnstile-gated) |
| POST | `/admin/login`, `/admin/logout` | Admin bearer-token auth |
| GET  | `/admin/metrics`, `/admin/sessions/...` | Operator analytics |

All non-public endpoints require the `X-API-Key` header; data endpoints also take
`X-Session-Id`.

## Running it locally

Prerequisites: Python 3.12 and Postgres 14+ running locally.

1. Create the database and apply the schema (this also creates the `demo_app`
   role and all row-level-security policies):
   ```bash
   createdb clinic_demo
   psql "postgresql://<owner>@localhost:5432/clinic_demo" -f schema.sql
   ```
2. Create a virtualenv and install dependencies:
   ```bash
   python3.12 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   ```
3. Configure the environment:
   ```bash
   cp .env.example .env
   # set LLM_PROVIDER=gemini + GEMINI_API_KEY, or LLM_PROVIDER=claude
   ```
4. Run the API:
   ```bash
   .venv/bin/uvicorn app.main:app --reload
   ```
5. Try it: `POST /session` (with your `X-API-Key`) to seed a session, then
   `POST /chat` with a question like "what are my active prescriptions?".

Run the tests with `pytest` (the maintainer's checkout includes the adversarial
suite noted above).

## Project structure

```
clinic-backend/
├── schema.sql            One consolidated schema: tables, RLS, demo_app role
├── app/
│   ├── main.py           FastAPI app factory + router wiring
│   ├── config.py         Pydantic settings (env-driven)
│   ├── db.py             asyncpg pool + startup RLS-bypass assertion
│   ├── api/              Routers: meta, catalog, chat, leads, admin
│   ├── core/             sql_runner, sessions, history, events, rate_limit, slack, turnstile
│   ├── llm/              Provider adapters (base, gemini, claude)
│   └── vertical/healthcare/   Schema prompt + sample-data seed
└── tests/                Capability and multi-turn tests
```

## License

MIT. See [LICENSE](LICENSE).
