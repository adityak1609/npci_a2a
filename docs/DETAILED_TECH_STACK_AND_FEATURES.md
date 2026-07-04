# Mindgate Policy Bot

## Detailed Technology Stack, Architecture, and Feature Functioning

This document describes the current implementation of the Mindgate Policy Bot in the repository root. It explains the technologies used, why they were selected, how the major components interact, and how each user-facing and administrative feature works.

> Implementation note: the workspace also contains a nested `mindgate-policy-bot/` copy. The root application currently contains the more complete implementation, including guardrails, admin endpoints, analytics, and the enhanced streaming UI. This document therefore treats the root files as the primary source of truth.

---

## Current A2A/NPCI Architecture Addendum

The active A2A design uses split endpoints by trust level:

- `POST /a2a/external`: NPCI-only JSON-RPC endpoint. It accepts only `Authorization: Bearer <NPCI_API_KEY>`. The frontend must never receive or use this key. Allowed methods are `agent.ingest_document` and `agent.receive_npci_reply`.
- `POST /a2a/internal`: admin-frontend JSON-RPC endpoint. It accepts only an admin JWT. Allowed methods are `agent.get_summary`, `agent.query_rag`, and `agent.escalate_to_npci`.
- `POST /a2a`: deprecated compatibility route. It no longer mixes NPCI API-key and admin JWT trust levels.

Inbound `agent.ingest_document` is queued. The route validates the JSON-RPC payload, stores the original file under `data/a2a_received/`, calculates `content_hash = sha256(file_bytes)`, creates `received_documents` and `a2a_ingestion_jobs` rows, returns `job_id` quickly, and processes extraction, chunking, embedding, summary generation, and email notification in a background task. Duplicate `content_hash` values are not re-ingested.

The ingestion lifecycle statuses are `queued`, `processing`, `extracting`, `embedding`, `ready`, and `failed`. Failed jobs can be retried with `POST /ingestion-jobs/{job_id}/retry`.

The active Inbox has two tabs:

- Received Documents: filename, source, lifecycle status, summary, retry-if-failed, and ask-about-this.
- NPCI Tickets: query, ticket ID, status (`pending`, `resolved`, `pending_review`, `failed`), and answer when available.

Document metadata is tracked through `ReceivedDocument`, `A2AIngestionJob`, `DocumentSummary.document_id`, and optional `DocumentChunk` fields such as `document_id`, `content_type`, `source_doc`, `ticket_id`, and `metadata_json`. `VoiceNote` and `MessageFeedback` may remain in the schema for legacy compatibility, but no active admin UI route depends on voice notes or answer feedback controls.

NPCI reply ingestion is controlled by `NPCI_REPLY_AUTO_INGEST`. When true, replies are embedded and marked `resolved`; when false, replies are stored as `pending_review` for admin approval before ingestion.

Inbound notification addresses from NPCI are filtered. The app uses `A2A_NOTIFY_LIST` plus optional `A2A_NOTIFY_ALLOWLIST` and `A2A_NOTIFY_ALLOWED_DOMAINS`; it does not blindly email arbitrary inbound `notify_list` recipients.

Low-confidence behavior is explicit:

```text
confidence = max(0.0, 1.0 - (best_distance / A2A_EMBEDDING_SCALE))
```

If confidence is below `A2A_CONFIDENCE_THRESHOLD`, `/chat/stream` emits a `low_confidence` SSE event, skips Gemini generation, and shows the Raise to NPCI action. Retrieval also applies a light source-priority rerank: `npci_official > npci_agent > admin_upload > old_policy`.

JSON-RPC examples:

```json
{"jsonrpc":"2.0","id":"1","method":"agent.ingest_document","params":{"filename":"circular.pdf","file_type":"pdf","content_b64":"...","notify_list":["ops@mindgate.in"],"metadata":{"source":"npci_agent"}}}
```

```json
{"jsonrpc":"2.0","id":"2","method":"agent.get_summary","params":{"document_id":42}}
```

```json
{"jsonrpc":"2.0","id":"3","method":"agent.escalate_to_npci","params":{"query":"What is the dispute timeline?","session_id":"...","context":{}}}
```

```json
{"jsonrpc":"2.0","id":"4","method":"agent.receive_npci_reply","params":{"ticket_id":"TKT-123","answer":"NPCI answer text","source_doc":"NPCI callback"}}
```

For demos, a mock NPCI agent can expose `npci.raise_query`, return a fake ticket ID, and call `/a2a/external` with `agent.receive_npci_reply`.

---

## 1. Project Purpose

The Mindgate Policy Bot is a Retrieval-Augmented Generation (RAG) application for answering employee questions about internal company policies.

Instead of allowing the language model to answer from general training knowledge, the system:

1. Extracts information from approved PDF documents.
2. Splits the extracted text into searchable chunks.
3. Converts each chunk into a semantic embedding.
4. Stores the text and embeddings in PostgreSQL.
5. Retrieves the most relevant chunks for each user question.
6. Gives those chunks to Google Gemini as controlled context.
7. Streams the generated answer to the browser.
8. Stores the conversation for future session history and follow-up questions.

The design goal is to provide fast, conversational access to internal policy information while maintaining authentication, document grounding, persistent history, administrative control, and several layers of prompt-injection protection.

---

## 2. High-Level Architecture

