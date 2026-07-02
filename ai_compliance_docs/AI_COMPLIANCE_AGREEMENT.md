# AI Compliance Agreement — Operator Checklist

**Version**: 1.0  
**Classification**: CONFIDENTIAL — Do not commit without redacting sensitive fields.

---

> [!CAUTION]
> This document **must be completed and committed** by an authorized signatory before setting
> `EXTERNAL_AI_DATA_PROCESSING_APPROVED=true` in any environment.
> Failure to do so constitutes a violation of NPCI data handling obligations.

---

## Section 1 — Approval Record

| Field | Value |
|---|---|
| Approving Authority | *(Name and Title of authorized signatory)* |
| Legal Entity | *(Organization name)* |
| Approval Date | *(YYYY-MM-DD)* |
| Review Expiry | *(YYYY-MM-DD, typically 12 months from approval)* |

---

## Section 2 — Data Residency

### 2.1 Geographic Boundaries
All NPCI policy data processed by Gemini API must remain within the following geographic boundaries:

- **Required Region**: `[e.g., asia-south1 (Mumbai)]`
- **Google Cloud Region Configured**: `[Operator must verify]`
- **Cross-border transfer allowed**: `[ ] Yes  [ ] No`

If cross-border transfer is NOT allowed, a private/self-hosted inference solution (e.g., vLLM + Llama 3 on-premises) must be used instead.

### 2.2 Data Retention by Vendor
- Google Gemini API processes data **in-transit** and does NOT retain inputs for model training under the Enterprise API terms (as of 2024).
- Operator must verify this against the current Google Cloud Data Processing Addendum (DPA).
- **DPA Version Reviewed**: `[e.g., v3.0, 2024-01]`
- **DPA URL**: `[Link to current DPA]`

---

## Section 3 — PII Redaction Policy

### 3.1 Redaction Requirements
The following categories of data are PROHIBITED from being sent to Gemini API and MUST be stripped before any embedding, chat, or summarization call:

- [ ] User account numbers / UPI IDs
- [ ] NPCI transaction reference numbers
- [ ] Aadhaar / PAN numbers
- [ ] Bank account numbers / IFSC codes
- [ ] User names in combination with financial identifiers
- [ ] IP addresses (where linked to individuals)

### 3.2 Redaction Mechanism
- **Tool used**: `guardrails.py` PII detection layer
- **Operator must verify**: Guardrails are active (`GUARDRAILS_ENABLED=true`) before enabling external AI.

---

## Section 4 — Vendor Controls

| Control | Status |
|---|---|
| Google Cloud DPA signed | `[ ] Yes  [ ] No` |
| Data Processing Addendum (GDPR) applicable | `[ ] Yes  [ ] No` |
| Model training opt-out confirmed | `[ ] Yes  [ ] No` |
| Incident notification SLA reviewed (72h) | `[ ] Yes  [ ] No` |
| Sub-processor list reviewed | `[ ] Yes  [ ] No` |

---

## Section 5 — Signatory Declaration

> I, the undersigned, confirm that the above prerequisites have been met, and I authorize the use of
> Google Gemini API for processing NPCI policy data under the conditions specified above.

**Signature**: _________________________________  
**Name**: _________________________________  
**Title**: _________________________________  
**Date**: _________________________________

---

## Section 6 — Technical Prerequisites Checklist

Before enabling `EXTERNAL_AI_DATA_PROCESSING_APPROVED=true`:

- [ ] `GUARDRAILS_ENABLED=true` is set
- [ ] PII redaction is verified via `pytest tests/test_guardrails.py`
- [ ] Google Cloud project is configured in the approved region
- [ ] API key is scoped to the minimum required permissions
- [ ] `GOOGLE_API_KEY` is stored in a secrets manager, not in `.env` files
- [ ] Audit logging is confirmed active (`AUDIT_LOG_ENABLED=true`)
- [ ] This document has been signed and committed to the repository
