# Mindgate Policy Bot — Technical Documentation

## 1. Overview & System Goal
The **Mindgate Policy Bot** is an AI-powered internal Retrieval-Augmented Generation (RAG) system designed to answer employee questions about company policies in plain English. Answers are securely grounded in official policy PDF documents, eliminating the need to manually read long manuals.

The system incorporates a **Role Selection** gateway, segregating features and access limits between general **Employees** (using the chatbot interface) and **Administrators** (managing documents, analytics, speech dictation, and system audit logs).

---

## 2. System Architecture
The application runs as a FastAPI backend powered by PostgreSQL with the `pgvector` extension and Google's Gemini models (`gemini-2.5-flash` for generation, `gemini-embedding-001` for embedding).

```
                        ┌───────────────────────────────────────┐
                        │       Role Selection Entrypoint       │
                        └───────────────────────────────────────┘
                                   /                 \
                                  /                   \
                                 ▼                     ▼
                        ┌─────────────────┐   ┌─────────────────┐
                        │  Employee Portal│   │  Admin Portal   │
                        │  (Chatbot RAG)  │   │  (Control Room) │
                        └─────────────────┘   └─────────────────┘
                                 │                     │
                                 ▼                     ▼
                        ┌───────────────────────────────────────┐
                        │            FastAPI Backend            │
                        └───────────────────────────────────────┘
                                   |                 |
                                   ▼                 ▼
                        ┌─────────────────┐   ┌─────────────────┐
                        │   Gemini AI     │   │  Postgres DB    │
                        │  (LLM & Embed)  │   │   (pgvector)    │
                        └─────────────────┘   └─────────────────┘
```