```text
┌─────────────────────────────────────────────────────────────┐
│                        Browser UI                           │
│ HTML + CSS + Vanilla JavaScript                            │
│                                                             │
│ Login • Chat • Streaming • History • Admin Dashboard       │
└──────────────────────────────┬──────────────────────────────┘
                               │ HTTP/JSON and SSE
                               ▼
┌─────────────────────────────────────────────────────────────┐
│                    FastAPI Application                      │
│                                                             │
│ Authentication       Chat/RAG          Administration       │
│ JWT + bcrypt         Guardrails        PDF management       │
│                      Retrieval         Usage analytics      │
│                      Gemini stream                          │
└───────────────┬───────────────────┬─────────────────────────┘
                │                   │
                │ SQL/pgvector      │ Gemini API
                ▼                   ▼
┌───────────────────────────┐  ┌──────────────────────────────┐
│ PostgreSQL 16 + pgvector  │  │ Google Gemini               │
│                           │  │                              │
│ users                     │  │ gemini-2.5-flash            │
│ chat_sessions             │  │ gemini-2.0-flash            │
│ chat_messages             │  │ gemini-embedding-001        │
│ document_chunks           │  └──────────────────────────────┘
└───────────────────────────┘
                ▲
                │ Extract, chunk, embed, store
┌───────────────┴─────────────────────────────────────────────┐
│             PDF Ingestion and File Watcher                  │
│ PyMuPDF • Watchdog • Sequential ingestion queue            │
└─────────────────────────────────────────────────────────────┘
```

### Main application modules

| File | Responsibility |
|---|---|
| `main.py` | FastAPI application, endpoints, RAG orchestration, streaming, admin operations, and frontend serving |
| `database.py` | SQLAlchemy engine, database models, indexes, pgvector setup, and session dependency |
| `auth.py` | Password hashing, JWT creation/validation, user registration, login, and role enforcement |
| `ingest.py` | PDF extraction, optional visual analysis, chunking, embedding generation, and database persistence |
| `retriever.py` | Query embedding and pgvector cosine-similarity retrieval |
| `prompt.py` | Grounded system prompt, security rules, answer rules, and context formatting |
| `guardrails.py` | Regex checks, semantic classification, input sanitization, document sanitization, and output leak detection |
| `watcher.py` | Automatic PDF change detection, debouncing, and sequential ingestion |
| `frontend/index.html` | Login, chat, streaming UI, session history, responsive layout, and admin dashboard |
| `docker-compose.yml` | Local PostgreSQL 16 and pgvector service |
| `start.ps1` | Windows launcher for Docker, PostgreSQL readiness, and Uvicorn |

---

## 3. Technology Stack Summary

| Layer | Technology | How it is used |
|---|---|---|
| Language | Python | Backend, AI integration, ingestion, authentication, and database logic |
| API framework | FastAPI | REST endpoints, dependencies, validation, static file serving, and SSE responses |
| ASGI server | Uvicorn | Runs the FastAPI application and supports asynchronous connections |
| AI generation | Gemini 2.5 Flash | Generates grounded policy answers and optionally analyzes PDF page images |
| AI classification | Gemini 2.0 Flash | Semantic prompt-injection and intent classifier |
| Embeddings | `gemini-embedding-001` | Converts document chunks and user queries into 3,072-dimensional vectors |
| Database | PostgreSQL 16 | Stores users, sessions, messages, document chunks, and analytics source data |
| Vector search | pgvector | Stores embeddings and performs cosine-distance similarity search |
| ORM | SQLAlchemy | Defines tables, relationships, queries, transactions, and connection pooling |
| PDF processing | PyMuPDF | Extracts PDF text and renders pages for optional visual analysis |
| Authentication | JWT with `python-jose` | Creates signed bearer tokens for protected requests |
| Password security | bcrypt | Salted one-way password hashing and password verification |
| Validation | Pydantic | Defines API request and response schemas |
| Configuration | python-dotenv | Loads API keys, database settings, and auth configuration from `.env` |
| Upload parsing | python-multipart | Enables multipart PDF uploads through FastAPI |
| File monitoring | Watchdog | Detects created and modified PDF files |
| Concurrency | asyncio + threads | Runs blocking SDK/database-related work without blocking the event loop |
| Streaming | Server-Sent Events | Streams model output over a standard HTTP response |
| Frontend | HTML, CSS, Vanilla JavaScript | Implements the complete user and admin interface without a build system |
| Browser persistence | Local Storage | Stores the JWT, user information, admin flag, and active session ID |
| Containers | Docker Compose | Starts PostgreSQL with the pgvector extension available |
| Launcher | PowerShell | Starts Docker, waits for PostgreSQL, and launches the API server |

---

## 4. Why Each Technology Was Chosen

### 4.1 Python

Python is the primary backend language.

Why it fits this project:

- Google provides a supported Gemini SDK for Python.
- PyMuPDF, SQLAlchemy, FastAPI, bcrypt, and pgvector all have mature Python integrations.
- AI pipelines involve text transformation and orchestration, which Python expresses clearly.
- The language allows the API, ingestion worker, security logic, and database layer to remain in one ecosystem.

Trade-off:

- Some operations and SDK iterators are blocking. The application handles this by moving blocking work into threads with `asyncio.to_thread()` or an executor.

### 4.2 FastAPI

FastAPI is the application framework responsible for all HTTP endpoints.

Why it was chosen:

- Native ASGI and `async` support suit long-running streaming connections.
- Dependency injection makes reusable authentication and database-session checks straightforward.
- Pydantic request and response models are integrated automatically.
- Swagger UI and OpenAPI documentation are generated at `/docs`.
- `StreamingResponse` supports SSE without requiring a WebSocket server.
- Static files and the frontend can be served from the same application.

How it is used:

- `Depends(get_db)` creates and cleans up database sessions.
- `Depends(get_current_user)` protects user endpoints.
- `Depends(get_current_admin)` protects administrative endpoints.
- `UploadFile` and `File` accept PDF uploads.
- `FileResponse` serves the browser interface.
- `StreamingResponse` carries incremental chat events.

### 4.3 Uvicorn

Uvicorn is the ASGI server used to run FastAPI.

Why it was chosen:

- It is lightweight and is the standard development/runtime server for FastAPI.
- It supports asynchronous request handling and long-lived SSE responses.
- The `uvicorn[standard]` package includes useful performance and protocol dependencies.

The launcher runs:

```powershell
python -m uvicorn main:app --host 0.0.0.0 --port 8000
```

### 4.4 Google Gemini SDK

