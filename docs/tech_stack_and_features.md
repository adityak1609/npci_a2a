# Mindgate Policy Bot — Technology Stack & Features (In-Depth Reference)

This document provides a highly detailed, component-level breakdown of the technology stack, design rationales, database schemas, and architectural logic implemented in the **Mindgate Policy Bot** (RAG-based policy chatbot).

---

## 🗺️ Architectural Overview

The application is structured around a decoupled model where **FastAPI** drives the API backend, **PostgreSQL** serves as a unified relational and vector database, and the **Google Gemini SDK** powers semantic reasoning and text embeddings.

```
                  ┌──────────────────────────────────────────────┐
                  │            Client Browser (UI)               │
                  └──────────────────────┬───────────────────────┘
                                         │ HTTPS / SSE
                                         ▼
                  ┌──────────────────────────────────────────────┐
                  │                 FastAPI App                  │
                  └──────┬───────────────┬────────────────┬──────┘
                         │               │                │
            Reads/Writes │               │                │ Streams
                         ▼               │                ▼
      ┌──────────────────────┐           │    ┌──────────────────────┐
      │     PostgreSQL       │           │    │     Google Gemini    │
      ├──────────────────────┤           │    ├──────────────────────┤
      │ • users (relational) │           │    │ • gemini-2.5-flash   │
      │ • sessions/messages  │           │    │   (Chat & RAG)       │
      │ • document_chunks    │           │    │ • gemini-2.0-flash   │
      │   (Vector / pgvector)│           │    │   (Guard Classifier) │
      └──────────────────────┘           │    │ • gemini-embedding-  │
                                         │    │   001                │
                                         │    └──────────────────────┘
                                         │
                                         ▼
                               ┌──────────────────┐
                               │   Watcher Loop   │
                               ├──────────────────┤
                               │ • data/*.pdf     │
                               │ • Ingest Queue   │
                               └──────────────────┘
```

---

## 🛠️ Technology Stack Deep Dive & Rationales

### 1. Backend Web Framework: **FastAPI (Python)**
*   **Why it was chosen:**
    *   **Asynchronous Native Support:** FastAPI is built on top of Starlette and Uvicorn, offering native asynchronous capabilities. This is critical for streaming chatbot responses using **Server-Sent Events (SSE)** without tying up server threads.
    *   **Pydantic Integration:** Request/Response payloads are validated at runtime via Pydantic schemas. This enforces strict contracts for user data, query structures, and auth schemas.
    *   **Speed:** Benchmarks place FastAPI among the fastest Python web frameworks (on par with Go or Node.js) due to its ASGI model.
    *   **Auto-Generated Documentation:** Standardizes development with automatically generated Swagger UI (`/docs`) and ReDoc (`/redoc`) portals based on route definitions.

### 2. Database & Vector Store: **PostgreSQL + `pgvector`**
*   **Why it was chosen:**
    *   **Unified Architectural Footprint:** Traditional RAG stacks require two separate databases: a relational database (e.g., PostgreSQL or MySQL) for storing users, session configurations, and message history, and a specialized vector database (e.g., Pinecone, Milvus, Qdrant) for embeddings. Using PostgreSQL with the `pgvector` extension allows both workloads to reside in a single, standard instance.
    *   **Metadata Integration & Joining:** Joining relational data (such as querying only chunks derived from a specific document) is done natively via SQL `JOIN` clauses.
    *   **Local Portability:** Easily configured to run in Docker container environments (via `pgvector/pgvector:pg16` image), making local deployment and updates highly predictable.
    *   **Cosine Similarity Operators:** Exposes the `<=>` operator representing Cosine distance, executing vector lookups natively in database memory.

### 3. LLM & Embeddings: **Google Gemini SDK (`google-genai`)**
*   **Why it was chosen:**
    *   **`gemini-2.5-flash`:** Used for core generation. It is extremely fast, possesses a massive context window, and is highly cost-effective while delivering advanced reasoning capabilities for grounding tasks.
    *   **`gemini-2.0-flash` (Classifier Isolation):** Used as a semantic text classifier for input guardrails. Separating this request onto another model prevents rate-limit depletion on the primary conversational model.
    *   **`gemini-embedding-001`:** Computes 768-dimensional dense vectors representing the semantic characteristics of policy passages and user queries.

### 4. Document Processing: **PyMuPDF (`fitz`)**
*   **Why it was chosen:**
    *   **High Performance:** Written in C, PyMuPDF performs PDF parsing tasks exponentially faster than pure Python alternatives like PyPDF2, pdfplumber, or PDFMiner.
    *   **Layout Preservation:** Properly maintains blocks of text, facilitating uniform chunk splitting without losing page structure or semantic flow.

