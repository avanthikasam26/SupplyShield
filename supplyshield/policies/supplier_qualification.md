# Procurement Policy: Supplier Qualification & Eligibility

## Policy ID: POL-SUP-002
**Effective Date:** 2026-01-01  
**Scope:** Factory Backup Supplier Selection and Vetting

### 1. Approved Suppliers
- Autonomous replenishment orders may only be issued to vendors with an explicit status of **`APPROVED`**.
- Approved vendors must have passed quality audit, safety verification, and ISO-9001 factory compliance.

### 2. Prohibited and Unapproved Vendors
- Vendors designated as **`PROHIBITED`**, **`SUSPENDED`**, or **`UNAPPROVED`** are strictly forbidden from receiving automated purchase requests.
- Even if an unapproved vendor offers lower prices or faster lead times, the agent must reject the quote during the `VALIDATE` stage.
- Prohibited vendor quotes must be flagged with a non-compliance reason in the agent execution audit trail.

### 3. Backup Supplier Ranking Priority
- Preference is given to approved suppliers with:
  1. Guaranteed delivery within standard tolerance (<= 7 business days)
  2. Proven reliability score >= 90%
  3. Lowest total landed cost (item cost + shipping)