The project uses the `google-genai` package for generation, classification, embeddings, and optional vision processing.

#### Gemini 2.5 Flash

Used for:

- Main policy answer generation.
- Streaming answer generation.
- Optional visual analysis of PDF pages when `USE_VISION=True`.

Why it was chosen:

- Low latency is important for a conversational interface.
- It supports streaming output.
- Its context capacity is suitable for retrieved policy chunks and recent conversation history.
- Multimodal support allows the same model family to process rendered PDF pages.

#### Gemini 2.0 Flash

Used for:

- Tier 2 semantic security classification in `guardrails.py`.

Why a separate classifier is useful:

- Semantic classification catches attacks that simple regex patterns miss.
- A dedicated model call keeps security logic independent from answer generation.
- The classifier can reason about user intent instead of matching only exact phrases.

#### `gemini-embedding-001`

Used for:

- Document chunk embeddings during ingestion.
- User-question embeddings during retrieval.

Current vector dimension:

```text
3,072 dimensions
```

Why embeddings are required:

- Keyword matching fails when the question and document use different wording.
- Embeddings map semantically similar text near each other in vector space.
- The same model is used for documents and questions, ensuring compatible vectors.

### 4.5 PostgreSQL 16

PostgreSQL is the application's persistent data store.

It stores:

- Registered users.
- Password hashes and roles.
- Chat sessions.
- User and assistant messages.
- Per-answer user ratings, reasons, and comments.
- PDF chunk text.
- Embedding vectors.
- The source data used by admin analytics.

Why it was chosen:

- It is reliable, transactional, and widely supported.
- Relational constraints are valuable for user/session/message ownership.
- It can also perform vector search through pgvector.
- One database is simpler to operate than separate relational and vector databases.
- SQL aggregation supports analytics without an additional reporting system.

### 4.6 pgvector

pgvector extends PostgreSQL with a vector column type and vector-distance operators.

How it is used:

- `document_chunks.embedding` is a `Vector(3072)` column.
- Query vectors are compared using the cosine-distance operator `<=>`.
- Results are ordered from lowest distance to highest distance.
- The top five chunks are returned by default.

The retrieval query is conceptually:

```sql
SELECT text
FROM document_chunks
ORDER BY embedding <=> query_vector
LIMIT 5;
```

Why it was chosen:

- Embeddings and source text remain in the same database.
- No separate vector service or API is required.
- PostgreSQL filtering, transactions, backups, and access controls remain available.
- An IVFFlat index can accelerate larger datasets.

The application attempts to create an IVFFlat cosine index after at least 100 chunks exist:

```text
Index type: IVFFlat
Lists: 50
Operator class: vector_cosine_ops
```

### 4.7 SQLAlchemy

SQLAlchemy is the ORM and database toolkit.

Why it was chosen:

- Models document the schema in Python.
- Relationships and cascade deletion simplify session ownership.
- Transactions make ingestion and account operations safer.
- Connection pooling is configurable.
- Raw SQL remains available for pgvector-specific queries.

Current connection-pool settings:

| Setting | Value | Purpose |
|---|---:|---|
| `pool_size` | 20 | Persistent database connections |
| `max_overflow` | 30 | Temporary extra connections during bursts |
| `pool_timeout` | 30 seconds | Maximum wait for a free connection |
| `pool_recycle` | 1,800 seconds | Replaces older connections to reduce stale-connection issues |
| `pool_pre_ping` | Enabled | Tests pooled connections before use |

### 4.8 PyMuPDF

PyMuPDF is imported as `fitz` and handles PDF processing.

How it is used:

- Opens PDF files.
- Reads page count.
- Extracts page text with `page.get_text("text")`.
- Renders pages as PNG images when visual analysis is enabled.

Why it was chosen:

- It is fast and supports both extraction and rendering.
- A single library handles textual and visual PDF workflows.
- Page boundaries can be added to extracted text for better source context.

### 4.9 JWT and `python-jose`

JWT bearer tokens provide API authentication.

Basic flow:

1. A user registers or logs in.
2. The server creates a signed token containing the user's email in the `sub` claim.
3. The token receives an expiration timestamp.
4. The browser sends the token in the `Authorization: Bearer ...` header.
5. Protected endpoints decode the token and load the matching user.

Current settings:

| Setting | Default |
|---|---|
| Algorithm | HS256 |
| Token duration | 1,440 minutes / 24 hours |
| Identity claim | `sub` containing the email address |

Why JWT was chosen:

- It works naturally with browser `fetch()` requests.
- The API does not need a separate server-side session store.
- Role checks can be layered on top of the authenticated user lookup.

### 4.10 bcrypt

bcrypt securely hashes user passwords.

How it works:

- Registration calls `bcrypt.gensalt()` and `bcrypt.hashpw()`.
- Only the resulting hash is stored.
- Login uses `bcrypt.checkpw()` to compare the submitted password with the stored hash.

Why it was chosen:

- bcrypt is intentionally computationally expensive, making password cracking harder.
- Each password receives a salt.
- Passwords cannot be decrypted from the database.

### 4.11 Pydantic

Pydantic models define structured request and response data.

Examples:

- `UserCreate`
- `UserLogin`
- `Token`
- `ChatRequest`
- `ChatResponse`

Why it was chosen:

- FastAPI uses Pydantic automatically.
- Invalid JSON shapes receive standardized validation responses.
- Response models document API contracts.

### 4.12 python-dotenv

`python-dotenv` loads local environment variables from `.env`.

Used for:

- `GOOGLE_API_KEY`
- `DATABASE_URL`
- `SECRET_KEY`
- `ACCESS_TOKEN_EXPIRE_MINUTES`

Why it was chosen:

- Secrets and deployment-specific configuration remain outside source code.
- Local setup is simple.
- The same code can use real environment variables in deployment.

### 4.13 Watchdog

Watchdog monitors the `data/` directory for PDF creation and modification.

Why it was chosen:

