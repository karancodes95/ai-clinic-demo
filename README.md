# Ask Clinic: AI Healthcare Assistant Demo

A patient-facing AI assistant for a fictional clinic, Bluebell Clinic. A patient asks in plain English about their appointments, prescriptions, lab results and doctors, and gets an answer drawn from their own records. Fictional data, not a real medical service.

**Live demo:** https://demo.bytcra.com/clinic/

## What is in this repo

| Path | What it is |
| ---- | ---------- |
| `index.html` | Landing page with the two demo options |
| `clinic-user/` | Patient Demo: the chat page (plain HTML, CSS and JavaScript) |
| `clinic-admin/` | Admin Demo: placeholder page, coming soon |
| `clinic-backend/` | The API that answers the chat (FastAPI and Postgres) |

## How it works

```
question --> LLM writes SQL --> validated: one statement, SELECT only
                                    |
                                    v
        read-only transaction + row-level security for this session + row cap
                                    |
                                    v
             rows --> LLM writes the answer --> streamed to the page (SSE)
```

Each visitor gets a fresh session seeded with one sample patient's records. Row-level security ties every table to that session, so one visitor can never read another visitor's rows, even if the model is tricked into trying. The assistant looks up records only; anything medical is routed back to the clinic.

## Security

The model is treated as untrusted. Generated SQL is parsed and must be a single `SELECT`, runs in a read-only transaction with a timeout and a row cap, and the API connects as a role that cannot bypass row-level security. Endpoints sit behind an API key and per-IP daily limits. Details: [clinic-backend/README.md](clinic-backend/README.md) and [clinic-backend/SECURITY.md](clinic-backend/SECURITY.md).

## Stack

- **Front end:** plain HTML, CSS and JavaScript, no build step
- **Backend:** Python 3.12, FastAPI, Pydantic, Postgres with row-level security, asyncpg, sqlglot
- **LLM:** Google Gemini, or Claude via the Claude Agent SDK
- **Streaming:** Server-Sent Events

## Hosting

1. **Pages:** static files served by nginx on an Oracle Cloud (OCI) Ubuntu server, behind Cloudflare, at https://demo.bytcra.com/clinic/. GitHub Pages also publishes them from this repo's `main` branch.
2. **API:** the backend runs under uvicorn as a systemd service bound to localhost on the same Oracle Cloud server, behind nginx and Cloudflare, at `https://demo-api.bytcra.com/clinic`. Its Postgres database runs on the same server.

## Deployment

1. **Pages on demo.bytcra.com:** changed files are copied to the server's web root over SSH, then their SHA-256 checksums are compared with the repo copies.
2. **GitHub Pages:** each push to `main` runs GitHub's `pages-build-deployment` workflow, which republishes the pages.
3. **API:** the server runs the backend from a git checkout of this repo. To deploy, pull `main` there, install any changed requirements into the backend's virtualenv (see [clinic-backend/README.md](clinic-backend/README.md)), and restart the backend's systemd service. The service reads the checkout's commit when it starts, so `GET /version` on the API shows what is live.
4. **Settings:** a `.env` file next to the backend on the server. Git ignores it, so it stays out of the repo.

## Run it locally

1. Start the backend: follow [clinic-backend/README.md](clinic-backend/README.md).
2. In `clinic-user/app.js`, set `BASE_URL` to your local API and `API_KEY` to the key in your backend `.env`. By default the page calls the hosted API at `https://demo-api.bytcra.com/clinic`.
3. Serve the repo folder with any static file server and open `clinic-user/`. If the page's origin differs from the API's, add it to `ALLOWED_ORIGINS` in the backend `.env`.

## License

MIT. See [clinic-backend/LICENSE](clinic-backend/LICENSE).

---

Built by [Bytcra](https://bytcra.com)
