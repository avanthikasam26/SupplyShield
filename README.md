# SupplyShield — Autonomous AI Procurement & FinanceCheck Agent

**SupplyShield** is an autonomous AI agent engineered to safely handle factory inventory shortages and verify purchase-to-payment bills. When an essential manufacturing part runs out of stock, the agent triages the shortage, identifies alternative suppliers, calculates total landed costs, retrieves applicable procurement policies via RAG, enforces strict safety guardrails, and either requests human approval or simulates an authorized purchase order. 

Through **SupplyShield FinanceCheck**, the platform also performs automated 3-way matching between Purchase Orders, Loading Dock Goods Receipts, Supplier Invoices, and Payment records to protect factory cash flows.

---

## 1. LLM Identification & Reasoning Architecture

Per architectural inspection:
- **Provider:** Google Gemini
- **Exact Model Name:** `gemini-3.8-flash`
- **SDK & Package:** `@google/genai` (v2.4.0 TypeScript SDK)
- **Where It Is Called:** Server-side proxy (`/api/agent/investigate` in `server.ts`). The model generates concise, plain-English operational summaries and board explanations.
- **Deterministic Independence:** All procurement rules (spending limits, supplier qualifications, prompt injection defense, and 3-way invoice matching) execute **independently in deterministic Python code**. Model outputs can never bypass programmatic guardrails.
- **Offline Fallback:** Active. When running without an API key or offline, SupplyShield executes deterministic rule-based reasoning without interruption.
- **Secret Protection:** API keys are never exposed in browser bundles or client-side telemetry.

---

## 2. Four Simplified Actions for Factory Operations

1. **Check Low Stock:** Live inventory view across 15 factory parts with clear stock statuses (Healthy, Low Stock, or Critical Stockout) and single-click restock actions.
2. **Find Suppliers:** Compares quotes across pre-approved backup suppliers, estimates landed costs, checks spending caps, and creates simulated orders.
3. **Receive Goods:** Allows loading dock officers to log physical deliveries, record damaged or missing goods, and instantly add good units to warehouse inventory.
4. **Check Bills & Payments (SupplyShield FinanceCheck):** 3-way invoice verification matching purchase orders, dock receipts, vendor invoices, and payments. Detects duplicate invoices, price hikes, missing parts, and payment mismatches.

---

## 3. SupplyShield FinanceCheck: Purchase-to-Payment Verification

The FinanceCheck module automates financial reconciliation using SQLite:

- **Matched:** All quantities, unit prices, delivery receipts, and payments align 100% with the purchase order.
- **Needs Review:** Discrepancy detected (e.g., supplier billed for 36 units, but warehouse dock only received 26 units; or invoiced unit price is $95.00 vs $85.00 agreed PO price).
- **Partially Paid:** Tracks installment payments and calculates outstanding balance due.
- **Payment Mismatch:** Flags overpayments or unmapped transaction totals.
- **Human Resolution:** Authorized managers can review discrepancies, record explanations (e.g., credit notes, contract adjustments), and preserve an immutable audit trail in `finance_audit_logs`.

---

## 4. Architecture & The 8 Agent Engineering Topics

| # | Topic | Implementation in SupplyShield |
|---|---|---|
| **1** | **Harness** | Explicit finite-state machine (`TRIAGE` → `ASSESS_IMPACT` → `FIND_ALTERNATIVES` → `VALIDATE` → `APPROVAL_REQUIRED` / `READY_TO_ORDER` → `COMPLETE`). |
| **2** | **Loop Engineering** | Hard ceiling of **5 planning steps**, duplicate-tool-call detection to prevent recursion loops, and bounded exponential backoff retries (max 2 attempts). |
| **3** | **Tool Calls** | Modular Python tools: `check_inventory`, `find_backup_suppliers`, `calculate_costs`, `retrieve_procurement_policies`, and `create_simulated_purchase_order`. |
| **4** | **RAG** | Keyword-scoring policy retriever scanning local Markdown policy files (`spending_limits.md`, `supplier_qualification.md`, `data_protection_and_prompt_injection.md`) and generating audit citations. |
| **5** | **Guardrails** | Deterministic Python rules independent of the LLM: **$10,000 automatic spend cap**, mandatory `APPROVED` supplier qualification, untrusted vendor input sanitization, prompt injection defusal, and log redaction. |
| **6** | **Golden Set** | 6 predefined test scenarios with explicit expected state transitions and safety assertions. |
| **7** | **Evals** | Automated evaluation suite (`supplyshield/evals.py`) executing all 6 scenarios, achieving a **100% pass rate** in ~120ms. |
| **8** | **Observability** | Real-time telemetry tracking execution latency (ms), token consumption, estimated USD inference cost, tool invocation traces, and sanitized execution logs. |

---

## 5. Directory Structure

```
├── README.md                      # Beginner-friendly setup and architecture guide
├── requirements.txt               # Python package dependencies
├── run_tests.py                   # Zero-dependency test runner (python3 run_tests.py)
├── streamlit_app.py               # Streamlit single-page dashboard
├── server.ts                      # Express server mounting API routes & Vite middleware
├── supplyshield.db                # SQLite database with 15 items & 5 suppliers
├── supplyshield/
│   ├── __init__.py
│   ├── agent.py                   # State machine harness & loop engineering
│   ├── cli.py                     # CLI bridge connecting Express API to Python agent
│   ├── db.py                      # Database schema and realistic seed data
│   ├── evals.py                   # Automated Golden Set evaluation runner
│   ├── finance.py                 # SupplyShield FinanceCheck 3-way matching module
│   ├── golden_set.py              # Predefined golden scenario definitions
│   ├── guardrails.py              # Deterministic safety guards & prompt injection defense
│   ├── tools.py                   # Discrete procurement tools & RAG engine
│   └── policies/                  # Procurement policies in Markdown
│       ├── spending_limits.md
│       ├── supplier_qualification.md
│       ├── lead_time_and_stock.md
│       └── data_protection_and_prompt_injection.md
├── tests/
│   └── test_supplyshield.py       # Automated test suite (17 tests)
├── src/
│   ├── App.tsx                    # Simplified 4-action frontend with FinanceCheck
│   └── main.tsx
└── package.json
```

---

## 6. Verification & Automated Testing

Run the test suite covering both procurement agent guardrails and FinanceCheck matching:
```bash
python3 run_tests.py
```
*(Runs 17 unit and integration tests verifying fully matched bills, missing goods detection, price discrepancies, duplicate invoice blocking, partial payments, and payment mismatches).*

Run the automated Golden Set benchmark (6 scenarios):
```bash
python3 -m supplyshield.evals
```

Launch the full-stack web application:
```bash
npm run dev
```
Open [http://localhost:3000](http://localhost:3000) in your browser.