- Event-driven monitoring avoids constant polling.
- New policy files can become searchable without manually restarting the app.
- It integrates with a debounce timer and a controlled ingestion queue.

### 4.14 Server-Sent Events

SSE streams responses from `/chat/stream`.

Why SSE was chosen:

- Chat output flows in one direction: server to browser.
- It uses standard HTTP infrastructure.
- It is simpler than WebSockets for one-way token streaming.
- It works with authenticated `fetch()` requests and a readable response body.

### 4.15 HTML, CSS, and Vanilla JavaScript

The frontend is a single self-contained `frontend/index.html`.

Why this approach was chosen:

- No Node.js build step is required.
- There is no frontend framework runtime or dependency tree.
- FastAPI can serve the UI directly.
- The deployment consists primarily of Python code and one HTML asset.

The trade-off is that the single file is large and combines presentation, state, API calls, and rendering logic. A framework or modular JavaScript structure may become beneficial as the interface grows.

### 4.16 Docker Compose

Docker Compose starts the database from:

```text
pgvector/pgvector:pg16
```

Why it was chosen:

- Developers do not need to install PostgreSQL or pgvector manually.
- Database versions remain consistent.
- The named volume `pgdata` preserves database data across container restarts.
- Port `5432` is exposed for the application and development tools.

### 4.17 PowerShell

`start.ps1` automates local Windows startup.

It:

1. Checks whether Docker is running.
2. Attempts to start Docker Desktop if needed.
3. Runs `docker compose up -d`.
4. Waits until PostgreSQL accepts connections.
5. Starts Uvicorn on port 8000.

This reduces setup friction for developers who are not comfortable starting each service separately.

---

## 5. Database Design

### 5.1 Entity relationships

```text
users
  │ 1
  │
  │ many
chat_sessions
  │ 1
  │
  │ many
chat_messages

document_chunks
  Independent indexed policy content
```

### 5.2 `users`

| Column | Type | Purpose |
|---|---|---|
| `id` | Integer primary key | Internal user identifier |
| `email` | String, unique, indexed | Login identity |
| `hashed_password` | String | bcrypt password hash |
| `full_name` | Nullable string | Display name |
| `is_admin` | Boolean | Grants access to admin endpoints |
| `created_at` | Date/time | Account creation time |

### 5.3 `chat_sessions`

| Column | Type | Purpose |
|---|---|---|
| `id` | 36-character UUID string | Conversation identifier generated by the browser |
| `user_id` | Foreign key | Session owner |
| `created_at` | Date/time | Session creation time |

Deleting a user cascades to the user's sessions.

### 5.4 `chat_messages`

| Column | Type | Purpose |
|---|---|---|
| `id` | Integer primary key | Message identifier |
| `session_id` | Foreign key | Parent conversation |
| `role` | String | `user` or `assistant` |
| `content` | Text | Message body |
| `created_at` | Date/time | Message creation time |

Deleting a session cascades to its messages.

### 5.5 `document_chunks`

| Column | Type | Purpose |
|---|---|---|
| `id` | Integer primary key | Chunk identifier |
| `source_file` | String | Original PDF filename |
| `chunk_index` | Integer | Order within the source PDF |
| `text` | Text | Extracted and sanitized chunk text |
| `embedding` | Vector(3072) | Gemini semantic embedding |

A unique constraint on `(source_file, chunk_index)` prevents duplicate positions for the same document.

### 5.6 `message_feedback`

| Column | Type | Purpose |
|---|---|---|
| `id` | Integer primary key | Feedback identifier |
| `message_id` | Foreign key | Rated assistant message |
| `user_id` | Foreign key | User who submitted the rating |
| `rating` | String | `positive` or `negative` |
| `reason` | Nullable string | Structured reason such as incorrect, incomplete, or unclear |
| `comment` | Nullable text | Optional explanation of the rating |
| `created_at` | Date/time | Initial submission time |
| `updated_at` | Date/time | Most recent update time |

Each user can maintain one feedback record per assistant message. Submitting another rating updates the existing record.

---

## 6. PDF Ingestion Feature

### 6.1 Ways ingestion can start

Ingestion can be triggered through:

- `python ingest.py`
- Authenticated `POST /ingest`
- Admin PDF upload
- Watchdog after a PDF is created or modified in `data/`

### 6.2 Ingestion flow

```text
PDF file
   │
   ▼
Extract page text with PyMuPDF
   │
   ├── Optional: render each page and ask Gemini Vision about visuals
   │
   ▼
Add page markers and combine extracted content
   │
   ▼
Split into overlapping chunks
   │
   ▼
Sanitize suspicious document instructions
   │
   ▼
Generate one embedding per chunk
   │
   ▼
Delete older chunks for the same source file
   │
   ▼
Insert new chunks and vectors into PostgreSQL
```

### 6.3 Chunking strategy

Current configuration:

| Setting | Value |
|---|---:|
| Approximate chunk size | 800 tokens |
| Approximate overlap | 100 tokens |
| Token approximation | 1 token ≈ 0.75 words |
| Approximate words per chunk | 600 |
| Approximate overlap words | 75 |

Why overlap is used:

- Important sentences can cross chunk boundaries.
- Repeating a small amount of text preserves local context.
- Retrieval is less likely to miss an answer located near a split.

### 6.4 Embedding generation

Each chunk is embedded separately with `gemini-embedding-001`.

The pipeline:

- Retries transient errors such as rate limits and temporary service failures.
- Uses exponential backoff.
- Sleeps between chunks to reduce rate-limit pressure.
- Stores the returned vector alongside the original chunk.

### 6.5 Re-ingestion behavior

Before inserting new chunks, the application deletes all existing chunks with the same `source_file`.

This prevents:

- Duplicate chunks.
- Old document versions remaining searchable.
- Mixed answers from outdated and current copies of the same filename.

### 6.6 Optional visual understanding

`USE_VISION` is currently `False`.

When enabled:

