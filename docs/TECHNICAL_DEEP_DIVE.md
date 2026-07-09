# NPCI A2A Partner Platform — Technical Deep Dive

*The bank's side of the NPCI Agent-to-Agent (A2A) change-management workflow.*

This document explains, in plain terms, **what the platform is built from, why each technology was chosen, how the security works, and how the A2A protocol works.**

It also clears up the most common confusion up front: **is "A2A" just the Google library?** Short answer: **no.** The *delivery wire* is Google's A2A (ADK), but the protocol that rides on it — the message shape, the state machine, the 8 security layers, the routing — is all **hand-written in this repo**. Google's ADK carries our protocol; it isn't the protocol. See [§5](#5-how-a2a-actually-works).

It's a companion to `docs/ARCHITECTURE.md` and `docs/TECHNICAL_DOCUMENTATION.md`. Those explain *what each file does*; this one explains *why the design is the way it is*.

---

## Table of contents

1. [System at a glance](#1-system-at-a-glance)
2. [The technology stack — and why](#2-the-technology-stack--and-why)
3. [Request flows](#3-request-flows)
4. [The 8-layer authentication stack](#4-the-8-layer-authentication-stack)
5. [How A2A actually works](#5-how-a2a-actually-works)
6. [The RAG assistant and guardrails](#6-the-rag-assistant-and-guardrails)
7. [Human authentication (browser sessions, JWT, MFA)](#7-human-authentication)
8. [Asynchronous ingestion and the worker](#8-asynchronous-ingestion-and-the-worker)
9. [Data layer](#9-data-layer)
10. [Operational hardening summary](#10-operational-hardening-summary)

---

## 1. System at a glance

This platform is **the bank's end** of a two-party conversation with NPCI. It's software talking to software.

Here's the story it supports: NPCI sends the bank a *change communication* — a regulatory or product change, along with a kit of documents. The bank's software receives it and walks it through a lifecycle: acknowledge it, ask questions, propose changes, flag blockers, report progress. On top of that sits a **web portal** where bank product managers work through each change, helped by an **AI assistant that answers questions using that change's own documents**.

Two very different kinds of caller use the system, so there are **two separate ways to log in**:

| Caller | What they use | How they're checked |
|--------|---------------|---------------------|
| **NPCI's software** (a machine) | the Google-A2A server (`a2a_adk_server`, port 8002) | The **8-layer security stack** — [§4](#4-the-8-layer-authentication-stack) |
| **Bank staff** (people in a browser) | `POST /a2a/internal`, plus normal web endpoints | Login session (password + cookie), with a phone code (MFA) required for admin accounts — [§7](#7-human-authentication) |

> **Scope note:** this repo builds **only the bank's side**. NPCI's own platform and its certification test system are out of scope. A **mock (fake) NPCI agent** is included so the whole thing can be tested locally. Certification message types exist in the code but just reply "not implemented."

---

## 2. The technology stack — and why

### 2.1 Backend runtime

| Technology | Version | Why it was chosen |
|------------|---------|-------------------|
| **Python 3.10+** | — | The work here is mostly *waiting on the network* (calls to NPCI, the database, Gemini) rather than heavy number-crunching, and Python's `async` model is a great fit for that. It also has ready-made libraries for everything else: Gemini, pgvector, PDF reading, cryptography. |
| **FastAPI** | 0.138 | Picked over Flask/Django because: (1) it's **async** — one request can talk to Gemini *and* the database at the same time; (2) it **validates every incoming message** against a strict shape, so malformed traffic is rejected at the door; (3) its **dependency injection** makes the auth checks and database access easy to plug in and test. |
| **Uvicorn + Gunicorn** | 0.49 / 23.0 | Uvicorn is the server that runs the async app. In production, Gunicorn runs several copies of it so the app can use all CPU cores and restart cleanly. This is the standard way to run FastAPI in production. |
| **Pydantic** | 2.13 | The library that defines and checks the shape of every message. It's fast (Rust core), and it means an invalid message type or field simply can't reach the code that would act on it. |

**Why async matters here:** processing incoming documents (read PDF → split → embed → summarize) and live chat share the same engine in development. Slow, blocking steps are pushed onto a pool of background threads so they don't freeze everything else, and that pool is sized to match the database connection limit so threads never pile up waiting.

#### 2.1a A2A transport libraries

| Technology | Version | Why |
|------------|---------|-----|
| **`google-adk[a2a]`** | 2.3.0 | Google's **Agent Development Kit** — the delivery wire for A2A ([§5.6](#56-the-google-a2a-adk-transport)). It turns the bank's agent into an A2A server and auto-publishes a description of it. Used because the **real NPCI agent speaks Google A2A**. It also pulls in **`a2a-sdk`** (the actual A2A client and server). |
| **`a2a-sdk`** (via the ADK) | — | The pieces used to build and send A2A messages in `agent/adk/`. Chosen over ADK's `RemoteA2aAgent` (which is meant for AI sub-agents) because we need plain, predictable, file-carrying sends. |

> This has **nothing to do with Gemini.** `google-genai` (§2.2) is the AI SDK; `google-adk`/`a2a-sdk` are the delivery method for messages. They're both from Google, but that's the *only* thing they have in common.

### 2.2 AI / RAG layer

| Technology | Why |
|------------|-----|
| **Google Gemini 2.5 Flash** (`google-genai` SDK 2.10) | The assistant only reasons over documents it's given — it's not a general chatbot. **Flash** is the cheap, fast tier, which is right for high-volume Q&A and for the safety filter that runs on *every* message. One SDK covers embeddings, summaries, chat, and the safety filter. |
| **`gemini-embedding-001`** | Turns text into number-vectors so we can search documents by meaning. Using the same provider for embeddings and answers keeps everything consistent. |
| **PyMuPDF (`fitz`)** | 1.27 | Fast, lightweight PDF text extraction for incoming documents. Scanned images fall back to a Gemini vision path. |

> ⚠️ **Important:** every `from google import genai` in the code is the **Gemini AI SDK** — it has **nothing to do with A2A messaging**. Don't mix it up with `google-adk` / `a2a-sdk` (§2.1a), which are the A2A wire. The core of A2A (the message shape, the state machine, the routing, the security) is all hand-written here; Google's ADK just carries it. See [§5.1](#51-one-protocol-delivered-over-google-a2a-adk).

### 2.3 Data + persistence

| Technology | Why |
|------------|-----|
| **PostgreSQL** | The one source of truth for changes, tickets, audit logs, users, and the job queue. We use Postgres as the queue too (instead of a separate tool) because it can safely hand out jobs to workers with `FOR UPDATE SKIP LOCKED` — one fewer thing to run. |
| **pgvector** | 0.4 | Stores the meaning-vectors *inside* Postgres, so semantic search is just a SQL query. No separate vector database to run — business data, search, and audit all live in one place. |
| **SQLAlchemy 2.0 + Alembic** | The database models plus versioned schema migrations. In production the app **refuses to start unless the database schema is exactly the version it expects** — so mismatches fail loudly instead of causing weird bugs later. |
| **Redis** | 4.6 | Production only. Used for rate limiting and coordinating the multiple app copies, where per-copy counting would be wrong. Required at startup in production. |

### 2.4 Security & auth libraries

| Technology | Why |
|------------|-----|
| **`python-jose[cryptography]`** | Creates and checks login tokens (JWT). Uses **RS256** in production, where the key that *checks* a token can't be used to *create* one — so a leaked verify-key can't mint fake logins. |
| **`bcrypt`** | Password scrambling. It has a known quirk (it ignores anything past 72 characters), so passwords are pre-scrambled with SHA-256 first, and the method is versioned so old passwords still work. |
| **`cryptography`** | Reads client certificates for the mTLS check (Layer 1). |
| **`pyotp`** | The 6-digit phone-app codes (TOTP) for admin MFA, with a small time window for clock differences. |
| **`hmac` / `hashlib`** (built in) | The A2A message signature (Layer 3), compared in constant time so attackers can't learn the secret by timing the response. No extra library needed. |

### 2.5 Frontend

| Technology | Why |
|------------|-----|
| **React 18 + TypeScript** | The portal is an interactive single-page app. TypeScript matches the strictly-typed backend, so message shapes are checked while writing the code. |
| **Vite 5** | Fast dev server and an optimized production build with uniquely-named files — needed because the backend serves it under a strict security policy. |
| **Tailwind CSS** | Styling written directly in the markup; no extra runtime cost. |
| **react-router-dom 6** | Handles page navigation inside the app so bookmarks and deep links work. |

In production, the frontend is built inside the Docker image, then the backend serves it and stamps a fresh security token onto every script/style tag on each response.

### 2.6 Observability & ops

| Technology | Why |
|------------|-----|
| **OpenTelemetry** | Traces a single request across the app, outbound NPCI calls, and the database — so one A2A message can be followed end to end. |
| **prometheus-client** | Metrics (auth rejections by reason, request timings, etc.) that work correctly across the multiple app copies. |
| **Docker / docker-compose** | Reproducible local stack and a clean multi-stage production image. |
| **httpx** | The HTTP client used underneath the A2A wire for **outbound** Bank→NPCI calls, with timeouts, retries, and client-certificate support. |

---

## 3. Request flows

### 3.1 Inbound A2A (NPCI → Bank)

```
NPCI agent
  │  Google-A2A message/send → a2a_adk_server (:8002)   (signature/token headers; mTLS at the proxy)
  ▼
[ transport middleware ]   ── read raw body (size-capped) → security Layers 1–4
  ▼
[ bank agent ]             ── rebuild the message → security Layers 5–8
  ▼
[ dispatch ]               ── claim as unique → run the handler → advance state → log it
  ▼
typed JSON result   +   (usually) an automatic acknowledgement back to NPCI
```

### 3.2 Portal (browser → Bank)

```
React app ──login cookie──▶ POST /a2a/internal
   │                            │
   │  admin vs portal user?     ▼
   │                     only the methods that role is allowed to call
   ▼                            ▼
 web pages / live chat / websocket   e.g. query_rag, escalate_to_npci, …
```

The machine endpoint and the browser endpoint speak **different languages on purpose**: the inbound A2A server routes on the spec's `task_type` (machine traffic), while `/a2a/internal` routes on internal method names like `agent.query_rag` (browser traffic). The old shared `/a2a` endpoint now returns **410 Gone** to push callers to the correct one.

---

## 4. The 8-layer authentication stack

This is the security core of the inbound A2A path (`a2a_adk_server`). It exists because the caller is **another machine on the public internet** — there's no browser login to lean on. Each layer is a **separate, independent check**. They run **in order and stop at the first failure**, and every rejection comes with a stable error code (so both NPCI's monitoring and our own metrics can recognize it).

> **Guiding rule — fail closed, fail specific.** Every rejection returns a fixed machine-readable code (`mtls_required`, `replay_detected`, `partner_mismatch`, …) and is written to the audit table. Rejections are also counted by error code.

Each layer below: **what it proves**, **how**, and **why it's its own check**.

### Layer 1 — mTLS (mutual TLS)
- **Proves:** the connection itself was opened by a client holding a certificate we trust — identity at the network level, before we read even one byte of the message.
- **How:** the reverse proxy (Nginx) handles TLS and passes along the client's certificate details. The check rejects any connection that wasn't verified, then confirms the certificate's **fingerprint** is on our allowed list.
- **Why separate:** it's the only check about the *connection* rather than the *message*. It turns away unknown clients before any real work. Required in production; can be turned off in dev so the plain-HTTP mock can connect.
- **Failure codes:** `mtls_required`, `mtls_fingerprint_mismatch`.

### Layer 2 — CIDR allowlist (network check)
- **Proves:** the request came from an approved network range.
- **How:** it takes the caller's IP and checks it against the allowed ranges.
- **Why separate:** cheap and coarse. It catches a stolen certificate being used from the wrong place, and blocks random internet noise before the expensive checks below.
- **Failure code:** `ip_not_allowed`.

### Layer 3 — HMAC signature (message integrity)
- **Proves:** the **message wasn't tampered with**, is **fresh** (not a replay), and came from someone holding the shared secret.
- **How:** three headers are required (timestamp, nonce, signature). The signature is computed over the timestamp, the nonce, and a hash of the exact body. Three checks:
  1. **Timestamp** — rejected if more than **±5 minutes** off, which limits how long a stolen message could be reused.
  2. **Signature** — recomputed with our secret and compared in constant time. Checked *before* touching the database, so a flood of bad signatures can't fill up storage.
  3. **Nonce (one-time value)** — must be unused. It's recorded so it can't be reused; a repeat is treated as a replay and rejected. Old nonces are cleaned up by a background task, not during the request.
- **Why separate:** this is the main proof that the message is genuine and untouched. mTLS secures the pipe; this secures the *contents*. The raw bytes are read *before* parsing so the bytes we check are exactly the bytes that were signed.
- **Failure codes:** `missing_envelope_headers`, `timestamp_skew`, `signature_mismatch`, `replay_detected`.

### Layer 4 — JWT (revocable identity)
- **Proves:** a valid, non-revoked login token is attached, giving us a subject identity.
- **How:** the bearer token is decoded and its expiry checked (same logic as for human logins). If it has an ID, that ID is checked against a revoked list, so a **leaked or logged-out token stops working before it naturally expires**. Required in production; can be relaxed in dev because HMAC is the primary proof.
- **Why separate:** HMAC proves *you hold the shared secret*; JWT gives a *per-caller identity that can be switched off instantly* without changing the shared secret for everyone.
- **Failure codes:** `invalid_token`, `session_revoked`, `session_expired`.

*(At this point the message body is parsed into its strict shape; a malformed message is itself an auth failure.)*

### Layer 5 — Partner status + credential binding
- **Proves:** the claimed partner is **active**, **and** the credentials from Layers 1 and 4 actually **belong to that same partner** — not just valid on their own.
- **How:** confirm the partner is known and active, then confirm the JWT identity maps to that partner *and* the certificate fingerprint maps to that partner.
- **Why separate:** this stops **mix-and-match** attacks — Partner A using Partner B's valid token, or a valid certificate with someone else's message. Each credential being valid isn't enough; they all have to agree on *one* partner.
- **Failure codes:** `partner_inactive`, `partner_mismatch`.

### Layer 6 — Resource ownership
- **Proves:** the specific change this message is about **exists** and **belongs to the caller**.
- **How:** for message types that reference an existing change, look it up. Not found → `unknown_id` (404); belongs to someone else → `partner_mismatch` (403). Message types that *create* something new are skipped (there's nothing to own yet).
- **Why separate:** being authenticated isn't the same as being allowed to touch *this* object. This is the guard against accessing someone else's data by guessing IDs.
- **Failure codes:** `unknown_id`, `partner_mismatch`.

### Layer 7 — State-machine validity
- **Proves:** the message makes sense for **where the change currently is** (e.g. you can't resolve a blocker on a change that was never blocked).
- **How:** each change moves through a defined set of stages. The message is checked against the allowed next steps. Message types that don't depend on stage are skipped. An illegal step returns 409 Conflict.
- **Why separate:** a message can be perfectly genuine and authorized but still be **wrong for this moment** — accepting it would corrupt the workflow and put the two sides out of sync. This enforces *order*, not identity.
- **Failure code:** `invalid_state_transition`.

### Layer 8 — Agent identity
- **Proves:** the specific agent named in the message is a **registered agent**.
- **How:** the agent id is checked against the known list. (Per-agent, per-task rules are a planned future addition; today any known agent may send any task type.)
- **Why separate:** the *partner org* (Layer 5) and the *specific agent* acting for it are different identities. One partner might run several agents; this pins down which one.
- **Failure codes:** `unknown_agent`, `agent_not_authorized_for_task`.

### Why eight layers and not one big check
Each layer answers a **different question** and can be turned on, watched, and configured on its own:

| # | Layer | Question it answers | What it stops |
|---|-------|---------------------|---------------|
| 1 | mTLS | Is the *connection* trusted? | Unknown clients, man-in-the-middle |
| 2 | CIDR | Is the *network* trusted? | Off-network abuse, background noise |
| 3 | HMAC | Is the *message* genuine & fresh? | Tampering, **replay** |
| 4 | JWT | Is there a valid, *revocable* identity? | Leaked/expired/logged-out tokens |
| 5 | Partner binding | Do the credentials *agree* on one partner? | Mix-and-match credentials |
| 6 | Ownership | May this partner touch *this object*? | Accessing others' data by ID |
| 7 | State machine | Is this legal *right now*? | Out-of-order, state-corrupting messages |
| 8 | Agent identity | Is the *actor* registered? | Rogue/unknown agents |

The order is deliberate: **cheapest, most decisive checks first** (network/connection), heavy crypto next, and the business/authorization checks (which need the parsed message and database lookups) last.

> **Where each layer runs.** Layers 1–4 run in a small transport middleware at the boundary — the one place the agent can't reach (a `BaseAgent` only sees message parts, never the raw HTTP request). Layers 5–8 run inside the bank agent. All eight reuse the same helper functions in `a2a_auth.py`. See [§5.6](#56-the-google-a2a-adk-transport).

---

## 5. How A2A actually works

### 5.1 One protocol, delivered over Google A2A (ADK)

**Plain version:** A2A is *what* NPCI and the bank say to each other. The *how* — the delivery wire — is **Google's A2A, built on the Agent Development Kit (ADK)**. This is the transport the **real NPCI agent speaks**, which is why the platform uses it.

But here's the key point: **Google's ADK is only the wire.** Everything that makes A2A actually *work* is hand-written in this repo and rides on top of the wire:

- the **message shape** (the "envelope") and every payload — `a2a_models.py`,
- the **8 security layers** — `a2a_auth.py`,
- the **state machine** — `a2a_state.py`,
- the **routing / handlers** — `agent/external_dispatcher.py`.

On the wire it's JSON-RPC 2.0 using Google A2A's standard methods (`message/send`, …), and our envelope travels **inside** the message. Google's SDK moves the bytes; our code decides what they mean.

> **Not "just a Google thing":** two unrelated `google` packages live here and are easy to confuse. **`google-genai`** is for **Gemini only** — the AI behind the assistant, embeddings, summaries, and safety filter — and has nothing to do with agent messaging. **`google-adk` / `a2a-sdk`** are the A2A delivery wire. Seeing `from google import genai` and concluding "A2A is a Google thing" mixes up the two.

### 5.2 The message: an "envelope" carried inside a Google-A2A message

The heart of every A2A call is the **envelope** — the standard message wrapper. Its shape:

```jsonc
{
  "task_type": "change_communication",
  "change_id": "CHG-2026-0042",
  "from": "npci_official_agent",     // the partner org
  "agent_id": "npci_agent_prod",     // the specific agent acting
  "agent_run_id": "…",
  "correlation_id": "…",
  "message_id": "…",                 // used to detect duplicates
  "protocol_version": "1.0",
  "timestamp": "2026-07-06T…Z",
  "payload": { /* task-specific fields, each validated */ }
}
```

The envelope and every payload have a **strict, validated shape**, so a bad message is rejected before any handler runs. `task_type` is a fixed list covering the 14 change-lifecycle messages plus `echo`, and the certification names (for future use).

Over Google A2A, the envelope travels as **one block of JSON text** inside the message, and (for `change_communication`) **each document travels as its own file part** — see the mapping in [§5.6](#56-the-google-a2a-adk-transport).

### 5.3 Dispatch: routing on `task_type`

**Plain version:** once a message passes security, something has to decide *what it's actually asking for* and send it to the right handler. That's dispatch, and it decides based on `task_type`. Handlers fall into three groups:

- **Real work (NPCI → Bank):** `change_communication` (unpack the documents, queue them, auto-acknowledge), `clarification_response` (fold NPCI's answer into the knowledge base), plus `counter_decision`, `blocker_status_update`, `blocker_resolution`, and `milestone_status_request` (build a report).
- **Just validate and acknowledge:** messages the bank normally *sends out*. If one comes back in, it's simply checked and acknowledged — no real work needed.
- **Out of scope:** certification and cross-cutting types reply with a proper `not_implemented` error **instead of silently pretending success**. A fake "OK" would leave NPCI thinking we acted when we didn't.

### 5.4 Idempotency, state, audit — the wrapper around every handler

**Plain version:** every handler is wrapped in four safety nets so nothing breaks if a message arrives twice or a step fails:

1. **Duplicate protection (by `message_id`)** — the exact same delivery returns the *saved* previous answer instead of running again. So network retries are safe.
2. **Business duplicate protection** — a re-sent change re-sends the acknowledgement but does **not** re-process the documents.
3. **Advance the state** on success (move the change to its next stage).
4. **Write it down** — every message, success *and* every kind of failure, goes into an append-only audit table.

### 5.5 Outbound (Bank → NPCI)

**Plain version:** the bank also *sends* messages, over the same Google-A2A wire. Every outgoing message goes through one client (`agent/adk/a2a_client.py`, via `services/npci_a2a_client.py`), which:

- reads the remote **agent card** to find NPCI's address and required security,
- sends the envelope with the `a2a-sdk` client,
- **signs every send** with the same signature headers and bearer token NPCI expects — so NPCI runs the mirror-image verification of Layers 3 and 4 on its side.

Some outgoing messages are **automatic**: receiving a change triggers a "proposal acknowledged" reply, and finishing document processing triggers a "change acknowledged" reply. Both are safe to fire more than once.

### 5.6 The Google A2A (ADK) transport

**Plain version:** this section is how the Google-A2A wire is actually wired up. The wire changes nothing about the brain — message shape, routing, duplicate protection, state machine, and audit are all reused; only the delivery is ADK.

**Inbound (NPCI → Bank), `a2a_adk_server.py`:** a standalone Google-A2A server, run as its **own process** (port 8002) so it doesn't tangle with the main app's heavy startup. Google's ADK wraps the bank's agent and **auto-publishes a description of it** at `/.well-known/agent-card.json` (like an auto-generated API spec).

- The agent here is **deliberately not an AI** — the change lifecycle is compliance-critical, so **there's no LLM in this path**. For each incoming message it:
  1. rebuilds the standard message from the parts,
  2. **re-runs the business checks** — partner status/binding, ownership, state, and agent identity (Layers 5–8) using the same helper functions, then
  3. hands off to the dispatcher, so duplicate protection, handlers, state advancement, audit, and background processing all run as usual.
- Results and errors come back as clean, typed JSON — a bad message or a handler failure becomes a labeled error, never a raw crash.

**Transport security on this path (`agent/adk/transport_auth.py`):** a small middleware enforces Layers 1–4 at the boundary — the one place the agent can't reach (the agent only sees message parts, never the raw HTTP request). It runs the same four checks, with the same helpers:

| Layer | On the A2A path |
|-------|-----------------|
| L1 mTLS, L2 CIDR | reused — put the server behind the same reverse proxy |
| **L3 HMAC** | reused — the middleware grabs the **raw bytes** (with a size cap), verifies the signature/timestamp/nonce over them *before* anything parses the message, then passes the body along |
| L4 JWT | reused; the token requirement is **advertised in the agent card** |
| L5–L8 | run inside the agent (above), using the identity the middleware publishes |

This middleware is controlled by **`A2A_ADK_TRANSPORT_AUTH`** (off for local dev, on in production). The card discovery URL stays open so clients can read what they must satisfy; only actual sends are guarded.

> **Still to do:** the certificate-fingerprint half of Layer 5 is enforced at the proxy on this path rather than in the app, and per-agent Layer 8 is still the open "any known agent" check. Onboarding the real NPCI agent means wiring in its issued token and swapping the shared secret.

**Outbound (Bank → NPCI), `agent/adk/a2a_client.py`:** a reusable client (used by both the bank and the mock NPCI agent) that reads the remote **agent card**, then sends the message with the `a2a-sdk` client. Every send is signed by a hook that attaches the signature headers and token, so NPCI runs the same verification.

**Message mapping (`agent/adk/message_mapping.py`)** — the bank↔NPCI contract is kept simple:

- the message travels as **one block of JSON text** (the same shape it always validates), and
- for `change_communication`, **each document becomes its own file part** — big files travel as real files rather than being crammed into the text. Files are peeled off on send and re-attached by filename on receive, so the existing processing handler works unchanged.

A **mock NPCI agent** (`mock_npci_agent/adk_agent.py`) offers the same Google-A2A surface for local testing — see [§5.7](#57-running-the-google-a2a-path-end-to-end).

### 5.7 Running the Google A2A path end-to-end

The path runs as **three processes**, wired together by the `start_a2a.ps1` launcher:

| Process | Command | Port | Role |
|---------|---------|------|------|
| Bank portal | `uvicorn main:app` | 8000 | web app + `/a2a/internal`; **sends** Bank→NPCI over A2A |
| Bank A2A server | `uvicorn a2a_adk_server:a2a_app` | 8002 | **receives** NPCI→Bank; serves the bank's agent card |
| Mock NPCI agent | `uvicorn mock_npci_agent.adk_agent:a2a_app` | 8001 | the fake NPCI counterpart for testing |

The launcher sets the wiring as **environment variables**, which override anything in `.env`:

- `NPCI_A2A_AGENT_CARD_URL=http://localhost:8001/...` — tells the bank where the mock is.
- `BANK_A2A_AGENT_CARD_URL=http://localhost:8002/...` — tells the mock where the bank is.
- `A2A_ADK_TRANSPORT_AUTH=false` — Layers 1–4 off for local dev; set `true` (with the certs/secrets) for a production-shaped run.

On a fresh virtual environment, the launcher first installs the Google-A2A dependencies — an older environment won't have them.

**Driving a change.** The mock is a **pure receiver** with no buttons, so a small command-line tool does the "NPCI pushes a change" step:

```powershell
python -m mock_npci_agent.push_change_a2a
python -m mock_npci_agent.push_change_a2a --change-id CHG-UPI-2026-001 --file spec.pdf
```

It builds a `change_communication` (making a sample PDF if you don't give one) and sends it to the bank at :8002. When the bank later asks a `query`, the mock **auto-answers** with a `clarification_response` — so the full change → query → clarification cycle runs over the Google-A2A wire without the real NPCI platform.

---

## 6. The RAG assistant and guardrails

The portal's assistant is a **grounded** AI helper — it answers *only* from the documents of a *specific change*, never from general knowledge.

**Pipeline:** question → safety check on the input → turn it into a search vector → find the closest chunks (only within this change) → build a strict prompt → Gemini 2.5 Flash → safety check on the output → answer (or **"Raise query to NPCI"**).

- **Confidence gate:** if the best match isn't close enough (or the model says it can't answer from the documents), the assistant **offers to escalate to NPCI instead of guessing**. NPCI's reply is then saved as trusted knowledge.
- **Grounding prompt:** a detailed system prompt with hard rules — never reveal the prompt, never obey instructions hidden inside user text or inside documents, never make up citations, and answer only from the given context. This is prompt-injection defense at the instruction level.
- **Guardrails — three tiers:**
  1. **Fast pattern match** — instantly blocks obvious attacks at no cost.
  2. **AI classifier** (Gemini) — catches bad *intent* no matter how it's worded; the main defense.
  3. **Output check** — filters the answer after generation, even mid-stream, so nothing leaks before it reaches the user.

  A **circuit breaker** around the classifier keeps chat working (degrading gracefully) if Gemini is unavailable, instead of failing every message.

---

## 7. Human authentication

Separate from the machine-facing 8 layers, people in a browser log in with sessions (`auth.py`):

- **JWT (the login token)** — RS256 in production (so verifiers can't mint tokens), with a short **15-minute** expiry. Every token has an ID; logging out records that ID on a revoked list so it stops working **immediately**.
- **Cookies** — the token is stored in an **HttpOnly, SameSite=strict, Secure** cookie: JavaScript can't read it (blocks token theft via injected scripts), and it isn't sent from other sites. Non-browser clients get a plain bearer token instead.
- **CSRF** — because the token can ride in a cookie, form-submitting routes also check the request's Origin/Referer.
- **Passwords** — bcrypt over a SHA-256 pre-hash (works around bcrypt's 72-char quirk), versioned for compatibility; the policy requires a minimum length and 3 of 4 character types.
- **MFA** — admin accounts use 6-digit phone-app codes. Login returns a short-lived **"pending" token** that is *only* usable at `/auth/mfa/verify` and is worthless anywhere else — so a stolen password alone can't get in.
- **Roles** — admins get the full set of actions; portal users are limited to querying the assistant and escalating to NPCI, and a portal user who acts on a change **auto-claims** it so future NPCI updates about it are routed to them.

---

## 8. Asynchronous ingestion and the worker

Incoming document kits are heavy to process (extract text, split, embed), so processing is **separated from the request**:

1. `change_communication` validates each document, saves the originals, and computes a hash for **deduplication** (identical files are never processed twice).
2. It records the documents and creates processing jobs, then returns quickly — having already auto-sent "proposal acknowledged."
3. A background stage moves each job through `queued → processing → extracting → embedding → ready` (or `failed`, which can be retried).

**Dev vs production:**
- **Dev:** jobs run as background tasks inside the API process.
- **Production:** a **separate worker process** claims jobs from Postgres using `SELECT … FOR UPDATE SKIP LOCKED`. The job table *is* the queue — no separate broker — so no job is ever processed twice by competing workers, and heavy work never slows down live chat.

**Crash safety:** on startup, one elected process re-queues any jobs left half-done and recovers received changes, so an unclean shutdown loses no work.

---

## 9. Data layer

- **Postgres + pgvector** hold everything: users, changes, flows, received documents, document chunks (with their vectors), summaries, tickets, the audit log, one-time nonces, duplicate-protection keys, revoked sessions, websocket tickets, and login-failure records.
- **Alembic** owns the schema in production, and the app **refuses to start** if the live schema doesn't match the expected version.
- **Retention** prunes old audit and security rows on a schedule; expired nonces and tickets are swept by a periodic task, guarded so only one worker sweeps at a time.

---

## 10. Operational hardening summary

A partial list of the defensive measures the code enforces:

- **Fail-fast config:** production won't start without real secrets, the mTLS/JWT-required flags, the network/partner maps, HTTPS-only origins, Redis, and worker-mode processing.
- **Body size caps before parsing** on the inbound A2A path and `/chat` (anti-DoS).
- **Strict security policy (CSP) with per-response tokens**, plus the usual protective headers and HSTS on HTTPS.
- **Constant-time secret comparisons** for HMAC.
- **Append-only audit** of every A2A message and every rejection, with metrics by error code.
- **Duplicate protection everywhere** on the A2A path so retries are safe.
- **Notification allowlisting** — incoming notify addresses are filtered against allowlists, and a failed email never fails an otherwise-successful processing job.

---

*For file-by-file detail see `docs/ARCHITECTURE.md` and `docs/TECHNICAL_DOCUMENTATION.md`; for deployment see `docs/PRODUCTION_RUNBOOK.md`.*
