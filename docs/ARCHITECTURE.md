# Architecture & Repository Layout

This document maps the codebase so contributors can navigate a repository whose
core modules currently live in a **flat layout** (all at the repo root). It also
records the recommended target package layout and the exact touchpoints a future
migration must update.

## Runtime entrypoints

| Command | File | Purpose |
| --- | --- | --- |
| `uvicorn main:app` | `main.py` | FastAPI app: middleware, CORS, CSP, router wiring, lifespan (DB init, job recovery, nonce cleanup). |
| `python worker.py` | `worker.py` | Out-of-process ingestion worker (`A2A_INGESTION_MODE=worker`). |
| `uvicorn a2a_adk_server:a2a_app` | `a2a_adk_server.py` | Bank's Google-A2A (ADK) server — separate process/port (`BANK_A2A_PORT`, default 8002); serves the agent card and receives NPCI A2A messages when the ADK transport is used. |

## Core modules (flat, repo root)

Grouped by responsibility. These are plain top-level modules imported absolutely
(e.g. `from database import ...`).

### Platform / infrastructure
| Module | Responsibility |
| --- | --- |
| `config.py` | Shared configuration and logging helpers. |
| `database.py` | SQLAlchemy engine, session, ORM models, `init_db`. |
| `secrets_loader.py` | Load secrets from `*_FILE` paths into the environment. |
| `telemetry.py` | OpenTelemetry distributed tracing setup. |
| `metrics.py` | Prometheus metrics (multi-process Gunicorn aware). |
| `csrf.py` | Origin/Referer CSRF protection for cookie-auth routes. |
| `rate_limit.py` | Rate limiting for security-sensitive endpoints. |
| `utils.py` | Shared helpers. |

### Authentication
| Module | Responsibility |
| --- | --- |
| `auth.py` | User auth, JWT issue/revoke, role dependencies, login models. |

### A2A protocol (Bank-side)
| Module | Responsibility |
| --- | --- |
| `a2a_models.py` | A2A message/envelope schemas. |
| `a2a_auth.py` | HMAC / mTLS / JWT credential binding for A2A. |
| `a2a_state.py` | A2A task/idempotency state. |
| `a2a_audit.py` | A2A-specific audit trail. |

### RAG / AI
| Module | Responsibility |
| --- | --- |
| `retriever.py` | Vector retrieval over document chunks. |
| `ingest.py` | PDF ingestion pipeline. |
| `prompt.py` | Prompt construction. |
| `embedding_config.py` | Shared embedding dimensionality config. |
| `guardrails.py` | Input/output guardrails, verified-stream buffering. |

### Compliance & validation
| Module | Responsibility |
| --- | --- |
| `compliance.py` | External-AI data-processing approval gate. |
| `file_validation.py` | Validate files before OCR/parse/embed/store. |
| `audit_export.py` | Export and verify append-only audit logs. |

## Existing packages

| Package | Contents |
| --- | --- |
| `routes/` | FastAPI routers: `a2a`, `admin`, `auth`, `chat`, `feedback`, `sys`, `ws`. |
| `services/` | A2A ingestor/vision/reply, NPCI clients (`npci_client` legacy JSON-RPC, `npci_a2a_client` Google A2A, `npci_transport` selector on `NPCI_TRANSPORT`), outbound, retention, email, settings/KB stores. |
| `agent/` | A2A dispatch: `dispatcher`, `external_dispatcher`, `methods`, `errors`; `agent/adk/` — Google-A2A transport (`bank_agent` deterministic ADK agent, `transport_auth` L1–L4 middleware, `a2a_client` outbound sender + HMAC/bearer signing, `message_mapping` envelope↔A2A parts). |
| `ws/` | WebSocket connection manager. |

## Supporting directories

| Dir | Purpose |
| --- | --- |
| `alembic/` | DB migrations (`versions/`) + `env.py`. |
| `frontend/` | React/Vite SPA (`src/`), built to `frontend/dist`. |
| `tests/` | Unit tests; `tests/integration/` needs Postgres/pgvector. |
| `deploy/` | nginx, k8s, prometheus, grafana, alertmanager, secrets. |
| `docs/` | Project documentation (this file + tech-stack, runbook, security notes). |
| `mock_npci_agent/` | Local NPCI counterpart for end-to-end testing (`main.py` legacy protocol; `adk_agent.py` Google-A2A sibling on port 8001). |
| `load_tests/`, `tools/`, `scripts/` | Locust load test, ad-hoc tools, ops scripts. |
| `ai_compliance_docs/`, `data/` | Compliance agreement, seed data / docs. |

---

## Recommended target layout (not yet applied)

The flat core modules are the main structural debt. A future migration should
consolidate them into a single importable package:

```
app/
  main.py            # entrypoint -> app.main:app
  worker.py          # entrypoint -> python -m app.worker
  core/              config, database, utils, secrets_loader, telemetry,
                     metrics, csrf, rate_limit
  auth/              auth
  a2a/               models, auth, state, audit
  rag/               retriever, ingest, prompt, embedding_config, guardrails
  compliance/        compliance, file_validation, audit_export
  routes/  services/  agent/  ws/     # moved under app/ unchanged
```

### Migration touchpoints (do all in one PR, then run the test suite)
Moving the core modules rewrites **~160 import statements across ~45 files**.
Beyond the imports, these must be updated together or the app breaks:

1. **Frontend path in `main.py`** — `frontend_dir` is derived from `__file__`
   (`os.path.dirname(__file__)`). If `main.py` moves into `app/`, this must walk
   up one more level, or it will look for `app/frontend`.
2. **Entrypoints** — `Dockerfile` (`main:app`), `docker-compose.prod.yml`
   (`main:app`, `worker.py`), `docker-compose.yml`, `start.ps1` (`main:app`).
3. **CI** — `.github/workflows/ci.yml` mypy step hardcodes the flat module list.
4. **Alembic** — `alembic/env.py` and `versions/20260629_0001_baseline_schema.py`
   import from `database`; migration files are historically frozen, so prefer a
   compatibility shim or update them deliberately.
5. **Config** — `mypy.ini` package bases, `pyproject.toml` coverage sources.

### Verification
`python -m pytest --collect-only -q` catches every broken import (baseline: 104
tests collect). Follow with the full unit run; integration tests require Postgres.