1. PyMuPDF renders each page at 150 DPI.
2. The PNG is sent to Gemini 2.5 Flash.
3. Gemini describes tables, charts, diagrams, infographics, and embedded text.
4. The description is appended to the page's extracted text.
5. The combined information is chunked and indexed.

This feature is disabled by default because it increases API usage, latency, and quota requirements.

---

## 7. Automatic PDF Monitoring

The watcher starts during the FastAPI lifespan startup event.

### Flow

1. Watchdog listens for `.pdf` creation and modification events.
2. Each file receives a five-second debounce timer.
3. Repeated events reset the timer.
4. After the file stops changing, its path enters a bounded queue.
5. One daemon worker ingests queued files sequentially.

Why sequential processing is used:

- Multiple simultaneous PDF embeddings could overwhelm Gemini quotas.
- It avoids competing database replacement operations.
- Logs remain easier to understand.

The queue can hold up to 50 pending files.

Admin uploads temporarily suppress watcher events for the uploaded path because the upload endpoint already performs ingestion itself.

---

## 8. RAG Question-Answering Feature

### 8.1 End-to-end request flow

```text
Authenticated user question
        │
        ▼
Fast regex security check
        │
        ▼
Question sanitization
        │
        ▼
Load/create session and recent history
        │
        ├──────────────────────────────┐
        ▼                              ▼
Semantic guardrail check       Embed sanitized question
                                       │
                                       ▼
                              pgvector similarity search
                                       │
        └──────────────────────┬────────┘
                               ▼
                    Top five policy chunks
                               │
                               ▼
              Build grounded Gemini prompt
                               │
                               ▼
                  Generate or stream answer
                               │
                               ▼
                    Output leak inspection
                               │
                               ▼
                   Save both messages
```

### 8.2 Retrieval

The question is embedded with the same embedding model used during ingestion.

The retriever:

1. Checks that at least one document chunk exists.
2. Embeds the question.
3. Converts the vector to PostgreSQL vector syntax.
4. Orders chunks by cosine distance.
5. Returns the five closest text chunks.

### 8.3 Grounded prompt construction

Retrieved chunks are labeled:

```text
[Chunk 1]
...

[Chunk 2]
...
```

The system prompt instructs Gemini to:

- Answer only from retrieved context.
- Avoid outside knowledge.
- Avoid inventing policy clauses or citations.
- State when context is insufficient.
- Explain policy conditions instead of giving misleading one-word answers.
- Identify contradictions rather than silently resolving them.
- Remain an informational assistant rather than an approver or legal authority.

### 8.4 Conversation memory

The application includes the last five user/assistant pairs, equivalent to a maximum of ten previous messages.

History provides:

- Follow-up question continuity.
- Pronoun and topic resolution.
- A more natural conversation.

Persisted user history is sanitized before reuse to reduce memory-poisoning risk.

### 8.5 Parallel request preparation

The semantic guardrail check and document retrieval are independent, so the application starts them together with `asyncio.gather()`.

This reduces total latency compared with waiting for one operation before starting the other.

---

## 9. Security Guardrails

The project uses defense in depth rather than relying only on the model prompt.

### 9.1 Tier 1: Regex fast path

Precompiled patterns detect common attacks such as:

- Ignoring or replacing system instructions.
- Requesting internal prompts.
- Asking for raw retrieved chunks.
- Role-play as HR, management, legal, or an approver.
- Fabricating citations.
- Poisoning future conversation memory.
- Encoding or reformatting hidden data.
- Forcing misleading yes/no output.

Benefits:

- Immediate response.
- No model cost.
- Prevents obvious attacks before retrieval or generation.

### 9.2 Tier 2: Semantic model classifier

Questions that pass regex checks are classified by Gemini 2.0 Flash.

This layer looks at intent and can catch:

- Paraphrased attacks.
- Indirect extraction attempts.
- Creative role or instruction overrides.
- Attacks that do not use known keywords.

### 9.3 Question sanitization

The question is cleaned before retrieval and generation.

The original question is still stored in chat history for an accurate user-visible record, while the sanitized version is used in sensitive model operations.

### 9.4 Document sanitization

Policy documents can themselves contain malicious or accidental instruction-like text.

During ingestion, suspicious patterns are wrapped as document text so they are less likely to be interpreted as executable instructions by the model.

### 9.5 Output guard

The completed answer is scanned for:

- System prompt fragments.
- Grounding or security rule leakage.
- Multiple raw chunk markers.
- Large-scale reproduction of retrieved chunks.

If contamination is detected, the answer is replaced with a safe fallback.

For streaming requests, the backend can send a final `replace` event so the browser replaces already streamed unsafe content with the validated fallback.

---

## 10. Streaming Chat Feature

### 10.1 Backend streaming

Gemini's streaming iterator is blocking, so it runs in an executor thread.

The thread sends events into an `asyncio.Queue`:

```text
("token", text)
("error", error message)
("done", None)
```

The asynchronous SSE generator reads from the queue and immediately yields JSON events.

Example token event:

```text
data: {"token":"Policy text"}
```

Completion event:

```text
data: {"done":true,"chunks_used":5}
```

Relevant response headers reduce proxy buffering:

- `Cache-Control: no-cache, no-transform`
- `Connection: keep-alive`
- `X-Accel-Buffering: no`
- `X-Content-Type-Options: nosniff`

### 10.2 Frontend streaming

The browser:

1. Calls `/chat/stream` with `fetch()`.
2. Reads `response.body` with a `ReadableStream` reader.
3. Decodes bytes with `TextDecoder`.
4. Separates complete SSE events.
5. Parses each JSON payload.
6. Feeds received text into a smooth presentation buffer.

### 10.3 Smooth rendering behavior

Provider chunks can contain several characters or words at once. Rendering each chunk immediately can look jumpy.

The current UI:

