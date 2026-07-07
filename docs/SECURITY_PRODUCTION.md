# Production Security Notes

This application is configured to fail closed for NPCI-facing A2A traffic.

## Required Before Production

- Set real high-entropy values for `SECRET_KEY`, `A2A_HMAC_SECRET`, and `NPCI_API_KEY`.
- Load secrets from a managed secret store or mounted secret files; never bake them into images or commit `.env.production`.
- Set `APP_ENV=production` and `AUTH_COOKIE_SECURE=true` behind HTTPS.
- Run `alembic upgrade head` before starting the API; production startup refuses unmanaged schemas.
- Set `DATABASE_URL` to managed PostgreSQL 16 with pgvector and `REDIS_URL` to managed Redis.
- Set `A2A_INGESTION_MODE=worker` and run `python worker.py` separately from the API.
- Keep `A2A_MTLS_REQUIRED=true` and terminate inbound mTLS at a trusted reverse proxy that forwards `X-SSL-Client-Verify` and `X-SSL-Client-Fingerprint`; let the app perform the final mTLS decision so failed certificate attempts are written to the A2A audit chain.
- Keep `A2A_JWT_REQUIRED_EXTERNAL=true` unless the NPCI contract explicitly removes that layer.
- Configure `A2A_CIDR_ALLOWLIST` and `A2A_MTLS_FINGERPRINTS` for the NPCI endpoints used in production.
- Configure `A2A_MTLS_PARTNER_MAP` and `A2A_JWT_PARTNER_MAP` so the claimed A2A `from` partner is bound to the presented certificate/JWT subject. mTLS map keys may be the proxy-native fingerprint or the SHA-256 fingerprint computed from `X-SSL-Client-Cert`.
- Configure `NPCI_AGENT_URL`, `NPCI_CLIENT_CERT_FILE`, and `NPCI_CLIENT_KEY_FILE`; outbound `proposal_acknowledged` messages and queries are signed and sent over mTLS instead of only logged locally.
- If the Google-A2A transport is used (`NPCI_TRANSPORT=a2a` or the ADK server `a2a_adk_server.py` is reachable by NPCI), set `A2A_ADK_TRANSPORT_AUTH=true` and place the ADK server behind the same mTLS-terminating reverse proxy as `/a2a/external`. The ADK path enforces the same spec §4.1 layers: mTLS/CIDR/HMAC/JWT in `agent/adk/transport_auth.py`, partner/ownership/state/agent checks in the agent. Never expose the ADK server with the flag off.
- Keep `EXTERNAL_AI_DATA_PROCESSING_APPROVED=false` until NPCI data handling, residency, and vendor terms explicitly permit sending policy/payment data to Gemini.
  - **Data Residency Policy (Override permitted)**: NPCI data must remain within the geographic boundaries specified by the compliance agreement.
  - **Vendor Controls & Redaction**: All sensitive user PII must be redacted before being sent to the Gemini API. Vendor terms dictate that Google will NOT use the sent data for model training.
- If external AI is not approved, migrate embeddings, chat, summary, and OCR/vision to an approved self-hosted or private deployment (e.g. vLLM with Llama 3) before enabling those workflows.
- Review the NPCI agreement/spec for audit retention, hash-chain/export, and append-only storage requirements.

## Implemented Guardrails

- A2A mTLS and external JWT fail closed by default and are validated at startup.
- HMAC timestamp, nonce format, signature format, replay storage, and scheduled nonce cleanup are enforced.
- A2A request bodies are capped before buffering, and invalid HMAC requests are rejected before nonce rows are written.
- A2A external credentials are bound to partner identities before state or document handlers run; Nginx forwards the escaped client certificate so the app can compute a SHA-256 fingerprint consistently.
- The Google-A2A (ADK) server applies the same auth chain when `A2A_ADK_TRANSPORT_AUTH=true`: raw-body HMAC verification before message parsing, JWT + revocation, and in-agent partner/ownership/state/agent checks; outbound ADK sends are HMAC-signed and bearer-authenticated (`NpciEnvelopeAuth`), and rejections are written to the same `a2a_messages` audit chain.
- Inbound A2A transport and business idempotency keys are claimed before handler side effects, preventing concurrent retries from double-processing the same message.
- Known but unimplemented NPCI task types return explicit JSON-RPC errors and audit entries instead of ACKs.
- WebSocket subscriptions use short-lived single-use tickets instead of long-lived JWT/API key query parameters.
- Browser admin auth uses an HttpOnly, SameSite=strict cookie while retaining Bearer-token compatibility for API clients.
- Baseline browser security headers are added by the FastAPI app, including CSP, frame denial, nosniff, referrer policy, and HTTPS-only HSTS.
- Chat, login, and `/a2a/external` are rate limited through Redis when `REDIS_URL` is configured; local development falls back to in-process limits.
- Upload and A2A ingestion validate file type, size, base64, PDF magic, parseability, encryption, and page count before OCR/embedding.
- A2A audit rows and admin audit rows are hash-chained and protected by append-only ORM hooks plus database triggers.
- KB content has visibility-tier fields and retrieval supports tier filtering.
- `/metrics` exposes Prometheus-compatible counters and latency histograms for scraping.
- `audit_export.py` verifies hash chains and exports JSONL audit artifacts for immutable storage.
- `docker-compose.prod.yml` provides separate migration, API, worker, TLS/mTLS reverse-proxy, and Prometheus services for deploy-time wiring.

## Operational Requirements

- Put the app behind TLS, WAF/reverse proxy request-size limits, and centralized access logs.
- Use managed database backups with restore drills, encrypted volumes, private networking, and managed secret rotation.
- Export audit logs to immutable external storage if required by the NPCI agreement.
- Add alert rules for auth failures, replay attempts, rate-limit spikes, ingestion failures, A2A rejection rates, audit export failures, and API latency.