### 5. File System Event Monitoring: **Watchdog**
*   **Why it was chosen:**
    *   Monitors filesystem directories on a separate thread, consuming zero CPU cycles while idling (unlike active folder loops).

### 6. Relational ORM: **SQLAlchemy & Alembic**
*   **Why it was chosen:**
    *   **SQLAlchemy:** Establishes type-safe Python definitions for database schemas, managing database sessions, transactions, and relation cascades cleanly.
    *   **Alembic:** Manages database schema migrations over the lifecycle of the application.

---

## 🗄️ Relational Database Schemas

All schemas are initialized via SQLAlchemy (defined in [database.py](file:///d:/RAG_PDF_CHATBOT/database.py)) using PostgreSQL dialects.

```
       ┌──────────────┐                 ┌──────────────────┐
       │    users     │                 │  chat_sessions   │
       ├──────────────┤                 ├──────────────────┤
       │ id (PK)      │ 1             * │ id (PK - UUID)   │
       │ email        ├─────────────────┤ user_id (FK)     │
       │ hashed_pwd   │                 │ created_at       │
       │ full_name    │                 └────────┬─────────┘
       │ is_admin     │                          │ 1
       │ created_at   │                          │
       └──────────────┘                          │ *
                                        ┌────────▼─────────┐
                                        │  chat_messages   │
                                        ├──────────────────┤
                                        │ id (PK)          │
                                        │ session_id (FK)  │
                                        │ role (user/model)│
                                        │ content          │
                                        │ created_at       │
                                        └──────────────────┘
```

### Table Definitions

#### 1. `users` Table
Stores authentication details and permission levels.
*   `id` (Integer, Primary Key): Autoincrementing index.
*   `email` (String(255), Unique, Indexed): User email used as identity username.
*   `hashed_password` (String(255)): Salted, bcrypt-hashed password string.
*   `full_name` (String(255), Nullable): Friendly user display name.
*   `is_admin` (Boolean, Default: False): Escalation flag granting access to administrative endpoints (`/admin/*`).
*   `created_at` (DateTime): Timestamp of creation (stored in UTC).

#### 2. `chat_sessions` Table
Organizes message flows.
*   `id` (String(36), Primary Key): UUID generated by clients.
*   `user_id` (Integer, Foreign Key references `users.id` with `ondelete="CASCADE"`): Identifies the session owner.
*   `created_at` (DateTime): Time of creation.
*   *Indexes:* Composite index `ix_chat_sessions_user_created` over `(user_id, created_at)` speeds up history listing queries.

#### 3. `chat_messages` Table
Maintains message history within sessions.
*   `id` (Integer, Primary Key): Message ID.
*   `session_id` (String(36), Foreign Key references `chat_sessions.id` with `ondelete="CASCADE"`): Links message to parent session.
*   `role` (String(20)): Identifies speaker context (`user` or `assistant`).
*   `content` (Text): The actual text content.
*   `created_at` (DateTime): Time the message was sent.
*   *Indexes:* `ix_chat_messages_session_created` on `(session_id, created_at)` enables rapid historical sequencing.

#### 4. `document_chunks` Table
Holds processed text fragments and their high-dimensional vector embeddings.
*   `id` (Integer, Primary Key): Vector block ID.
*   `source_file` (String(512), Indexed): Base filename of source PDF (e.g. `policy.pdf`).
*   `chunk_index` (Integer): Incremental order index of the chunk in the document.
*   `text` (Text): Plaintext content.
*   `embedding` (Vector(3072)): Dense floating-point array (pgvector type) generated by the Gemini embedding model.
*   *Constraints:* UniqueConstraint on `(source_file, chunk_index)` prevents duplicate chunk processing.

---

## ⚙️ Core System Features & Functioning

### 1. User Authentication & JWT Protection
*   **Security Configuration:** Implemented in [security/auth.py](file:///d:/RAG_PDF_CHATBOT/auth.py).
*   **Hashing (Bcrypt):** Plaintext passwords are raw-hashed using `bcrypt.gensalt()` before storage. Plaintext passwords never enter database records.
*   **Verification:** `bcrypt.checkpw()` verifies incoming credentials by matching candidate strings against the stored hash.
*   **Token Authorization:** On successful authentication, `/auth/login` returns a JSON Web Token (JWT) signed with the server's `SECRET_KEY` using the `HS256` algorithm.
*   **FastAPI Dependency Injection:**
    *   Protected routes inject `get_current_user` or `get_current_admin`.
    *   The backend validates the token from the request's HTTP Bearer Header, decoding the payload's `sub` claim (email) and retrieving user details from the database.

### 2. Auto-Ingestion Queue (`watcher.py`)
Provides autonomous document updates on file drops.
*   **Watchdog Handler:** Monitors `data/` for PDF creation or write completion events.
*   **Debounce Safety:** When an OS modifies a file, it does so in fragments. Watcher instantiates a `threading.Timer` with a **5-second debounce**. If another file write occurs, the timer resets, preventing half-written ingestion.
*   **Sequential Queue:** Once debounced, files are sent to a thread-safe `queue.Queue` processed by a single background daemon thread. This avoids parallel API floods and rate-limiting blocks.

### 3. PDF Ingestion Pipeline (`rag/ingest.py`)
Splits documents into mathematical representations.
*   **Extraction:** Opens a PDF via PyMuPDF.
*   **Semantic Chunking:** Loops through lines, accumulating chunks up to 1000 characters. To prevent context division, a 200-character overlap buffer is maintained between consecutive chunks.
*   **Injection Sanitization:** Scans chunk text for prompt injection signatures (e.g. `SYSTEM OVERRIDE: Ignore instructions`). Matches are wrapped in `[DOCUMENT TEXT: ...]` flags to isolate them as data rather than instructions.
*   **Vector Construction:** The FastAPI app contacts the Gemini embedding service, generating the vector weights.
*   **Transaction Lock:** Truncates older records matching `source_file` and executes bulk SQL inserts.

### 4. Vector Cosine Retriever (`rag/retriever.py`)
Implements the core search capabilities.
*   **Vector Conversion:** Embeds incoming questions using the same model (`gemini-embedding-001`).
*   **Similarity Search:** Performs a cosine distance calculation. PostgreSQL compares the search vector with all table elements, using the `<=>` operator (where a lower distance indicates high similarity). The top 5 closest fragments are returned.

### 5. Multi-Tier Security Guardrails (`security/guardrails.py`)
To prevent exploitation, user inputs and outputs are processed through three defensive boundaries:

*   **Tier 1: Regex Fast-Path (Input Check)**
    *   Scans the input question against a list of pre-compiled regular expressions.
    *   Blocks common exploits: instruction overrides ("ignore system prompts"), system extraction ("show prompt templates"), roleplay simulation ("act as manager"), or memory poisoning ("remember this").
    *   **Cost: 0 API units.** Immediately stops obvious attacks.
*   **Tier 2: Semantic LLM Classifier (Input Check)**
    *   If Tier 1 passes, the question is evaluated by `gemini-2.0-flash` with a system instruction specifying safe vs unsafe categories.
    *   Catches creative paraphrasing and semantic bypasses.
    *   If blocked, it stops the request and returns a safe refusal message.
*   **Tier 3: Output Leak Guard (Output Check)**
    *   Scans generated responses before sending them to users.
    *   If system prompt components or raw chunks are present in the response, it replaces the output with a generic refusal message.

### 6. Grounded Answer Streaming (`main.py` / `/chat/stream`)
Optimizes responsiveness for the end user.
*   **Parallel Acceleration:** Run guardrail checks (`check_input`) and database retrieval (`retrieve`) concurrently via `asyncio.gather`, saving ~1-2 seconds of server overhead.
*   **SSE Token Stream:** Rather than waiting for the entire LLM response, Uvicorn pipes events chunk-by-chunk using `StreamingResponse` with `media_type="text/event-stream"`.
*   **Async Thread Queueing:** An `asyncio.Queue` passes tokens from a background thread (running the blocking SDK generator) to the async generator loop, preventing event-loop blocks.

### 7. Administrative Panel (`main.py`)
Gives admins granular control over the data engine.
*   **Endpoints:**
    *   `GET /admin/pdfs`: Lists files, chunk counts, sizes, and timestamps.
    *   `POST /admin/upload`: Handles PDF upload, saves files, triggers ingestion, and suppresses the standard watcher to avoid double processing.
    *   `DELETE /admin/pdfs/{filename}`: Clears database chunks and deletes the file from disk.
    *   `GET /admin/dashboard`: Generates usage metrics, query history charts (last 14 days), top active users, and recent query logs.

## Current Admin/A2A Note

The active app is admin-only. Use `POST /auth/admin/login` for the frontend; `/auth/login`, `/feedback/{message_id}`, and `/admin/dashboard` are legacy compatibility surfaces and are not part of the active admin UI.

A2A is split by trust level:

- `POST /a2a/external`: NPCI API key only; allowed methods are `agent.ingest_document` and `agent.receive_npci_reply`.
- `POST /a2a/internal`: admin JWT only; allowed methods are `agent.get_summary`, `agent.query_rag`, and `agent.escalate_to_npci`.

Inbound A2A documents are stored in `data/a2a_received/`, deduplicated by SHA-256 content hash, tracked in `received_documents` and `a2a_ingestion_jobs`, and processed asynchronously. The Inbox exposes Received Documents and NPCI Tickets, failed ingestion retry, and pending-review support when `NPCI_REPLY_AUTO_INGEST=false`.