- Buffers received text.
- Reveals text incrementally with `requestAnimationFrame`.
- Uses an adaptive catch-up rate when the buffer grows.
- Displays a blinking caret while streaming.
- Re-renders lightweight Markdown as text becomes visible.
- Applies the final complete Markdown when streaming finishes.
- Auto-scrolls only when the user remains close to the bottom.

This separates network chunk size from visual typing speed and produces a more polished experience.

---

## 11. Authentication and Authorization Features

### 11.1 Registration

The registration form submits:

- Full name.
- Email address.
- Password.

The backend:

1. Checks for an existing email.
2. Hashes the password with bcrypt.
3. Creates the user.
4. Generates a JWT.
5. Returns the token and public user information.

### 11.2 Login

The login endpoint:

1. Loads the user by email.
2. Verifies the bcrypt hash.
3. Returns HTTP 401 for invalid credentials.
4. Creates a new JWT for valid credentials.

### 11.3 Protected endpoints

`get_current_user`:

1. Reads the bearer token.
2. Validates its signature and expiration.
3. Extracts the email from `sub`.
4. Loads the current user from PostgreSQL.

### 11.4 Admin authorization

`get_current_admin` first authenticates the user and then checks `user.is_admin`.

Non-admin users receive HTTP 403 for `/admin/*` endpoints.

### 11.5 Browser persistence

The browser stores:

- JWT.
- Email.
- Display name.
- Admin flag.
- Active chat session ID.

Sign-out removes these values and reloads the application.

---

## 12. Chat Session and History Features

### 12.1 Session creation

The browser creates a UUID-like session ID and stores it locally.

On the first message, the backend:

- Verifies whether the session exists.
- Creates it if needed.
- Associates it with the authenticated user.

### 12.2 Message persistence

After a completed response, the backend stores:

- The original user question.
- The final assistant answer.

### 12.3 Session sidebar

The frontend fetches `/chat/sessions` and displays:

- A preview from the first user message.
- Message count.
- Creation date.
- Active-session styling.

Sessions are grouped into:

- Today.
- Yesterday.
- This Week.
- Older.

### 12.4 Loading history

When a session is selected, the browser requests `/chat/history/{session_id}` and rebuilds the conversation.

Ownership checks ensure users cannot read another user's sessions.

### 12.5 Session deletion

Deleting a session:

1. Confirms the action in the browser.
2. Calls `DELETE /chat/sessions/{session_id}`.
3. Verifies ownership on the backend.
4. Deletes the session.
5. Cascades deletion to all child messages.

---

## 13. Admin Dashboard Features

### 13.1 Analytics

`GET /admin/dashboard` calculates:

- Total non-admin users.
- Total chat sessions.
- Total messages.
- Total user queries.
- Queries today.
- Query counts for the last 14 days.
- Top ten users by query count.
- Twenty most recent user questions.

The frontend renders:

- Summary statistic cards.
- Helpful-answer rate, total feedback count, and negative-feedback count.
- A 14-day bar chart.
- A top-user table.
- A recent-query activity list.
- A recent-feedback review list containing ratings, reasons, comments, users, and answer excerpts.

### 13.2 PDF listing

`GET /admin/pdfs` combines database and filesystem information:

- Filename.
- Number of indexed chunks.
- File size.
- Last-modified timestamp.

### 13.3 PDF upload

The admin interface supports:

- Click-to-select.
- Drag and drop.
- PDF extension validation.
- Multipart upload.
- Visible ingestion progress state.

The backend:

1. Saves the file in `data/`.
2. Suppresses duplicate watcher ingestion.
3. Runs ingestion in a background thread.
4. Returns the resulting filename, chunk count, and embedding dimension.
5. Removes the saved file if ingestion fails.

### 13.4 PDF deletion

The admin can delete a PDF and its indexed content.

The backend:

1. Rejects obvious path traversal patterns.
2. Deletes matching database chunks.
3. Deletes the physical file when present.
4. Returns the number of removed chunks.

---

## 14. Frontend UX Features

The single-page browser interface provides:

- Admin login form.
- Password visibility controls.
- Loading and validation states.
- Persistent authentication state.
- Responsive chat layout.
- Desktop and mobile history sidebar behavior.
- New-conversation action.
- Suggested starter questions.
- Typing indicator.
- Smooth streamed output.
- Markdown formatting.
- User and assistant avatars.
- Message timestamps.
- Toast notifications.
- Conversation suggestions after answers.
- Session grouping and deletion animations.
- Admin document list, upload panel, chat, and Inbox tabs.
- Responsive styles for smaller screens.
- Accessible labels and an `aria-live` toast region.

Why these details matter:

- Streaming and typing feedback reduce perceived latency.
- Persistent history makes the bot useful for ongoing policy research.
- Responsive navigation keeps the application usable on laptops and phones.
- Clear error states prevent failed API requests from feeling like frozen UI.

---

## 15. API Endpoint Reference

| Method | Endpoint | Authentication | Function |
|---|---|---|---|
| `GET` | `/` | No | Serves the frontend |
| `GET` | `/health` | No | Returns API and index readiness status |
| `POST` | `/auth/admin/login` | No | Validates administrator credentials and returns a JWT |
| `POST` | `/auth/login` | No | Deprecated admin-only compatibility login |
| `POST` | `/ingest` | Admin | Ingests the default `data/policy.pdf` |
| `POST` | `/chat` | Admin | Returns a complete non-streaming answer |
| `POST` | `/chat/stream` | Admin | Streams answer events over SSE |
| `POST` | `/a2a/external` | NPCI API key | NPCI-only JSON-RPC calls |
| `POST` | `/a2a/internal` | Admin | Admin-frontend JSON-RPC calls |
| `GET` | `/documents` | Admin | Lists received A2A documents and ingestion statuses |
| `POST` | `/ingestion-jobs/{job_id}/retry` | Admin | Retries a failed A2A ingestion job |
| `GET` | `/npci-tickets` | Admin | Lists NPCI escalation tickets |
| `POST` | `/feedback/{message_id}` | Admin | Legacy compatibility endpoint; no active feedback UI |
| `GET` | `/chat/sessions` | Admin | Lists the current admin's sessions |
| `GET` | `/chat/history/{session_id}` | Admin | Returns owned session messages without feedback metadata |
| `DELETE` | `/chat/sessions/{session_id}` | Admin | Deletes an owned session |
| `GET` | `/admin/dashboard` | Admin | Deprecated compatibility analytics route; not in active UI navigation |
| `GET` | `/admin/pdfs` | Admin | Lists indexed PDF information |
| `POST` | `/admin/upload` | Admin | Uploads and ingests a PDF |
| `DELETE` | `/admin/pdfs/{filename}` | Admin | Deletes a PDF and its chunks |

