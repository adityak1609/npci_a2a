# Mindgate Policy Bot

> **RAG-based internal policy chatbot** for Mindgate Solutions staff.  
> Ask natural-language questions about company rules, regulations, and policies — answers are grounded exclusively in the official policy PDF.

---

## Architecture

```
User Question
    │
    ▼
┌──────────┐     ┌────────────────┐     ┌──────────────┐
│ Frontend │────▶│  FastAPI /chat  │────▶│   Retriever  │
│ (HTML)   │     │                │     │  (PostgreSQL +│
└──────────┘     │  Gemini 2.5    │◀────│  pgvector)   │
                 │  Flash         │     └──────────────┘
                 └────────────────┘
                        │
                        ▼
                  Grounded Answer
```

**Stack:** Python · FastAPI · Google Gemini 2.5 Flash · gemini-embedding-001 · PostgreSQL · pgvector · Docker · PyMuPDF

---

## Project Structure

```
mindgate-policy-bot/
├── main.py              # FastAPI app — /chat, /ingest, /health endpoints
├── ingest.py            # PDF → chunks → embeddings → Postgres
├── retriever.py         # Postgres pgvector query + chunk retrieval
├── database.py          # SQLAlchemy models and Postgres connection
├── auth.py              # JWT authentication and password hashing
├── prompt.py            # System prompt template with grounding rules
├── watcher.py           # Auto-ingestion on PDF drop
├── docker-compose.yml   # PostgreSQL container configuration
├── start.ps1            # PowerShell launcher for Docker and API
├── requirements.txt     # Pinned Python dependencies
├── .env                 # Environment variables (GOOGLE_API_KEY)
├── README.md
├── frontend/
│   └── index.html       # Chat UI
└── data/
    └── policy.pdf       # ← Place your PDF here
```

---

## Setup

### 1. Prerequisites
- **Python 3.10+**
- **Docker Desktop** (Required for PostgreSQL + pgvector)

### 2. Clone & install dependencies

```bash
# Create and activate a virtual environment (recommended)
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # macOS / Linux

# Install dependencies
pip install -r requirements.txt
```

### 3. Set your Environment Variables

Create a `.env` file in the project root:

```env
GOOGLE_API_KEY=your_google_api_key_here
# Optional: Change the JWT secret for production
JWT_SECRET_KEY=your_super_secret_key_here
```

