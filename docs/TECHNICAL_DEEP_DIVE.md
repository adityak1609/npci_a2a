# NPCI A2A Partner Platform — Technical Deep Dive

*Bank-side implementation of the NPCI Agent-to-Agent (A2A) change-management workflow.*

This document explains **what the platform is built from, why each technology was chosen, how the 8-layer authentication stack works, and how the A2A protocol itself works** — including the common point of confusion: *is A2A the Google library?* (Short answer: it began as NPCI's **custom** JSON-RPC protocol, and a **Google A2A (ADK)** transport has since been added alongside it — the two run in parallel and are flag-selectable; see [§5](#5-how-a2a-actually-works).)

It is a companion to the module-level `docs/ARCHITECTURE.md` and `docs/TECHNICAL_DOCUMENTATION.md`. Where those describe *what each file does*, this one focuses on *the reasoning behind the design*.

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

The platform is **the bank's endpoint** in a two-party machine-to-machine workflow with NPCI. NPCI's agent pushes *change communications* (regulatory/product changes with document kits); the bank's agent receives them, drives an acknowledgement / query / counter-proposal / blocker / milestone lifecycle, and exposes a **partner portal** where bank product managers work through changes with the help of a **RAG assistant grounded in each change's documents**.

There are two distinct classes of caller, and therefore two distinct authentication regimes:

| Caller | Surface | Auth regime |
|--------|---------|-------------|
| **NPCI's agent** (machine) | `POST /a2a/external` | The **8-layer stack** (mTLS + HMAC + JWT + partner/state/agent checks) — [§4](#4-the-8-layer-authentication-stack) |
| **Bank humans** (browser SPA) | `POST /a2a/internal`, REST, SSE, WebSocket | Session **JWT / HttpOnly cookie + optional MFA** — [§7](#7-human-authentication) |

> **Scope note:** this repository implements **only the Bank responsibilities** of the NPCI A2A spec. The NPCI platform and the certification (Part B) simulator are out of scope; a **mock NPCI agent** is bundled for local end-to-end testing. Certification `task_type`s are modelled but return `not_implemented`.

---

## 2. The technology stack — and why

### 2.1 Backend runtime

| Technology | Version | Why it was chosen |
|------------|---------|-------------------|
| **Python 3.10+** | — | The A2A logic is I/O-bound (network calls to NPCI, Postgres, Gemini) rather than CPU-bound, so Python's `async` model fits well. It also has first-class SDKs for everything downstream: Gemini, pgvector, PyMuPDF, cryptography. `StrEnum` (3.11) and modern typing are used throughout. |
| **FastAPI** | 0.138 | Chosen over Flask/Django for three reasons: (1) **native `async`/`await`**, which matters because a single request may fan out to Gemini *and* Postgres concurrently; (2) **Pydantic-based validation** — the entire A2A envelope and every payload is a Pydantic model, so malformed machine traffic is rejected at the edge with a precise error; (3) **dependency injection** (`Depends`) makes the auth stack and DB session composable and testable. |
| **Uvicorn + Gunicorn** | 0.49 / 23.0 | Uvicorn is the ASGI server that actually runs the async app. In production it runs **behind Gunicorn** as a process manager: Gunicorn forks multiple worker processes for CPU-core parallelism and handles graceful restarts, while each worker is a Uvicorn event loop. This is the standard production pattern for FastAPI. |
| **Pydantic** | 2.13 | v2's Rust core makes per-request envelope validation cheap. `StrEnum` task types, `AliasChoices` (to accept both `from` and `from_`), and strict typing give us spec conformance *by construction* — an invalid `task_type` or `doc_type` can't reach a handler. |

**Why async matters here specifically:** the ingestion pipeline (OCR → chunk → embed → summarize) and live chat share the same event loop in dev. Blocking calls (PyMuPDF, Gemini, Postgres) are pushed to a thread pool via `asyncio.to_thread`, and the default executor is sized to the DB pool ceiling (`main.py` lifespan) so threads never outnumber the connections they need.

#### 2.1a A2A transport libraries

| Technology | Version | Why |
|------------|---------|-----|
| **`google-adk[a2a]`** | 2.3.0 | Google's **Agent Development Kit**, used *only* for the optional Google-A2A transport ([§5.6](#56-the-google-a2a-adk-transport)): `to_a2a()` exposes the bank's deterministic agent as an A2A server with an auto-generated agent card. Added to interoperate with the **real NPCI agent, which speaks Google A2A**. The `[a2a]` extra pulls **`a2a-sdk`** (the A2A client + JSON-RPC server). ADK's OTel floor (≥1.39) is why the OpenTelemetry pins in §2.6 are bumped. |
| **`a2a-sdk`** (via ADK extra) | — | The `a2a.client` (`ClientFactory`, `A2ACardResolver`) and `a2a.types` (`Message`, `TextPart`, `FilePart`) used by `agent/adk/a2a_client.py` and the message mapper. Chosen over ADK's `RemoteA2aAgent` (built for LLM sub-agent delegation) for deterministic, file-bearing protocol sends. |

> This is a **separate concern from Gemini.** `google-genai` (§2.2) is the LLM SDK; `google-adk`/`a2a-sdk` here are a wire transport. Both happen to be Google packages — that is the *only* thing they share.

### 2.2 AI / RAG layer

| Technology | Why |
|------------|-----|
| **Google Gemini 2.5 Flash** (`google-genai` SDK 2.10) | The assistant is a *grounded* RAG bot, not a general chatbot — it only needs to reason over retrieved chunks and follow strict grounding rules. **Flash** is the cost/latency-optimized tier, which is the right trade-off for high-volume, low-creativity Q&A and for the guardrail classifier that runs on *every* message. The same SDK is reused for embeddings, summaries, chat, and the semantic guardrail — one dependency, one auth key. |
| **`gemini-embedding-001`** | Produces the vectors stored in pgvector for semantic retrieval. Keeping embeddings and generation in one provider avoids a second vendor integration and keeps the embedding space consistent between ingest-time and query-time. |
| **PyMuPDF (`fitz`)** | 1.27 | Fast, dependency-light PDF text extraction for inbound document kits. Images fall through to a Gemini vision path (`services/a2a_vision.py`). |

> ⚠️ **Important:** the `from google import genai` imports across the codebase are the **Gemini SDK for the LLM** — they have **nothing to do with the A2A protocol**. Don't confuse this with **`google-adk` / `a2a-sdk`** (§2.1a), which are a *separate, optional* Google-A2A transport. A2A's core (envelope, state machine, dispatch, auth) is a hand-written implementation of NPCI's spec; Google's ADK only provides an alternate wire for it. See [§5.1](#51-two-transports-for-one-protocol).

### 2.3 Data + persistence

| Technology | Why |
|------------|-----|
| **PostgreSQL** | Single source of truth for changes, tickets, audit, users, and the durable ingestion queue. Postgres was chosen over a separate message broker because its transactional guarantees let us use **`FOR UPDATE SKIP LOCKED`** to turn a table into a safe work queue (see [§8](#8-asynchronous-ingestion-and-the-worker)) — one fewer moving part to operate. |
| **pgvector** | 0.4 | Stores document-chunk embeddings *inside Postgres*, so semantic retrieval is a SQL query with a `<->` distance operator. This avoids running a dedicated vector database (Pinecone/Weaviate) — retrieval, business data, and audit all live in one ACID store, one backup, one migration chain. |
| **SQLAlchemy 2.0 + Alembic** | ORM for the models and **Alembic** for versioned migrations. Production **refuses to start unless the DB is at the exact expected Alembic head** (`_assert_database_migrated` in `main.py`), derived from the migration scripts themselves — this makes schema drift a hard failure rather than a silent runtime bug. |
| **Redis** | 4.6 | Production-only. Backs **distributed rate limiting** and **worker coordination** across Gunicorn workers, where in-process counters would be per-worker and therefore wrong. Required at startup in production (`_validate_security_config`). |

### 2.4 Security & auth libraries

| Technology | Why |
|------------|-----|
| **`python-jose[cryptography]`** | JWT issue/verify. Supports **RS256** (asymmetric, preferred — private key signs, public key verifies) and falls back to **HS256** for local dev. Asymmetric signing means verifiers never hold a secret capable of *minting* tokens. |
| **`bcrypt`** | Password hashing. A subtle footgun is handled explicitly: bcrypt silently truncates at 72 bytes, so passwords are **SHA-256 pre-hashed and base64-encoded** before bcrypt, and the scheme is **versioned** (`sha256$` prefix) so legacy hashes still verify (`auth.py`). |
| **`cryptography`** | X.509 parsing for mTLS client-certificate fingerprinting (Layer 1). |
| **`pyotp`** | TOTP-based MFA for admin accounts (RFC 6238), with a ±1 window for clock skew. |
| **`hmac` / `hashlib`** (stdlib) | The A2A message signature (Layer 3) is HMAC-SHA256 with **constant-time comparison** (`hmac.compare_digest`) to defeat timing attacks. No external dependency needed. |

### 2.5 Frontend

| Technology | Why |
|------------|-----|
| **React 18 + TypeScript** | The portal is a stateful SPA (dashboard, change board, per-user updates, assistant, KB). TypeScript matches the strictly-typed backend contracts, so envelope/response shapes are checked at compile time. |
| **Vite 5** | Fast dev server (HMR on `:5173`) and an optimized production build with **content-hashed assets** — important because FastAPI serves the built bundle under a strict CSP (hashed filenames + nonce injection). |
| **Tailwind CSS** | Utility-first styling keeps the design system in the markup; no runtime CSS-in-JS cost. |
| **react-router-dom 6** | Client-side routing for the SPA; `/admin/*` paths are all served the same `index.html` (`main.py`) so deep links work. |

The build is baked into the production Docker image in a `node:20` stage; FastAPI then serves `frontend/dist/` and injects a **per-response CSP nonce** into every `<script>`/`<style>` tag (`_inject_csp_nonce`).

### 2.6 Observability & ops

| Technology | Why |
|------------|-----|
| **OpenTelemetry** | Distributed tracing across FastAPI → httpx (outbound NPCI calls) → SQLAlchemy, so a single A2A message can be followed end-to-end. |
| **prometheus-client** | Multiprocess-safe metrics (auth rejections by error code, request latency histograms, not-implemented counts) suitable for Gunicorn's multi-worker model. |
| **Docker / docker-compose** | Reproducible local stack (Postgres+pgvector, Redis) and a multi-stage production image (frontend build → Python runtime). |
| **httpx** | Async HTTP client for **outbound** Bank→NPCI JSON-RPC calls, with per-attempt timeouts, retry/backoff, and mTLS client-cert support (`services/npci_client.py`). |

---

## 3. Request flows

### 3.1 Inbound A2A (NPCI → Bank)

```
NPCI agent
  │  POST /a2a/external   (JSON-RPC 2.0 body; HMAC + JWT headers; mTLS at proxy)
  ▼
[ _read_limited_body ]  ── app-level body cap before any parsing (anti-DoS)
  ▼
[ a2a_external_auth ]  ── the 8 layers, short-circuiting on first failure
  ▼
[ dispatch_external ]  ── idempotency claim → task_type handler → state advance → audit
  ▼
JSON-RPC result   +   (often) an auto-fired Bank→NPCI acknowledgement
```

### 3.2 Portal (browser → Bank)

```
React SPA ──session JWT / HttpOnly cookie──▶ POST /a2a/internal
   │                                            │
   │  role check (admin vs portal user)         ▼
   │                                   method allowlist (INTERNAL vs PORTAL)
   ▼                                            ▼
 REST / SSE (/chat/stream) / WebSocket   agent.query_rag, agent.escalate_to_npci, …
```

The two A2A endpoints deliberately speak **different vocabularies**: `/a2a/external` dispatches on the spec's `task_type`; `/a2a/internal` dispatches on internal method names like `agent.query_rag`. The old shared `/a2a` endpoint now returns **410 Gone** to force callers onto the right one.

---

## 4. The 8-layer authentication stack

This is the security core of `/a2a/external`, implemented in `a2a_auth.py` and orchestrated by `a2a_external_auth()`. It exists because the caller is **another machine on a public network**, so we cannot rely on a browser session. Each layer is **defense-in-depth**: independently valuable, run in order, and **short-circuiting** — the first failure raises `A2AAuthError` carrying a spec-defined error-code string (so NPCI's SOC tooling and our own Prometheus counters can pattern-match), and no later layer runs.

> **Design principle — fail closed, fail specific.** Every rejection returns a stable machine-readable `a2a_error` code (`mtls_required`, `replay_detected`, `partner_mismatch`, …) and is written to the `a2a_messages` audit table via `log_a2a_rejection`. Rejections are counted in `a2a_auth_rejections_total{error_code}`.

Below, each layer: **what it proves**, **how**, and **why it's separate**.

### Layer 1 — mTLS (mutual TLS)
- **Proves:** the transport itself was established by a client holding a certificate we trust — network-level identity, before we read a single byte of the body.
- **How:** TLS is terminated at the reverse proxy (Nginx), which forwards `X-SSL-Client-Verify`, `X-SSL-Client-Fingerprint`, and the escaped PEM in `X-SSL-Client-Cert`. `check_mtls` rejects `NONE`/non-`SUCCESS` verification and then checks the certificate's **SHA-256 fingerprint** against `A2A_MTLS_FINGERPRINTS`. Fingerprints are normalized (colons/case stripped) so config format doesn't matter.
- **Why separate:** it's the only layer that authenticates the *TLS channel* rather than the *message*. It stops unknown clients before any application work. Required in production; can be disabled in dev so the plain-HTTP mock agent can connect.
- **Failure codes:** `mtls_required`, `mtls_fingerprint_mismatch`.

### Layer 2 — CIDR allowlist
- **Proves:** the request originated from an approved network range.
- **How:** `check_cidr` parses `request.client.host` and tests membership in the `A2A_CIDR_ALLOWLIST` networks (`ipaddress.ip_network`).
- **Why separate:** cheap, coarse, and independent of certificates — it defends against a stolen/mis-issued cert being used from an unexpected network, and blocks internet background-noise before the expensive crypto in Layer 3.
- **Failure code:** `ip_not_allowed`.

### Layer 3 — HMAC envelope verification (the message signature)
- **Proves:** the **body was not tampered with**, is **fresh** (not replayed), and was produced by a party holding the shared secret.
- **How (`verify_hmac`):** three headers are required — `X-NPCI-Timestamp`, `X-NPCI-Nonce`, `X-NPCI-Signature`. The signature is `HMAC-SHA256(secret, "{timestamp}.{nonce}.{sha256_hex(raw_body)}")`. Three independent checks:
  1. **Timestamp skew** — rejected if more than **±300 s** from server time (`timestamp_skew`), bounding replay windows.
  2. **Signature match** — computed with `A2A_HMAC_SECRET` and compared using **constant-time** `hmac.compare_digest` (`signature_mismatch`). The signature is verified *first, before* touching the nonce table, so bad-signature floods can't fill storage.
  3. **Nonce replay** — the nonce must be unused; it's inserted into `a2a_nonces` with a TTL. A duplicate (or a unique-constraint race on insert) is treated as `replay_detected` — **fail-closed**. Expired nonces are reaped by a periodic background task, not on the hot path (to avoid write amplification).
- **Why separate:** this is the primary message-integrity/authenticity proof. mTLS secures the pipe; HMAC secures the *content*. The raw body must be read **before** JSON parsing precisely so the bytes signed are the bytes verified.
- **Failure codes:** `missing_envelope_headers`, `timestamp_skew`, `signature_mismatch`, `replay_detected`.

### Layer 4 — JWT verification + session revocation
- **Proves:** a valid, non-revoked bearer credential accompanies the call, and yields a subject (`sub`) identity.
- **How (`verify_jwt_session`):** the `Authorization: Bearer` token is decoded with the same `decode_token` used for humans (signature + `exp` enforced). If it carries a `jti`, that id is checked against `revoked_sessions` so a **leaked or logged-out token stops working before its natural expiry**. JWT is *required* in production (`A2A_JWT_REQUIRED_EXTERNAL`); HMAC is the primary proof so JWT can be relaxed in dev.
- **Why separate:** HMAC proves *possession of a shared secret*; JWT provides a *revocable, per-subject* identity that can be turned off instantly without rotating the HMAC secret for everyone.
- **Failure codes:** `invalid_token`, `session_revoked`, `session_expired`.

*(The JSON body is parsed into an `A2AEnvelope` Pydantic model at this point; a malformed envelope is itself an auth failure.)*

### Layer 5 — Partner status + credential binding
- **Proves:** the claimed partner (`from`) is an **active** partner, **and** the credentials from Layers 1/4 are actually *bound* to that partner — not just individually valid.
- **How:** `check_partner_status` checks `from` against `A2A_KNOWN_AGENTS`. `check_credential_binding` then enforces the mappings: the **JWT subject** must map to the claimed partner (`A2A_JWT_PARTNER_MAP`), and an **mTLS fingerprint** must map to it (`A2A_MTLS_PARTNER_MAP`).
- **Why separate:** this defeats **credential-confusion / mix-and-match** attacks — Partner A presenting Partner B's valid JWT, or a valid cert with someone else's envelope. Individual validity (Layers 1/4) is not enough; the identities must agree.
- **Failure codes:** `partner_inactive`, `partner_mismatch`.

### Layer 6 — Resource ownership (`change_id` / `cflow_id`)
- **Proves:** the specific change/flow the message operates on **exists** and **belongs to the calling partner**.
- **How (`check_ownership`):** for task types that reference existing entities, the `change_id`/`cflow_id` is looked up; a miss is `unknown_id` (404) and a partner mismatch is `partner_mismatch` (403). Entity-*creating* types (`change_communication`, `echo`, `status_request`, `notification`) are skipped — there's nothing to own yet.
- **Why separate:** authorization ≠ authentication. Layers 1–5 establish *who you are*; Layer 6 establishes *whether you may touch this object* — an **IDOR / horizontal-privilege-escalation** guard.
- **Failure codes:** `unknown_id`, `partner_mismatch`.

### Layer 7 — State-machine validity
- **Proves:** the message is **legal for the change's current lifecycle state** (e.g. you can't send a `blocker_resolution` for a change that was never blocked).
- **How (`check_state_transition` → `a2a_state.validate_transition`):** the change lifecycle is a state machine (`ASSIGNED → COMMUNICATED → RECEIVED → ACCEPTED → … → CERTIFIED`, plus `COUNTER_PROPOSAL`/`ABORTED`). Stateless types (`echo`, `query`, `status_*`, `clarification_response`, `notification`) skip this. An illegal transition is `409 Conflict`.
- **Why separate:** a message can be perfectly authenticated *and* authorized but still be **nonsensical for where the workflow is** — accepting it would corrupt state and desynchronize the two agents. This layer enforces protocol *ordering*, not identity.
- **Failure code:** `invalid_state_transition`.

### Layer 8 — Agent identity
- **Proves:** the `agent_id` inside the envelope is a **registered agent**, and (extension point) is authorized for this `task_type`.
- **How (`check_agent_identity`):** `agent_id` is checked against `A2A_KNOWN_AGENTS`. Per-agent, per-task authorization is a documented future table; today any known agent may send any task type.
- **Why separate:** `from` (the partner org, Layer 5) and `agent_id` (the specific software agent acting) are different identities. A partner may run several agents; this pins the *actor*.
- **Failure codes:** `unknown_agent`, `agent_not_authorized_for_task`.

### Why eight layers and not one big check
Each layer answers a **different question** and can be **configured, monitored, and disabled independently**:

| # | Layer | Question it answers | Class of attack it stops |
|---|-------|---------------------|--------------------------|
| 1 | mTLS | Is the *channel* trusted? | Unknown clients, MITM |
| 2 | CIDR | Is the *network* trusted? | Off-network abuse, background noise |
| 3 | HMAC | Is the *message* authentic & fresh? | Tampering, **replay** |
| 4 | JWT | Is there a valid, *revocable* identity? | Leaked/expired/logged-out tokens |
| 5 | Partner binding | Do the credentials *agree* on one partner? | Credential confusion / mix-and-match |
| 6 | Ownership | May this partner touch *this object*? | **IDOR** / horizontal priv-esc |
| 7 | State machine | Is this legal *now*? | Out-of-order / state-corrupting messages |
| 8 | Agent identity | Is the *actor* registered? | Rogue/unknown agents |

The ordering is deliberate: **cheapest and most decisive checks first** (network/transport), expensive crypto next, and business/authorization logic (which needs the parsed envelope and DB lookups) last.

> **Same eight layers on the Google-A2A transport.** This section describes the layers as they run on the legacy `/a2a/external` endpoint. When the ADK transport is used, the *identical* eight layers apply — L1–L4 move to an ASGI middleware and L5–L8 run inside the ADK agent, but they reuse these same `a2a_auth` helpers. See [§5.6](#56-the-google-a2a-adk-transport).

---

## 5. How A2A actually works

### 5.1 Two transports for one protocol

A2A on this platform now has **two coexisting wire transports**, selectable per deployment. Understanding the distinction is the single most important clarification:

- **`legacy` (default) — NPCI's custom protocol.** A bespoke implementation of NPCI's own *"Unified AgentNxt A2A Protocol Specification v1.0"* (see the module docstring in `a2a_models.py`): plain **JSON-RPC 2.0 over HTTPS**, secured by the custom **8-layer** stack ([§4](#4-the-8-layer-authentication-stack)). The envelope, task types, state machine, auth, idempotency, and dispatch are all hand-written in this repo (`a2a_models.py`, `a2a_auth.py`, `a2a_state.py`, `agent/external_dispatcher.py`). **Sections 5.2–5.5 describe this path.**
- **`a2a` — Google A2A via the ADK.** An **additive** transport built on Google's **Agent Development Kit** (`google-adk[a2a]`, which pulls the `a2a-sdk` client). The NPCI spec's reference implementation *aligns with Google's A2A ecosystem*: the spec's transport stack is `A2A SDK (Google A2A ADK) → JSON-RPC 2.0 → HMAC envelope → JWT bearer → TLS/mTLS → nginx` — i.e. the Google SDK is the **transport**, and NPCI's protocol layers (the envelope, HMAC, JWT, all 8 auth layers) ride **on top of it**. Both transports are JSON-RPC 2.0 on the wire; the difference is the **method vocabulary** — the legacy path overloads the JSON-RPC `method` to carry the `task_type`, whereas Google A2A uses the *standard* A2A methods (`message/send`, …) and carries the **same `A2AEnvelope`** inside the message parts. It reuses the existing dispatcher and business logic verbatim. **Section 5.6 describes this path.**

Both are **flag-selectable and run in parallel** — outbound transport is chosen by the `NPCI_TRANSPORT` setting (`services/npci_transport.py`); on the inbound side the legacy `POST /a2a/external` endpoint (`main.py`) and the new ADK server (`a2a_adk_server.py`) coexist as **separate processes**. Nothing about the custom endpoint or the 8-layer stack changed when the ADK path was added — it is purely additive, per the parallel-run migration strategy.

> **Still not "just a Google thing":** two unrelated `google` dependencies live in this repo and are easy to conflate. **`google-genai`** (`from google import genai`) is for **Gemini only** — the LLM behind the RAG assistant, embeddings, summaries, and the guardrail classifier — and has nothing to do with agent-to-agent messaging. **`google-adk` / `a2a-sdk`** are the (optional) A2A *transport*. Reading `from google import genai` and concluding "A2A = a Google thing" conflates the two.

### 5.2 The wire format: JSON-RPC 2.0 + an A2A envelope

Every A2A call is a JSON-RPC 2.0 request whose `params` object is the **A2A envelope**:

```jsonc
{
  "jsonrpc": "2.0",
  "id": "req-123",
  "method": "change_communication",   // mirrors task_type
  "params": {                          // ← the A2A envelope
    "task_type": "change_communication",
    "change_id": "CHG-2026-0042",
    "from": "npci_official_agent",     // partner org (accepted as `from` or `from_`)
    "agent_id": "npci_agent_prod",     // the acting agent
    "agent_run_id": "…",
    "correlation_id": "…",
    "message_id": "…",                 // idempotency key
    "protocol_version": "1.0",
    "timestamp": "2026-07-06T…Z",
    "payload": { /* task-specific, validated by a Pydantic model */ }
  }
}
```

The envelope and every payload are **Pydantic models** (`a2a_models.py`), so the shape is validated before any handler runs. `task_type` is a `StrEnum` covering all 14 Phase-C change-lifecycle messages plus `echo`, and the certification (Part B) names for forward compatibility.

### 5.3 Dispatch: routing on `task_type`

After auth, `agent/external_dispatcher.py` routes on `task_type` through the `EXTERNAL_HANDLERS` registry. Handlers fall into three groups:

- **Inbound (NPCI→Bank) — real work:** `change_communication` (unpack the document kit, queue ingestion, auto-ack), `clarification_response` (fold NPCI's answer into the KB), `counter_decision`, `blocker_status_update`, `blocker_resolution`, `milestone_status_request` (→ build a report).
- **Loopback / validate-and-ack:** messages the Bank normally *sends* (`counter_proposal`, `milestone_update`, `readiness_declaration`, `change_acknowledgement`, …) are simply validated and acknowledged if received back.
- **Out of scope:** certification (Part B) and cross-cutting types return a spec-compliant `not_implemented` (JSON-RPC `-32601`) **rather than silently ACKing** — a silent success would desynchronize state (NPCI thinks we acted; we didn't).

### 5.4 Idempotency, state, audit — the wrapper around every handler

`dispatch_external` wraps each handler with:

1. **Transport idempotency** on `message_id` — a duplicate delivery returns the *cached* prior result instead of re-executing (`claim_idempotency_key`). Network retries are therefore safe.
2. **Business idempotency** on a per-type key (e.g. `(change_id, version)` for `change_communication`) — a re-sent change re-fires the acknowledgement but does not re-ingest.
3. **State advancement** on success (`advance_change_state` / `advance_cert_state`).
4. **Audit logging** of success *and* every failure class into the append-only `a2a_messages` table.

### 5.5 Outbound (Bank → NPCI)

The Bank is also a *client*. Every outbound send goes through **one transport selector** — `services/npci_transport.py` exposes `post_npci_rpc`, and callers (`services/a2a_ingestor.py` auto-fires, `services/npci_outbound.py` senders) import it from there so switching transports is a **single config flip** (`NPCI_TRANSPORT`) with no code change:

- **`legacy` (default) → `services/npci_client.py`** sends JSON-RPC to NPCI over **httpx** with:
  - **Outbound HMAC** — it signs its own requests with the same `{ts}.{nonce}.{sha256(body)}` scheme (so NPCI can run the mirror-image verification).
  - **mTLS client cert** (`NPCI_CLIENT_CERT_FILE`/`KEY_FILE`, required in production).
  - **Retry with backoff** (3 attempts, 1/2/4 s) and per-attempt timeouts.
- **`a2a` → `services/npci_a2a_client.py`** forwards the same envelope over Google A2A (see [§5.6](#56-the-google-a2a-adk-transport)). The legacy JSON-RPC `params` object *is* the envelope, so it is sent as-is and NPCI's reply is re-wrapped in a JSON-RPC-shaped dict — existing callers are unaware which transport ran.

Several outbound messages are **auto-fired**: receiving a `change_communication` triggers `proposal_acknowledged`, and successful ingestion triggers `change_acknowledgement`, both idempotent (`services/a2a_ingestor.py`).

### 5.6 The Google A2A (ADK) transport

Added so the platform can interoperate with the **real NPCI agent, which speaks Google's A2A protocol**. It reuses the entire business core — envelope model, dispatcher, idempotency, state machine, audit — and only swaps the *wire*.

**Inbound (NPCI → Bank), `a2a_adk_server.py`:** a standalone Google-A2A server, run as its **own process** (`uvicorn a2a_adk_server:a2a_app`, default port 8002) to avoid nesting ASGI lifespans with `main.py`'s heavy startup. `google.adk...to_a2a()` wraps the bank's root agent and **auto-generates the agent card** at `/.well-known/agent-card.json` (the ADK equivalent of an OpenAPI/`securitySchemes` descriptor).

- The root agent (`agent/adk/bank_agent.py`) is a **deterministic `BaseAgent`, not an `LlmAgent`** — the change lifecycle is compliance-critical, so **there is no LLM in this path**. On each inbound message it:
  1. reconstructs the `A2AEnvelope` from the message parts,
  2. **re-runs the business-authz layers** — partner status + binding (`check_partner_status`, `check_partner_binding_sub`), `check_ownership`, `check_state_transition`, and `check_agent_identity` (spec §4.1 **layers 5–8**) via the existing `a2a_auth` helpers, then
  3. delegates to the **unchanged** `agent/external_dispatcher.dispatch_external`, so idempotency, task_type handlers, state advancement, audit, and async ingestion are reused verbatim.
- Handler results and errors are returned as structured JSON — a malformed message or handler failure becomes a typed error object (`invalid_message`, `not_implemented`, …), never a raw 500 over A2A. Authz rejections (L5–L8) are also written to the `a2a_messages` audit table, matching the legacy endpoint.

**Transport auth on the ADK path (`agent/adk/transport_auth.py`).** A pure-ASGI middleware enforces spec §4.1 **layers 1–4** at the boundary — the insertion point the agent can't reach (a `BaseAgent` only sees message parts, never the HTTP request). Per the spec's transport stack, the NPCI protocol layers ride **on top of** the Google A2A SDK, so it runs the **same four checks as the legacy endpoint, with the same helpers**:

| Layer | On the ADK path |
|-------|-----------------|
| L1 mTLS, L2 CIDR | reused (`check_mtls`, `check_cidr`) — put the server behind the same reverse proxy |
| **L3 HMAC envelope** | reused (`verify_hmac`) — the middleware buffers the **raw body bytes** (with the legacy size cap), verifies the signature/timestamp/nonce over them *before* the a2a-sdk parses the message, then replays the body downstream |
| L4 JWT (+revocation) | reused (`verify_jwt_session`); the bearer requirement is **advertised in the agent card's `securitySchemes`** |
| L5–L8 | run inside the agent (above), using the subject the middleware publishes on a contextvar |

The middleware is gated by **`A2A_ADK_TRANSPORT_AUTH`** (default **off** for local dev; set **on** in production). Card discovery (`GET /.well-known/agent-card.json`) stays open so clients can read the scheme they must satisfy; only RPC sends (`POST`) are guarded. Rejections return an A2A/JSON-RPC error and are audited to `a2a_messages`.

> **Remaining follow-up:** the mTLS-fingerprint half of partner-binding (L5) is enforced at the proxy on this path rather than in-process, and per-agent/per-task authorization (L8) is still the same open "any known agent" check as the legacy endpoint. Onboarding the real NPCI agent needs its issued bearer token wired into `A2A_JWT_PARTNER_MAP` and the shared HMAC secret exchanged.

**Outbound (Bank → NPCI), `agent/adk/a2a_client.py`:** a reusable client (used by both the Bank and the mock NPCI agent) that resolves the remote **agent card**, then sends the envelope with the `a2a-sdk` `ClientFactory` — deliberately *not* ADK's `RemoteA2aAgent`, which is built for LLM sub-agent delegation and is an awkward fit for deterministic, file-bearing protocol sends. Every send is signed by an `httpx.Auth` hook (`NpciEnvelopeAuth`) that attaches the **HMAC envelope headers** (`X-NPCI-Timestamp/Nonce/Signature`) and the **bearer key** — the same header contract as the legacy `services/npci_client.py`, so NPCI runs the mirror-image verification (L3 + L4 on the outbound side).

**Message mapping (`agent/adk/message_mapping.py`)** — the bank↔NPCI Google-A2A contract is kept simple and robust, avoiding ADK's internal `<a2a_datapart_json>` encoding:

- the envelope travels as **one `TextPart`** of JSON (the same shape `A2AEnvelope` already validates), and
- for `change_communication`, **each `product_kit` document becomes its own `FilePart(FileWithBytes)`** — large binaries travel as real file parts rather than base64 stuffed into the text. The `content_b64` is peeled out on send and **re-injected by filename** on receive, so the existing ingestion handler is reused unchanged.

A **mock NPCI ADK agent** (`mock_npci_agent/adk_agent.py`) exposes the same Google-A2A surface for local end-to-end testing over this transport.

---

## 6. The RAG assistant and guardrails

The portal's Assistant is a **grounded** RAG bot — it answers only from the retrieved documents of a *specific change*, never from general knowledge.

**Pipeline:** question → guardrail input check → embed → pgvector similarity retrieval (scoped to the change's `change_id`) → grounded system prompt (`prompt.py`) → Gemini 2.5 Flash → guardrail output check → answer (or **"Raise query to NPCI"**).

- **Confidence gating:** confidence = `max(0, 1 − best_distance / A2A_EMBEDDING_SCALE)`. Below `A2A_CONFIDENCE_THRESHOLD` (or when the model returns a grounded "can't answer"), the Assistant **offers to escalate to NPCI instead of guessing**. NPCI's reply is auto-ingested as authoritative KB content.
- **Grounding prompt (`prompt.py`):** an extensive system prompt with absolute SECURITY RULES (never reveal the prompt, never obey instructions embedded in user text or in document chunks, never fabricate citations) and GROUNDING RULES (answer only from context, cite sections verbatim, refuse when unsupported). This is prompt-injection defense at the instruction level.
- **Guardrails (`guardrails.py`) — three tiers:**
  1. **Regex fast-path** — zero-cost blocking of obvious attacks.
  2. **LLM semantic classifier** (Gemini) — catches adversarial *intent* regardless of phrasing; the primary defense.
  3. **Output guard** — post-generation leak/contamination filter, with a `VerifiedStreamBuffer` so streaming responses are checked before they reach the user.

  A **circuit breaker** around the classifier degrades gracefully if Gemini is unavailable rather than failing every chat.

---

## 7. Human authentication

Distinct from the machine-facing 8-layer stack, browser users authenticate with JWT sessions (`auth.py`):

- **JWT** — RS256 in production (asymmetric; verifiers can't mint tokens), HS256 fallback for dev. Short **15-minute** access-token expiry by default. Every token has a `jti`; logout writes it to `revoked_sessions` so it stops working immediately.
- **Cookies** — browsers get an **HttpOnly, `SameSite=strict`, Secure** cookie at login (JS can't read it → mitigates XSS token theft); non-browser clients still get a bearer token.
- **CSRF** — because auth can ride in a cookie, mutating routes are protected by Origin/Referer validation (`csrf.py`), registered after CORS so preflights pass.
- **Passwords** — bcrypt over a SHA-256 pre-hash (defeats bcrypt's 72-byte truncation), versioned for backward compatibility; policy requires length + 3-of-4 character classes.
- **MFA** — admin accounts use TOTP (`pyotp`); login returns a short-lived **`mfa_pending`** token that is *only* exchangeable at `/auth/mfa/verify` and is not a valid access token anywhere else.
- **Roles** — `get_current_user` / `get_current_admin` dependencies gate routes; on `/a2a/internal`, admins get the full method set while portal users are restricted to `query_rag` + `escalate_to_npci`, and a portal user acting on a change **auto-claims ownership** so future NPCI updates scope to them.

---

## 8. Asynchronous ingestion and the worker

Inbound document kits are heavy (OCR, chunking, embedding), so ingestion is **decoupled from the request**:

1. `change_communication` validates each `product_kit` entry, stores originals under `data/a2a_received/`, and computes `content_hash = sha256(bytes)` for **deduplication** (identical bytes are never re-ingested).
2. It creates `received_documents` + `a2a_ingestion_jobs` rows and returns quickly, having auto-fired `proposal_acknowledged`.
3. A background stage runs `queued → processing → extracting → embedding → ready` (or `failed`, retryable).

**Dev vs production execution model:**
- **Dev (inline):** jobs run as tracked `asyncio` tasks in the API process.
- **Production (`A2A_INGESTION_MODE=worker`, enforced at startup):** a **separate `worker.py` process** claims jobs from Postgres with **`SELECT … FOR UPDATE SKIP LOCKED`**. The `a2a_ingestion_jobs` / `documents` tables *are* the queue — no second broker — so a job can never be processed twice by competing consumers, and heavy work never degrades live chat latency.

**Crash safety:** on startup, one process (elected via a Postgres **advisory lock**) re-queues jobs orphaned in transient states and recovers durable received-change rows, so an unclean shutdown loses no work.

---

## 9. Data layer

- **Postgres + pgvector** hold everything: `users`, `a2a_changes`, `a2a_cflows`, `received_documents`, `document_chunks` (with `embedding vector`), `document_summaries`, `npci_tickets`, `a2a_messages` (audit), `a2a_nonces`, `a2a_idempotency_keys`, `revoked_sessions`, `ws_tickets`, `audit_logs`, and `login_failures`.
- **Alembic** owns the schema in production; the app compares the live `alembic_version` against the migration head derived from the scripts and **refuses to start on mismatch**.
- **Retention** (`services/retention.py`) prunes old audit/security rows on a schedule; expired nonces and WS tickets are swept by the periodic maintenance task, advisory-lock-guarded so only one worker sweeps per tick.

---

## 10. Operational hardening summary

A non-exhaustive list of defensive measures the code enforces:

- **Fail-fast security config** (`_validate_security_config`): production won't boot without real (high-entropy) `A2A_HMAC_SECRET`, `NPCI_API_KEY`, `SECRET_KEY`, mTLS/JWT required flags, CIDR/fingerprint/partner maps, HTTPS-only CORS origins, Redis, and worker-mode ingestion.
- **Body caps before parsing** on both `/a2a/external` and `/chat` (anti-DoS).
- **Strict CSP with per-response nonces**, plus `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy`, `Permissions-Policy`, and HSTS on HTTPS.
- **Constant-time secret comparisons** for HMAC.
- **Append-only audit** of every A2A message and every auth rejection, with Prometheus counters by error code.
- **Idempotency everywhere** on the A2A path so retries are safe.
- **Notification allowlisting** — inbound `notify_list` addresses are filtered through allowlists/allowed-domains; a failing SMTP send never fails an otherwise-successful ingestion.

---

*For file-by-file detail see `docs/ARCHITECTURE.md` and `docs/TECHNICAL_DOCUMENTATION.md`; for deployment see `docs/PRODUCTION_RUNBOOK.md`.*