FastAPI also provides:

- Swagger UI at `/docs`.
- ReDoc at `/redoc`.
- OpenAPI schema at `/openapi.json`.

---

## 16. Environment Configuration

Recommended `.env` values:

```env
GOOGLE_API_KEY=replace_with_your_google_api_key
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/mindgate_db
SECRET_KEY=replace_with_a_long_random_secret
ACCESS_TOKEN_EXPIRE_MINUTES=1440
```

| Variable | Required | Purpose |
|---|---|---|
| `GOOGLE_API_KEY` | Yes | Gemini generation, classification, vision, and embeddings |
| `DATABASE_URL` | No | Overrides the default local PostgreSQL connection |
| `SECRET_KEY` | Strongly recommended | Signs and validates JWTs |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | No | Controls bearer-token lifetime |
| `NPCI_AGENT_URL` | For NPCI escalation | Outbound NPCI JSON-RPC URL |
| `NPCI_API_KEY` | For external A2A | Shared secret accepted only by `/a2a/external` and sent only to NPCI |
| `NPCI_REPLY_AUTO_INGEST` | No | Auto-ingests NPCI replies when true; otherwise stores `pending_review` |
| `A2A_NOTIFY_LIST` | No | Static approved notification recipients |
| `A2A_NOTIFY_ALLOWLIST` | No | Explicit inbound email allowlist |
| `A2A_NOTIFY_ALLOWED_DOMAINS` | No | Approved domains for inbound `notify_list` recipients |

---

## 17. Startup and Runtime Flow

### Local startup

```powershell
.\start.ps1
```

The startup sequence is:

1. Ensure Docker is available.
2. Start PostgreSQL and pgvector.
3. Wait for PostgreSQL readiness.
4. Start Uvicorn.
5. FastAPI creates the pgvector extension and tables.
6. Existing indexes are created if absent.
7. The vector index is created when enough chunks exist.
8. The PDF watcher and ingestion worker start.
9. The UI becomes available at `http://localhost:8000`.

### FastAPI shutdown

During shutdown:

- The Watchdog observer stops.
- The ingestion worker receives a stop signal.
- The worker receives a short grace period to finish.

---

## 18. Declared Dependencies and Current Usage Status

The following packages appear in `requirements.txt`.

| Dependency | Status in current source |
|---|---|
| `fastapi` | Actively used |
| `uvicorn[standard]` | Actively used to run the app |
| `python-dotenv` | Actively used |
| `pydantic` | Actively used |
| `pydantic-settings` | Declared, but no active settings class is currently implemented |
| `google-genai` | Actively used |
| `PyMuPDF` | Actively used |
| `psycopg2-binary` | Used as the PostgreSQL driver behind SQLAlchemy |
| `pgvector` | Actively used |
| `sqlalchemy` | Actively used |
| `alembic` | Declared, but there is no Alembic migration directory/configuration in the current repository |
| `python-jose[cryptography]` | Actively used |
| `bcrypt` | Actively used |
| `python-multipart` | Required by the active upload endpoint |
| `numpy` | Imported in ingestion/retrieval modules, but current vector operations do not require it directly |
| `watchdog` | Actively used |

This distinction is useful during dependency cleanup and future maintenance.

---

## 19. Performance Design Choices

### Asynchronous API handling

FastAPI endpoints use `async` where network or streaming behavior benefits from it.

### Blocking work offloading

Gemini calls, ingestion, and some retrieval operations are moved to threads.

### Larger executor

The application configures a default thread pool with up to 100 workers to support concurrent blocking SDK work.

### Database pooling

SQLAlchemy maintains reusable connections and allows temporary overflow under load.

### Parallel guardrail and retrieval

The semantic classifier and vector retrieval run simultaneously.

### Approximate vector indexing

IVFFlat is created for larger chunk collections to improve retrieval speed.

### Frontend animation-frame rendering

Streamed content is rendered at the browser's visual refresh rate instead of on every raw network fragment.

---

## 20. Reliability and Error Handling

The application includes:

- Retry logic for transient Gemini generation failures.
- Retry and backoff for embeddings.
- Retry behavior for optional visual analysis.
- HTTP 400 when no index exists or the default PDF is missing.
- HTTP 401 for invalid or expired authentication.
- HTTP 403 for non-admin access to admin endpoints.
- HTTP 404 for missing sessions or PDFs.
- Safe cleanup when uploaded-file ingestion fails.
- Database session cleanup through `finally` blocks.
- Stream error events for failures occurring after an SSE connection begins.
- A bounded ingestion queue to avoid unlimited background work.
- File-change debouncing to avoid ingesting partially written PDFs.

---

## 21. Current Limitations and Production Considerations

The current implementation is well suited to local development and an internal prototype, but the following items should be addressed before a production rollout.

### Admin provisioning

Public registration always creates a standard user and does not accept an
`is_admin` field. The UI and API expose separate user and administrator login
paths, and each route validates the account role before issuing a token.

Admin roles must be assigned through a trusted database operation or a future
protected administrative provisioning workflow.

### JWT secret default

The fallback secret is `change-me`.

Production recommendation:

- Refuse startup when `SECRET_KEY` is missing.
- Use a long cryptographically random value.
- Rotate the secret through a managed secret store.

### Local Storage token risk

