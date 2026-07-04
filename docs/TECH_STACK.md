# Mindgate Policy Bot — Tech Stack and Features

## Technology Stack

| Technology | Purpose | Why It Was Chosen |
|---|---|---|
| **Python** | Main backend language | Strong AI, PDF-processing, and database ecosystem with readable, maintainable code. |
| **FastAPI + Uvicorn** | REST API and web server | Provides fast asynchronous endpoints, automatic validation, and simple API development. |
| **HTML, CSS, and Vanilla JavaScript** | Chat and admin interfaces | Keeps the frontend lightweight with no framework build process or extra runtime dependencies. |
| **Google Gemini 2.5 Flash** | Answer generation and optional PDF vision analysis | Offers fast natural-language generation and can understand visual content such as tables and diagrams. |
| **Gemini Embedding (`gemini-embedding-001`)** | Converts document chunks and questions into vectors | Enables semantic search based on meaning rather than exact keyword matches. |
| **PostgreSQL** | Stores users, chats, document chunks, and metadata | A reliable relational database suitable for persistent application data. |
| **pgvector** | Vector similarity search inside PostgreSQL | Keeps relational data and embeddings in one database while supporting efficient cosine-similarity retrieval. |
| **SQLAlchemy** | Database models and queries | Provides structured ORM models, relationships, connection pooling, and safer database access. |
| **PyMuPDF** | PDF text extraction and page rendering | Fast, dependable PDF processing with support for both text and page images. |
| **JWT + bcrypt** | Authentication and password security | JWT supports stateless protected API requests, while bcrypt securely hashes passwords. |
| **Pydantic** | Request and response validation | Ensures API data follows the expected schema and produces useful validation errors. |
| **Watchdog** | Monitors the `data/` directory | Automatically starts ingestion when PDFs are added or updated. |
| **Docker Compose** | Runs PostgreSQL with pgvector | Creates a consistent local database environment with minimal setup. |
| **Server-Sent Events (SSE)** | Streams chatbot responses | Displays generated text progressively without requiring WebSockets. |

## Basic Feature Functioning

### PDF Ingestion

PDF files are read with PyMuPDF, split into overlapping text chunks, converted into Gemini embeddings, and stored in PostgreSQL with their source metadata. Optional vision processing can describe tables, charts, and diagrams before indexing.

### RAG Question Answering

The user's question is embedded and compared with stored document vectors through pgvector. The most relevant chunks are inserted into a grounded prompt, and Gemini generates an answer using that retrieved policy context.

### Authentication and Roles

Administrators sign in with an email and password. Passwords are stored as bcrypt hashes, and successful authentication returns a JWT. Protected endpoints validate this token, while admin endpoints additionally verify the user's admin role. Legacy employee registration/login routes are not part of the active admin-only UI.

### Chat Streaming and Memory

Answers are sent token-by-token through SSE for a responsive chat experience. Sessions and messages are saved in PostgreSQL, allowing users to reopen, switch between, or delete previous conversations. Recent messages are included as context for follow-up questions.

### Guardrails

Questions are checked for prompt injection, role-play abuse, unrelated requests, and attempts to expose system instructions. Inputs are sanitized before retrieval, and generated answers are checked again before being returned.

### Automatic Re-ingestion

Watchdog monitors the PDF directory. When a PDF changes, it is queued for ingestion so the searchable knowledge base stays synchronized with the latest documents.

### A2A and NPCI Inbox

The A2A surface is split by trust level. `/a2a/external` is for NPCI callbacks only and accepts `NPCI_API_KEY`; `/a2a/internal` is for the admin frontend only and accepts an admin JWT. NPCI document ingestion stores the original file in `data/a2a_received/`, calculates a SHA-256 content hash for duplicate detection, creates `received_documents` and `a2a_ingestion_jobs` rows, returns a `job_id`, and processes extraction, chunking, embeddings, summaries, and approved email notifications asynchronously.

The admin Inbox has two tabs: Received Documents with lifecycle states and retry for failed jobs, and NPCI Tickets with pending/resolved/pending_review/failed statuses. NPCI replies can auto-ingest for demos or wait for review when `NPCI_REPLY_AUTO_INGEST=false`.

### Admin Console

Administrators can upload, inspect, and delete PDFs, chat with the RAG knowledge base, review received A2A documents, retry failed ingestion jobs, and track NPCI tickets. The older analytics dashboard route is retained only as a deprecated compatibility surface and is not part of the active frontend navigation.

### Frontend

The browser interface handles admin login, chat history, streamed responses, session management, document management, Inbox review, and sign-out.
