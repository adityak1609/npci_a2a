# Architecture & Repository Layout

This document maps the codebase so contributors can navigate it. Core modules
are grouped into four topical packages (`core/`, `security/`, `a2a_core/`,
`rag/`); only runtime entrypoints and the ORM hub stay at the repo root.

## Runtime entrypoints (repo root)

| Command | File | Purpose |
| --- | --- | --- |
| `uvicorn main:app` | `main.py` | FastAPI app: middleware, CORS, CSP, router wiring, lifespan (DB init, job recovery, nonce cleanup). |
| `python worker.py` | `worker.py` | Out-of-process ingestion worker (`A2A_INGESTION_MODE=worker`); also owns the daily retention run. |
| `uvicorn a2a_adk_server:a2a_app` | `a2a_adk_server.py` | Bank's Google-A2A (ADK) server — separate process/port (`BANK_A2A_PORT`, default 8002); serves the agent card and receives NPCI A2A messages when the ADK transport is used. |
| `python audit_export.py export\|verify` | `audit_export.py` | Export/verify the append-only audit logs (see `deploy/audit/README.md`). |
| — | `database.py` | SQLAlchemy engine, session, ORM models, `init_db`. Kept at root because it is imported by nearly every module and by Alembic migrations. |

## Packages

### `core/` — cross-cutting plumbing
| Module | Responsibility |
| --- | --- |
| `core/config.py` | Env helpers, Gemini model selection, and logging setup (`LOG_FORMAT=json\|text`, `LOG_LEVEL`, request-id injection). |
| `core/secrets_loader.py` | Load secrets from `*_FILE` paths into the environment. |
| `core/telemetry.py` | OpenTelemetry distributed tracing setup. |
| `core/metrics.py` | Prometheus metrics (multi-process Gunicorn aware). |
| `core/utils.py` | Chat history windowing, title generation, Gemini call-with-fallback wrappers. |

### `security/` — request-security layers
| Module | Responsibility |
| --- | --- |
| `security/auth.py` | User auth, JWT issue/revoke, role dependencies, login models. |
| `security/csrf.py` | Origin/Referer CSRF protection for cookie-auth routes. |
| `security/rate_limit.py` | Rate limiting for security-sensitive endpoints. |
| `security/file_validation.py` | Validate files before OCR/parse/embed/store. |
| `security/compliance.py` | External-AI data-processing approval gate. |
| `security/guardrails.py` | Input/output guardrails, verified-stream buffering. |

### `a2a_core/` — NPCI A2A protocol (Bank-side)
Named `a2a_core` because the Google A2A SDK owns the top-level `a2a` package
name (`from a2a.types import ...`) — a local `a2a/` directory would shadow it.

| Module | Responsibility |
| --- | --- |
| `a2a_core/models.py` | A2A message/envelope schemas (spec v1.0). |
| `a2a_core/auth.py` | 8-layer HMAC / mTLS / JWT credential binding for `/a2a/external`. |
| `a2a_core/state.py` | Change/Certification lifecycle state machines, idempotency state. |
| `a2a_core/audit.py` | A2A-specific append-only audit trail. |

### `rag/` — retrieval pipeline
| Module | Responsibility |
| --- | --- |
| `rag/retriever.py` | Hybrid retrieval: dense (pgvector) + BM25, RRF fusion, optional rerank, source bias, parent dedup. |
| `rag/ingest.py` | PDF ingestion pipeline (`python -m rag.ingest` standalone). |
| `rag/prompt.py` | Prompt construction. |
| `rag/embedding_config.py` | Shared embedding dimensionality config. |

### Pre-existing packages

| Package | Contents |
| --- | --- |
| `routes/` | FastAPI routers: `a2a`, `admin`, `auth`, `chat`, `feedback`, `sys`, `ws`. |
| `services/` | A2A ingestor/vision/reply, OCR gate, reranker, NPCI clients (`npci_client` legacy JSON-RPC, `npci_a2a_client` Google A2A, `npci_transport` selector on `NPCI_TRANSPORT`), outbound, retention, email, settings/KB stores. |
| `agent/` | A2A dispatch: `dispatcher`, `external_dispatcher`, `methods`, `errors`; `agent/adk/` — Google-A2A transport (`bank_agent` deterministic ADK agent, `transport_auth` L1–L4 middleware, `a2a_client` outbound sender + HMAC/bearer signing, `message_mapping` envelope↔A2A parts). |
| `ws/` | WebSocket connection manager. |

## Supporting directories

| Dir | Purpose |
| --- | --- |
| `alembic/` | DB migrations (`versions/`) + `env.py`. Migrations import only `database` and SQLAlchemy — keep it that way so historical revisions stay runnable. |
| `frontend/` | React/Vite SPA (`src/`), built to `frontend/dist`. |
| `tests/` | Unit tests; `tests/integration/` needs Postgres/pgvector. |
| `deploy/` | nginx, k8s, prometheus, grafana, alertmanager, secrets. |
| `docs/` | Project documentation (this file + tech-stack, runbook, security notes). |
| `mock_npci_agent/` | Local NPCI counterpart for end-to-end testing (`main.py` legacy protocol; `adk_agent.py` Google-A2A sibling on port 8001). |
| `load_tests/`, `tools/`, `scripts/` | Locust load test, ad-hoc tools, ops scripts. |
| `ai_compliance_docs/`, `data/` | Compliance agreement, seed data / docs. |

## Logging & debugging

- `core.config.configure_logging()` is called once per process (main, worker,
  ADK server, standalone ingest). Runtime code logs through
  `logging.getLogger(__name__)` — no `print()` in shipping modules, so every
  message carries its module name, level, and request id.
- `LOG_FORMAT=text` gives human-readable local output; the default `json` is
  for container log collectors. `LOG_LEVEL=DEBUG` turns up verbosity.
- Request ids propagate via `core.config.request_id_var` and appear in every
  log line, letting you trace one request across routes → services → RAG.

## Conventions

- **Absolute imports only** (`from security.auth import ...`); package
  `__init__.py` files re-export nothing, keeping the import graph explicit and
  cycle-free (`database.py` imports `rag.embedding_config` while `rag.ingest`
  imports `database` — an eager `rag/__init__` would deadlock that).
- New entrypoints must stay importable at the repo root (Docker/`start*.ps1`
  reference `main:app`, `a2a_adk_server:a2a_app`, `worker.py` by module name).
- CI type-checks `main.py core/ security/ a2a_core/ rag/ agent/ routes/
  services/ ws/` (`.github/workflows/ci.yml`) — add new packages to that list.

## Verification

`python -m pytest --collect-only -q` catches every broken import. Follow with
the full unit run (`python -m pytest --ignore=tests/integration`); integration
tests require Postgres/pgvector.
