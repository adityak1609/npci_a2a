# NPCI A2A Partner Platform (Bank-side)

> The **bank's side** of the NPCI Agent-to-Agent (A2A) change-management workflow.
> It receives change communications from NPCI, drives the acknowledgement /
> query / counter-proposal / blocker / milestone lifecycle, and gives bank
> product managers a portal with a **RAG assistant grounded in each change's
> documents** — with the ability to raise questions to NPCI and have the answers
> folded back into the knowledge base.

> **Scope:** this repo implements the **Bank responsibilities** of the A2A spec.
> The NPCI platform/cert-simulator side is out of scope; a **mock NPCI agent** is
> bundled for local end-to-end testing.

---

## What it does

- **A2A ingress from NPCI** — accepts `change_communication` documents over a
  spec-conformant, multi-layer-authenticated JSON-RPC endpoint, stores the
  originals, and asynchronously extracts → chunks → embeds → summarizes them.
- **Change lifecycle** — proposal acknowledgement, `query` / `clarification_response`,
  `counter_proposal` / `counter_decision`, `blocker` / status / resolution, and
  `milestone_update` / status reporting.
- **Partner portal (SPA)** — an incoming-changes board, a per-user "My Updates"
  feed, a change-detail view, a knowledge base, and an Assistant.
- **Grounded RAG Assistant** — answers questions scoped to a single change's
  documents. When the documents can't answer confidently, it offers **"Raise
  query to NPCI"**; NPCI's reply is auto-ingested as authoritative KB content.