JWTs are stored in browser Local Storage, which is accessible to JavaScript.

Production recommendation:

- Apply a strict Content Security Policy.
- Audit all HTML insertion paths.
- Consider secure, `HttpOnly`, `SameSite` cookies if architecture permits.

### CORS configuration

The API currently allows all origins.

Production recommendation:

- Restrict CORS to known application origins.
- Review credential behavior and allowed headers.

### Upload hardening

The upload endpoint primarily validates the filename extension.

Production recommendation:

- Sanitize uploaded filenames.
- Use `os.path.basename()` or generated storage names.
- Check PDF magic bytes/content type.
- Enforce upload size and page-count limits.
- Scan documents before processing if required by company security policy.

### Database migrations

Schema updates currently use `create_all()` and manual `ALTER TABLE`/index statements.

Production recommendation:

- Initialize Alembic.
- Store every schema change as a versioned migration.
- Test upgrades and rollbacks.

### Vector indexing lifecycle

The IVFFlat index is attempted during startup only when at least 100 chunks already exist.

Production recommendation:

- Ensure index creation or rebuilding also occurs after large ingestion operations.
- Analyze index parameters against the real document count.
- Consider HNSW if retrieval characteristics favor it.

### Observability

The application currently relies mainly on console output.

Production recommendation:

- Add structured logging.
- Add request IDs and latency metrics.
- Monitor Gemini failures, retrieval duration, ingestion duration, queue depth, and stream disconnects.
- Add database and service health checks.

### Testing

The repository contains guardrail tests, but broad automated coverage is limited.

Production recommendation:

- Add unit tests for auth, chunking, retrieval, and session ownership.
- Add integration tests against a temporary PostgreSQL/pgvector database.
- Add SSE parsing and stream interruption tests.
- Add frontend browser tests.
- Add adversarial prompt and malicious-document regression suites.

### Data privacy

The admin dashboard displays recent employee questions.

Production recommendation:

- Define retention rules.
- Limit access to authorized roles.
- Consider redaction of personal or sensitive information.
- Audit administrative access.

---

## 22. Suggested Future Improvements

- Split the frontend into separate HTML, CSS, and JavaScript modules.
- Add Alembic migration management.
- Remove unused dependencies and imports.
- Add document metadata such as page number, title, and section to retrieval results.
- Return source metadata with answers.
- Add hybrid semantic and keyword search.
- Add a minimum similarity threshold to reject weak retrieval results.
- Add reranking before generation.
- Add document-level access controls for teams or departments.
- Support token revocation or refresh-token rotation.
- Add rate limiting per user.
- Add background-job infrastructure for large ingestion workloads.
- Add object storage for documents in multi-instance deployments.
- Add centralized logging and metrics.
- Add production deployment configuration with TLS and a reverse proxy.

---

## 23. Feature-to-Component Map

| Feature | Frontend | API/orchestration | Supporting module | Storage/external service |
|---|---|---|---|---|
| Admin login | Admin login form | `/auth/admin/login` | `auth.py` | PostgreSQL |
| JWT validation | Auth headers | FastAPI dependencies | `auth.py` | PostgreSQL user lookup |
| PDF ingestion | Admin upload UI | `/ingest`, `/admin/upload` | `ingest.py` | Gemini + PostgreSQL |
| Auto-ingestion | None | Lifespan startup | `watcher.py` | Local filesystem |
| Semantic retrieval | Chat submission | `/chat`, `/chat/stream` | `retriever.py` | Gemini embeddings + pgvector |
| Grounded answers | Chat messages | RAG orchestration | `prompt.py` | Gemini 2.5 Flash |
| Guardrails | Error/refusal display | Chat endpoints | `guardrails.py` | Gemini 2.0 Flash |
| Streaming | Smooth message renderer | `/chat/stream` | `main.py` queue/generator | Gemini stream |
| Legacy feedback compatibility | None in active UI | `/feedback/{message_id}` | `main.py`, `database.py` | PostgreSQL |
| Chat memory | Session UI | History loading | `main.py`, `database.py` | PostgreSQL |
| Session deletion | Sidebar action | Session delete endpoint | `main.py` | PostgreSQL cascade |
| A2A external RPC | NPCI system | `/a2a/external` | `routes/a2a_routes.py`, `agent/dispatcher.py` | NPCI API key |
| A2A internal RPC | Admin Inbox/chat actions | `/a2a/internal` | `routes/a2a_routes.py`, `agent/dispatcher.py` | Admin JWT |
| Received documents | Inbox tab | `/documents` | `services/a2a_ingestor.py` | `received_documents`, `a2a_ingestion_jobs` |
| Failed ingestion retry | Inbox retry button | `/ingestion-jobs/{job_id}/retry` | `services/a2a_ingestor.py` | PostgreSQL |
| NPCI tickets | Inbox tab | `/npci-tickets` | `services/npci_client.py`, `services/npci_reply.py` | PostgreSQL + WebSocket |
| Admin analytics | Dashboard charts/tables | `/admin/dashboard` | `main.py` SQL aggregates | PostgreSQL |
| PDF management | PDF cards/upload panel | `/admin/pdfs/*` | `main.py`, `ingest.py` | Filesystem + PostgreSQL |

---

## 24. Conclusion

The Mindgate Policy Bot combines a lightweight browser interface, an asynchronous FastAPI backend, Google Gemini models, and PostgreSQL with pgvector to create a complete internal RAG system.

Its main architectural strengths are:

- One database for users, conversations, analytics, text, and vectors.
- Grounded answer generation using retrieved policy content.
- Persistent, user-owned chat history.
- Responsive SSE streaming with polished browser rendering.
- Automatic and admin-controlled PDF ingestion.
- Multiple prompt-injection and output-leak defenses.
- A straightforward Docker-based local setup.

The most important next production steps are to secure admin provisioning, remove the default JWT secret, harden uploads and browser token handling, introduce formal migrations, and expand automated testing and observability.