### 2.1 Database Models
The system schemas are built using SQLAlchemy in [database.py](file:///d:/RAG_PDF_CHATBOT/database.py):

*   **`users`**: User registration records. Standard users default to `is_admin = False`. Admin permissions are secured via `is_admin = True`. Passwords are encrypted with bcrypt.
*   **`chat_sessions`**: Session groups mapping back-and-forth conversations to individual users. Tied client-side via a generated UUID.
*   **`chat_messages`**: Message history within a session. Identifies the sender using the `role` column (`user` or `assistant`).
*   **`message_feedback`**: Feedback rating (`positive` or `negative`), selected category reason (e.g. `incorrect`, `incomplete`), and optional comment text linked to a message.
*   **`document_chunks`**: Segmented text chunks extracted from ingested PDFs, associated with their 3072-dimension vectors (`gemini-embedding-001`).
*   **`voice_notes`**: Dictated rules saved directly by admins as knowledge chunks.
*   **`audit_logs`**: Permanent records tracking admin events (e.g. file uploads, document deletions, voice note additions).

---

### 2.2 Phase 1: Ingestion Pipeline
Ingestion happens dynamically through the Admin Portal upload interface, the watcher background process, or manual import.

```
PDF Ingestion:
PDF/Voice Note  →  Extract Text  →  Overlap Chunking  →  Embedding Vectors  →  Save to PostgreSQL
```

1.  **Text Extraction**: Parses target PDF files page-by-page using PyMuPDF.
2.  **Chunking**: Splits parsed text into overlapping segments of ~800 tokens to preserve contextual continuity.
3.  **Vectorization**: Embeds text chunks via `gemini-embedding-001` into a 3072-value array.
4.  **Database Storage**: Commits the text and corresponding vector to `document_chunks` using `pgvector`.
5.  **Auto-Ingestion Watcher**: [watcher.py](file:///d:/RAG_PDF_CHATBOT/watcher.py) runs as a background worker thread. It polls an ingestion queue to process drops in the `data/` directory sequentially, preventing concurrency collisions or rate limits.

---

### 2.3 Phase 2: Chat Retrieval & Response (RAG)
When an employee asks a question, the backend completes retrieval in 2–3 seconds:

```
RAG Chat Flow:
User Question  →  Generate Embedding  →  pgvector Cosine Search  →  Assemble Prompt  →  Gemini LLM  →  SSE Stream Response
```

1.  **JWT Verification**: Checks authorization and fetches message session states.
2.  **Query Vectorization**: Converts the user's question into a 3072-dimensional vector.
3.  **Semantic Similarity Search**: Queries `document_chunks` using Cosine Distance (`<=>` operator) to fetch the top 5 most relevant policy text segments.
4.  **Prompt Assembly**: Feeds retrieved text blocks into a strict prompt template defined in [prompt.py](file:///d:/RAG_PDF_CHATBOT/prompt.py). The prompt instructs the LLM:
    *   Answer **ONLY** from the provided text segments.
    *   If the source text is insufficient, explicitly state that the answer is not available.
    *   Do **NOT** guess, infer, or hallucinate answers.
    *   Include citations (e.g. `[Page 4]`).
5.  **Streaming Generation**: Google Gemini LLM processes the prompt and streams the final answer token-by-token using **Server-Sent Events (SSE)**.

---

## 3. Roles & Authentication Flow

### 3.1 Role Selection Gateway
When loading the app without an active token, users encounter a role selection page. They select their role: **Employee** or **Administrator**.

*   **Employee**: Reveals **User Login** and **Register** tabs on the login card.
*   **Administrator**: Displays **only** the **Admin Login** form under a clean *Administrator Sign In* heading (the register option is hidden for administrators to prevent unauthorized access creation).

Users can return to the role selection page at any time by clicking the "Back to role selection" link at the top of the auth card.

### 3.2 JWT Authentication
Authentication is enforced using JSON Web Tokens (JWT) signed with a secret key in [auth.py](file:///d:/RAG_PDF_CHATBOT/auth.py). The token contains the email, name, and admin permission status of the user. Secure cookie headers or authorization headers must be supplied for API validation.

---

## 4. Portals & Interfaces

### 4.1 Employee Portal (Chat Interface)
*   **Sidebar Navigation**: Dynamic session list displaying past chats grouped under chronological categories: *Today*, *Yesterday*, *This Week*, and *Older*.
*   **Active Memory**: Incorporates chat memory tracking the last 5 turns to retain topic context.
*   **Suggestion Chips**: Shuffled context-aware chips giving quick follow-up prompt ideas.
*   **Grounded Citations**: Answers include page citations that link to source sections in policy documents.
*   **Interactive Feedback**: Message bubbles feature thumbs up/down icons. Clicking "Not helpful" reveals reason selections (Incorrect, Incomplete, Outdated, Unclear, etc.) and comment boxes.

### 4.2 Administrator Portal (Dashboard Control Room)
*   **Overview Analytics**: Real-time stats showing active users, queries, helpful ratings, and feedback logs.
*   **Daily volume chart**: Graphs showing daily queries over the past 14 days.
*   **Document Management**: Tab listing ingested PDF documents with size and chunk parameters. Allows administrators to permanently purge documents and their associated vector embeddings from the index.
*   **PDF Upload zone**: Drag-and-drop zone to drop and ingest new PDFs directly.
*   **Voice Notes Dictation**: Integrated HTML5 SpeechRecognition tool. Dictated speeches are converted to text and ingested directly into the database as policy knowledge pieces.
*   **Audit logs**: Interactive panel tracking all uploads, voice saves, and deleted items.

---

## 5. Technology Stack Summary

| Layer | Component | Description / Benefits |
|---|---|---|
| **AI LLM** | Google Gemini 2.5 Flash | High-speed response, cost-efficient token margins, SSE streaming support. |
| **Embeddings** | Google `gemini-embedding-001` | Generates semantic 3072-dimensional vectors. |
| **Database** | PostgreSQL + pgvector | SQL data relations with lightning fast cosine similarity index lookups. |
| **PDF Extraction** | PyMuPDF | Lightweight, fast document parser. |
| **Backend API** | FastAPI (Python) | High-performance asynchronous requests with automated interactive swagger docs. |
| **Frontend** | Pure HTML/CSS/Javascript | Responsive layout, low asset package weight, pure CSS transition states. |

---

## 6. API Reference

All chat, feedback, and document operations require a valid JWT token.

### 6.1 Authentication API
*   `POST /auth/register` — Registers a standard user account.
*   `POST /auth/user/login` — Authenticates an Employee account.
*   `POST /auth/admin/login` — Authenticates an Administrator account.

### 6.2 Chat API
*   `POST /chat` — Sync JSON chatbot query.
*   `POST /chat/stream` — SSE endpoint to stream responses.
*   `GET /chat/sessions` — Retrieves session list for logged-in user.
*   `GET /chat/history/{session_id}` — Gets full chat history for a session.
*   `DELETE /chat/sessions/{session_id}` — Deletes a session.
*   `POST /feedback/{message_id}` — Submits thumbs up/down ratings, reason keys, and comments.

### 6.3 Admin Panel API
*   `GET /admin/dashboard` — Statistics, audit lists, charts, and feedback reviews.
*   `GET /admin/pdfs` — Lists all ingested PDFs.
*   `POST /admin/upload` — Uploads and chunks new PDF.
*   `DELETE /admin/pdfs/{filename}` — Deletes a PDF and its chunks.
*   `GET /admin/voice-notes` — Lists custom voice notes.
*   `POST /admin/voice-notes` — Saves new voice dictation.
*   `DELETE /admin/voice-notes/{note_id}` — Purges custom voice note.
*   `GET /admin/audit-log` — Fetches admin activity logs.

---

## 7. Vector Index Optimization
To support fast retrieval queries on large documents, the PostgreSQL database initializes an **IVFFlat Index** on `document_chunks.embedding` using cosine distance operations:
```sql
CREATE INDEX IF NOT EXISTS ix_document_chunks_embedding
ON document_chunks
USING ivfflat (embedding vector_cosine_ops)
WITH (lists = 50);
```
*Note: The system trains this index automatically once the database grows past 100 entries, optimizing search speed dynamically.*
