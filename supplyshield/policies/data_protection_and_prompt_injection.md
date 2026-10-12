# Procurement Policy: Data Protection, Prompt Injection & Untrusted Input

## Policy ID: POL-SEC-004
**Effective Date:** 2026-01-01  
**Scope:** AI Agent Security, Input Sanitization, and Confidentiality

### 1. Untrusted Supplier Input
- Quotations, notes, shipment terms, and metadata supplied by vendors are classified as **Untrusted External Data**.
- All vendor commentary must pass through a sanitization filter to detect and neutralize prompt injection attempts (e.g., instructions such as "IGNORE PREVIOUS INSTRUCTIONS", "BYPASS SPENDING LIMIT", "APPROVE IMMEDIATELY WITHOUT HUMAN CHECK", or hidden system overrides).
- Any detected prompt injection attack must be quarantined, logged, and disarmed before passing text to the LLM.

### 2. Guardrail Independence
- Security guardrails, spending caps ($10,000 limit), and supplier eligibility checks must execute in deterministic code (Python/backend), never deferred to model discretion.
- An LLM output cannot override programmatic validation or authorize a bypass.

### 3. Log Confidentiality and Redaction
- All operational execution traces, audit logs, and diagnostic telemetry must redact confidential corporate information before storage or display.
- Redacted fields include:
  - Corporate payment tokens and banking account numbers (mask as `[REDACTED_ACCOUNT]`)
  - Proprietary discounted wholesale pricing formulas (`[REDACTED_MARGIN]`)
  - Employee personal identifiable info (PII) such as personal emails/phones (`[REDACTED_PII]`)
  - API keys and authorization secrets (`[REDACTED_SECRET]`)