- **Per-user privacy + admin oversight** — escalations (and NPCI's answers) are
  visible only to the user who raised them; admins see all escalations attributed
  to the raising user. NPCI documents are shared with everyone.
- **Mock NPCI agent** — a standalone FastAPI service that plays NPCI for local
  demos: push documents to the bank, answer queries, and decide counter-proposals
  from a structured inbox UI.

---

## Architecture

```
                          NPCI (or the bundled mock agent :8001)
                                       │  JSON-RPC over HTTPS
                                       │  HMAC + JWT (+ mTLS in prod)
                                       ▼
                          ┌──────────────────────────────┐
   Bank portal user       │  POST /a2a/external           │   change_communication,
   (browser, SPA)         │  8-layer auth, task_type       │   query, counter_decision,
        │                 │  dispatch                      │   blocker_*, milestone_*
        │ session JWT     └──────────────┬─────────────────┘
        ▼                                │
 ┌──────────────┐   /a2a/internal        ▼
 │  React SPA   │─────────────▶ ┌───────────────────┐   ┌──────────────────────┐
 │ (Vite/TS/    │  JSON-RPC     │  FastAPI (main.py) │──▶│ Ingestion worker      │
 │  Tailwind)   │◀───────────── │  routers + lifespan│   │ (PDF→chunks→embed→sum)│
 └──────────────┘   REST/SSE    └─────────┬─────────┘   └──────────┬───────────┘
                                          │                        │
                                          ▼                        ▼
                                 ┌──────────────────┐   ┌──────────────────────┐
                                 │ Gemini 2.5 Flash  │   │ PostgreSQL + pgvector │
                                 │ (RAG + summaries) │   │ (chunks, changes,     │
                                 └──────────────────┘   │  tickets, audit)      │
                                                        └──────────────────────┘
```

**Stack:** Python · FastAPI · Google Gemini 2.5 Flash · `gemini-embedding-001` ·
PostgreSQL + pgvector · Redis (prod) · Alembic · Docker · React + Vite +
TypeScript + Tailwind.

---

## Project structure

```
NPCI_A2A/
├── main.py                  # FastAPI app: middleware, CSP, routers, lifespan
├── worker.py                # Out-of-process ingestion worker (prod)
├── a2a_adk_server.py        # Bank's Google-A2A (ADK) server (port 8002)
├── database.py              # SQLAlchemy engine, ORM models, init_db
├── core/                    # config+logging, secrets, metrics, telemetry, utils
├── security/
│   ├── auth.py              # Users, JWT issue/revoke, role dependencies
│   ├── guardrails.py        # LLM input/output guardrails
│   └── csrf.py, rate_limit.py, file_validation.py, compliance.py
├── a2a_core/                # NPCI A2A protocol layer ("a2a" name is taken by the Google SDK)
│   ├── models.py            # A2A envelope + payload schemas (spec v1.0)
│   ├── auth.py              # HMAC / mTLS / JWT credential binding
│   ├── state.py             # Task + idempotency state
│   └── audit.py             # Append-only A2A audit trail
├── rag/
│   └── retriever.py / ingest.py / prompt.py / embedding_config.py   # RAG pipeline
├── agent/
│   ├── methods.py           # /a2a/internal JSON-RPC method names
│   ├── dispatcher.py        # Internal dispatch: query_rag, escalate, …
│   └── external_dispatcher.py  # /a2a/external task_type dispatch
├── routes/
│   ├── a2a_routes.py        # /a2a/external + /a2a/internal
│   ├── admin_routes.py      # Portal dashboard, changes, updates, admin ops
│   ├── auth_routes.py       # Login/session/MFA
│   └── chat_routes.py       # RAG chat + SSE streaming
├── services/
│   ├── a2a_ingestor.py      # change_communication → durable ingestion jobs
│   ├── npci_client.py       # Outbound Bank→NPCI queries + escalation routing
│   ├── npci_reply.py        # clarification_response ingest (→ KB)
│   ├── npci_outbound.py     # counter_proposal / milestone / blocker senders
│   ├── kb_ingest.py, settings_store.py, retention.py, email_notify.py
├── frontend/                # React + Vite + TS + Tailwind SPA (served from dist/)
│   └── src/pages/           # Dashboard, PmDashboard (My Updates), Assistant,
│                            #   ChangeDetail, KnowledgeBase, Users, Settings
├── mock_npci_agent/         # Standalone mock NPCI counterpart (port 8001)
│   ├── main.py, start.ps1
├── alembic/versions/        # Schema migrations (source of truth in prod)
├── docs/                    # ARCHITECTURE, TECHNICAL_DOCUMENTATION, RUNBOOK, …
├── docker-compose.yml       # Local Postgres (pgvector) + Redis
└── start.ps1                # Local launcher: Docker + Postgres + API
```

---

## Setup

### 1. Prerequisites
- **Python 3.10+**
- **Node 20+** (to build the SPA)
- **Docker Desktop** (PostgreSQL + pgvector, Redis)

### 2. Install dependencies

```bash
python -m venv .venv
.venv\Scripts\activate         # Windows
# source .venv/bin/activate    # macOS / Linux
pip install -r requirements.txt
```

### 3. Environment variables

Copy `.env.example` to `.env` and fill in at least:

```env
GOOGLE_API_KEY=your_google_api_key_here     # https://aistudio.google.com/apikey
SECRET_KEY=a-long-random-jwt-signing-secret
A2A_HMAC_SECRET=shared-hmac-secret-with-npci
NPCI_API_KEY=bearer-key-npci-presents-to-the-bank
# For local mock end-to-end testing:
NPCI_AGENT_URL=http://localhost:8001/a2a
A2A_MTLS_REQUIRED=false
```

> `.env` is git-ignored — never commit real secrets. See `.env.example` for the
> full list (confidence threshold, notification allowlists, retention, etc.).

### 4. Launch (local dev)

```powershell
.\start.ps1      # starts Docker Desktop if needed, brings up Postgres, runs the API
```

Open **http://localhost:8000** to sign in to the Partner Platform portal.

Registration always creates a standard (non-admin) account. Promote an admin
through a trusted DB operation:

```powershell
docker exec mindgate_postgres psql -U postgres -d mindgate_db -c "UPDATE users SET is_admin = TRUE WHERE email = 'admin@example.com';"
```

### 5. Build the frontend

FastAPI serves the built SPA from `frontend/dist/` at `/`, with hashed assets
under `/assets`. **`frontend/dist/` is git-ignored, so you must build it** (the
production Docker image does this automatically in a `node:20` stage):

```bash
cd frontend
npm ci
npm run build       # → frontend/dist, served by FastAPI
# or, for hot-reload dev against the API on :8000:
npm run dev         # Vite dev server on http://localhost:5173
```

### 6. (Optional) Run the mock NPCI agent

Plays the NPCI counterpart so you can exercise full A2A flows locally.

```powershell
.\mock_npci_agent\start.ps1      # binds http://localhost:8001
```

Its UI (http://localhost:8001) can push a `change_communication` to the bank and
has a structured **Inbox** to answer bank queries and decide counter-proposals,
blockers, and milestones by hand. Ensure the bank's `.env` has
`NPCI_AGENT_URL=http://localhost:8001/a2a` and `A2A_MTLS_REQUIRED=false`.

> The steps above run the **legacy custom** transport (`/a2a/external`, JSON-RPC
> + HMAC). To run on **Google A2A** — the transport NPCI's real agent speaks —
> use the ADK path below instead.

### 7. (Alternative) Run everything on the Google A2A (ADK) transport

The real NPCI agent speaks **Google A2A**, not the custom protocol. This mode
runs the same business core over the ADK transport. One command starts all three
processes wired together:

```powershell
.\start_a2a.ps1
```

It launches (each in its own window):

| Process | Command | URL |
|---|---|---|
| Bank portal (SPA + `/a2a/internal`) | `uvicorn main:app` | http://localhost:8000 |
| Bank ADK A2A server (inbound NPCI→Bank) | `uvicorn a2a_adk_server:a2a_app` | http://localhost:8002 |
| Mock NPCI ADK agent (Google-A2A counterpart) | `uvicorn mock_npci_agent.adk_agent:a2a_app` | http://localhost:8001 |

The script sets `NPCI_TRANSPORT=a2a` (so the portal's Bank→NPCI messages route
over A2A to the mock's card at `:8001`) and points the mock at the bank's card at
`:8002`. On a fresh venv it first installs the ADK deps from `requirements.txt`.
Transport auth (spec §4.1 L1–4) is left **off** for local dev via
`A2A_ADK_TRANSPORT_AUTH=false`; set it `true` (with the mTLS/HMAC/JWT config) for
a production-shaped run.

The ADK mock has no trigger UI — push a `change_communication` from the CLI:

```powershell
python -m mock_npci_agent.push_change_a2a
# or: python -m mock_npci_agent.push_change_a2a --change-id CHG-UPI-2026-001 --file spec.pdf
```

Queries the bank raises are auto-answered by the mock with a
`clarification_response` sent back to the bank's card.

---

## A2A endpoints

External NPCI calls (spec `task_type` dispatch, 8-layer auth):

```http
POST /a2a/external
Authorization: Bearer <NPCI_API_KEY>
X-NPCI-Timestamp / X-NPCI-Nonce / X-NPCI-Signature   # HMAC
```

Handled Phase-C task types: `change_communication`, `proposal_acknowledged`,
`change_acknowledgement`, `query`, `clarification_response`, `counter_proposal`,
`counter_decision`, `milestone_update`, `milestone_status_request`,
`milestone_status_report`, `readiness_declaration`, `blocker`,
`blocker_status_update`, `blocker_resolution` (+ `echo`).

Portal SPA calls (session JWT, role-gated):

```http
POST /a2a/internal
Authorization: Bearer <session JWT>   # or HttpOnly cookie
```

| Method | Who | Purpose |
|--------|-----|---------|
| `agent.query_rag` | user + admin | Grounded RAG answer for a change |
| `agent.escalate_to_npci` | user + admin | Raise a query/escalation to NPCI |
| `agent.get_summary` | admin | Fetch a stored document summary |
| `agent.submit_counter_proposal` | admin | Bank → NPCI counter-proposal |
| `agent.report_milestone` | admin | Bank → NPCI milestone update |
| `agent.declare_readiness` | admin | Bank → NPCI readiness declaration |
| `agent.raise_blocker` | admin | Bank → NPCI blocker |

`/a2a` (the old shared endpoint) is deprecated and returns **410 Gone**.
Never expose `NPCI_API_KEY` to the frontend.

### Selected REST endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/` | Serves the SPA |
| GET | `/health` | Health check |
| POST | `/auth/admin/login`, `/auth/login` | Authenticate; `/auth/me`, `/auth/logout` for session |
| GET | `/portal/dashboard` | Per-user updates feed (owner-scoped work items + shared docs) |
| GET | `/admin/changes` | Incoming changes board |
| POST | `/chat`, `/chat/stream` | RAG answer (JSON / SSE streaming) |
| GET | `/documents`, `/documents/{id}/download` | Received documents |
| POST | `/ingestion-jobs/{job_id}/retry` | Retry a failed ingestion |

---

## Key behaviors

**Asynchronous, idempotent ingestion.** `change_communication` stores originals
under `data/a2a_received/`, computes `content_hash = sha256(bytes)`, creates
`received_documents` + `a2a_ingestion_jobs`, and a background worker does the
extract/chunk/embed/summarize. Duplicate hashes are not re-ingested. Job status
moves through `queued → processing → extracting → embedding → ready` (or
`failed`, retryable).

**Grounded RAG + escalation.** Retrieval is scoped to the selected change's
documents. Confidence is `max(0, 1 − best_distance / A2A_EMBEDDING_SCALE)`; below
`A2A_CONFIDENCE_THRESHOLD` (or when the model returns a grounded "can't answer"),
the Assistant surfaces **Raise query to NPCI** instead of guessing.

**NPCI replies → knowledge base.** A `clarification_response` with
`amends_change=false` is embedded as authoritative KB content (with `grounded_in`
citations) when `NPCI_REPLY_AUTO_INGEST=true`; if it `amends_change`, it's held
as *provisional* until the formal change update lands. With auto-ingest off,
replies wait as `pending_review` for admin approval.

**Per-user update scoping.** Escalations are keyed to `raised_by_user_id`, so a
user only sees their own Q&A; admins see all of them, attributed to the raising
user. NPCI **documents and new-change notices are shared with everyone**
regardless of change ownership; counter-proposals, blockers, and milestones stay
scoped to the change owner.

**Notification safety.** Inbound `notify_list` addresses are filtered through
`A2A_NOTIFY_ALLOWLIST` / `A2A_NOTIFY_ALLOWED_DOMAINS`; the static
`A2A_NOTIFY_LIST` is always allowed. A failing SMTP send never fails an
otherwise-successful ingestion.

---

## Production

Use `.env.production.example` and `docker-compose.prod.yml`: managed PostgreSQL
and Redis, file-mounted secrets, Alembic migrations as a one-shot service, a
separate ingestion worker (`python worker.py` with `A2A_INGESTION_MODE=worker`),
TLS/mTLS terminated at Nginx, and Prometheus scraping + alert rules.

Production requires Alembic-managed schema state — run `alembic upgrade head`
before the API and set `APP_ENV=production`. See `docs/PRODUCTION_RUNBOOK.md` for
the full sequence, and `docs/ARCHITECTURE.md` / `docs/TECHNICAL_DOCUMENTATION.md`
for module-level detail.

Browser sessions use an HttpOnly, `SameSite=strict` cookie set at login; Bearer
tokens are still returned for non-browser API clients.

---

## License

Internal use only — Mindgate Solutions.
```