> Get a Google API key from [Google AI Studio](https://aistudio.google.com/apikey).

### 4. Add the policy PDF

Place your internal rules & regulations PDF at:

```
data/policy.pdf
```

### 5. Launch the Application

We have a convenient PowerShell script that ensures Docker is running, starts the PostgreSQL database container, waits for it to be ready, and then starts the FastAPI server.

```powershell
# Run the launcher script
.\start.ps1
```

Open **http://localhost:8000** in your browser to sign in to the Partner Platform portal.

User registration always creates a standard account. To provision an administrator,
promote an existing account through a trusted database operation:

```powershell
docker exec mindgate_postgres psql -U postgres -d mindgate_db -c "UPDATE users SET is_admin = TRUE WHERE email = 'admin@mindgate.com';"
```

### 6. Frontend (Partner Platform portal)

The portal is a React + Vite + TypeScript + Tailwind SPA in `frontend/`. FastAPI
serves the built bundle from `frontend/dist/` at `/` and `/admin/*`, with hashed
assets under `/assets`.

```bash
cd frontend
npm ci
npm run build      # → frontend/dist, served by FastAPI
# or, for hot-reload development against a running API on :8000
npm run dev        # Vite dev server on http://localhost:5173 (proxies /auth,/admin,/a2a,…)
```

The production Docker image builds the SPA in a `node:20` stage and copies
`dist/` into the Python image — no manual build step needed for containers.

---

## API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET    | `/` | Serves the chat UI |
| GET    | `/health` | Health check |
| POST   | `/auth/admin/login` | Authenticate an administrator |
| POST   | `/auth/login` | Deprecated admin-only compatibility login |
| POST   | `/ingest` | Trigger admin PDF ingestion/re-ingestion |
| POST   | `/chat` | Send an admin question, get an answer (JSON) |
| POST   | `/chat/stream` | Stream answers token-by-token using Server-Sent Events (SSE) |
| POST   | `/a2a/external` | NPCI-only JSON-RPC endpoint using `NPCI_API_KEY` |
| POST   | `/a2a/internal` | Admin frontend JSON-RPC endpoint using admin JWT |
| GET    | `/documents` | Admin Inbox received-document list |
| POST   | `/ingestion-jobs/{job_id}/retry` | Retry failed A2A ingestion |
| GET    | `/npci-tickets` | Admin Inbox NPCI ticket list |
| GET    | `/chat/sessions`| List admin chat sessions |
| GET    | `/chat/history/{session_id}` | Retrieve message history for a session |
| DELETE | `/chat/sessions/{session_id}`| Delete a session and its message history |

`/a2a` is deprecated and returns `410 Gone`; use the split external/internal routes above.

*(Admin chat and ingestion endpoints require an admin JWT. Never expose `NPCI_API_KEY` to the frontend.)*

Browser admin sessions use an HttpOnly, SameSite=strict cookie set by
`/auth/admin/login`; Bearer tokens are still returned for non-browser API
clients. Use `/auth/me` to restore the SPA session and `/auth/logout` to clear
the browser cookie.

For production deployment, use `.env.production.example` and
`docker-compose.prod.yml` as the safer topology: it expects managed PostgreSQL
and Redis, mounts secrets from files, runs Alembic migrations as a one-shot
service, starts a separate ingestion worker, terminates TLS/mTLS through Nginx,
and includes Prometheus scraping and alert rules.

Production startup expects Alembic-managed schema state. Run `alembic upgrade
head` before the API, set `APP_ENV=production`, configure `REDIS_URL`, and run
`python worker.py` with `A2A_INGESTION_MODE=worker` for ingestion. See
`docs/PRODUCTION_RUNBOOK.md` for the full deployment sequence.

---

## A2A and NPCI Workflow

External NPCI calls use only:

```http
POST /a2a/external
Authorization: Bearer <NPCI_API_KEY>
```

Allowed methods: `agent.ingest_document`, `agent.receive_npci_reply`.

Admin frontend calls use only:

```http
POST /a2a/internal
Authorization: Bearer <admin JWT>
```

Allowed methods: `agent.get_summary`, `agent.query_rag`, `agent.escalate_to_npci`.

`agent.ingest_document` is asynchronous: the API stores the original file under `data/a2a_received/`, computes `content_hash = sha256(file_bytes)`, creates `received_documents` and `a2a_ingestion_jobs` rows, returns `job_id`, then a background worker extracts text, chunks, embeds, summarizes, and sends approved notifications. Duplicate hashes are not re-ingested.

Inbox tabs:

- Received Documents: filename, source, status (`queued`, `processing`, `extracting`, `embedding`, `ready`, `failed`), summary, retry-if-failed, and ask-about-this.
- NPCI Tickets: query, ticket ID, status (`pending`, `resolved`, `pending_review`, `failed`), and answer when available.

Notification safety: inbound `notify_list` values are filtered through `A2A_NOTIFY_ALLOWLIST` or `A2A_NOTIFY_ALLOWED_DOMAINS`; the static `A2A_NOTIFY_LIST` is always allowed.

NPCI replies are auto-ingested only when `NPCI_REPLY_AUTO_INGEST=true`. If disabled, replies are saved as `pending_review` for admin approval before they become RAG knowledge.

Low-confidence RAG uses:

```text
confidence = max(0.0, 1.0 - (best_distance / A2A_EMBEDDING_SCALE))
```

If confidence is below `A2A_CONFIDENCE_THRESHOLD`, `/chat/stream` sends a `low_confidence` SSE event, does not call Gemini, and shows the Raise to NPCI action.

Example `agent.ingest_document`:

```json
{"jsonrpc":"2.0","id":"1","method":"agent.ingest_document","params":{"filename":"circular.pdf","file_type":"pdf","content_b64":"...","notify_list":["ops@mindgate.in"],"metadata":{"source":"npci_agent"}}}
```

Example `agent.get_summary`:

```json
{"jsonrpc":"2.0","id":"2","method":"agent.get_summary","params":{"document_id":42}}
```

Example `agent.escalate_to_npci`:

```json
{"jsonrpc":"2.0","id":"3","method":"agent.escalate_to_npci","params":{"query":"What is the dispute timeline?","session_id":"...","context":{}}}
```

Example `agent.receive_npci_reply`:

```json
{"jsonrpc":"2.0","id":"4","method":"agent.receive_npci_reply","params":{"ticket_id":"TKT-123","answer":"NPCI answer text","source_doc":"NPCI callback"}}
```

For demos without a real NPCI endpoint, point `NPCI_AGENT_URL` to a mock JSON-RPC service that accepts `npci.raise_query`, returns a fake `ticket_id`, and calls back to `/a2a/external` with `agent.receive_npci_reply`.

---

## Re-ingestion & Auto-ingestion

If the policy PDF is updated, the app needs to process it. You have three options:

1. **Auto-ingestion (Watcher):** The watcher is fully integrated into the FastAPI application. When the API starts, it automatically monitors the `data/` directory. When a new/updated PDF is dropped, it places it in a sequential ingestion queue (avoiding concurrency or rate limits) and starts processing. You can also run it standalone using `python watcher.py`.
2. **CLI Ingestion:** Run `python ingest.py` manually.
3. **API Ingestion:** Make an authenticated POST request to `http://localhost:8000/ingest`.

---

## Notes

- **Vector Database:** Uses PostgreSQL with the `pgvector` extension for efficient cosine-similarity search.
- **Chat Memory:** Conversations are organized into sessions and saved to Postgres, enabling persistent history. The UI provides a collapsible history sidebar with the option to switch between and delete chats.
- **Streaming Response:** Generates token-by-token streaming response to provide a fast, responsive user interface.
- **Strict Grounding:** The bot **only** answers from the indexed policy document — it will refuse unrelated questions. For legal/regulatory interpretation, the bot directs users to the compliance team.

---

## License

Internal use only — Mindgate Solutions.
