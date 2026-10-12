"""SupplyShield FinanceCheck: Purchase-to-Payment Verification Module.

Implements high-accuracy deterministic 3-way matching (PO ↔ Goods Receipt ↔ Invoice ↔ Payment)
using Python Decimal arithmetic, explicit rounding tolerances, duplicate detection,
human correction audits, and verification checklist validation for PDF report generation.
"""

import sqlite3
import re
import uuid
import json
import datetime
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from typing import Dict, List, Any, Optional, Tuple

from .db import get_connection, DB_PATH
from .guardrails import redact_log

ROUNDING_TOLERANCE = Decimal("0.02")

def to_decimal(val: Any) -> Decimal:
    """Safely convert any numeric value to Decimal."""
    if isinstance(val, Decimal):
        return val
    if val is None or val == "":
        return Decimal("0.00")
    try:
        clean = str(val).replace(",", "").replace("$", "").strip()
        return Decimal(clean)
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0.00")

def quantize_money(val: Any) -> Decimal:
    """Format and round to 2 decimal places using standard accounting ROUND_HALF_UP."""
    return to_decimal(val).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

def init_finance_tables(db_path: str = DB_PATH) -> None:
    """Create FinanceCheck tables and ensure required columns exist."""
    conn = get_connection(db_path)
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS goods_receipts (
        id TEXT PRIMARY KEY,
        po_id TEXT NOT NULL,
        received_date TEXT NOT NULL,
        received_by TEXT NOT NULL,
        quantity_received INTEGER NOT NULL,
        quantity_damaged INTEGER NOT NULL DEFAULT 0,
        quantity_missing INTEGER NOT NULL DEFAULT 0,
        condition_notes TEXT,
        batch_number TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY (po_id) REFERENCES purchase_orders (id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS supplier_invoices (
        id TEXT PRIMARY KEY,
        invoice_number TEXT NOT NULL,
        po_id TEXT NOT NULL,
        supplier_id TEXT NOT NULL,
        invoice_date TEXT NOT NULL,
        invoiced_quantity INTEGER NOT NULL,
        unit_price REAL NOT NULL,
        tax_amount REAL NOT NULL DEFAULT 0.0,
        shipping_amount REAL NOT NULL DEFAULT 0.0,
        discount_amount REAL NOT NULL DEFAULT 0.0,
        total_amount REAL NOT NULL,
        file_name TEXT,
        raw_content TEXT,
        extraction_confidence REAL NOT NULL DEFAULT 1.0,
        status TEXT NOT NULL, -- MATCHED, NEEDS_REVIEW, PARTIALLY_PAID, PAYMENT_MISMATCH, RESOLVED, REJECTED
        discrepancy_reasons TEXT, -- JSON array of strings
        plain_english_explanation TEXT,
        resolved_by TEXT,
        resolution_notes TEXT,
        resolved_at TEXT,
        original_extracted_data TEXT,
        is_manually_corrected INTEGER NOT NULL DEFAULT 0,
        current_report_id TEXT,
        needs_reverification INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        FOREIGN KEY (po_id) REFERENCES purchase_orders (id),
        FOREIGN KEY (supplier_id) REFERENCES suppliers (id)
    )
    """)

    # Ensure added columns exist if table was already created
    cur.execute("PRAGMA table_info(supplier_invoices)")
    existing_cols = [r[1] for r in cur.fetchall()]
    migration_cols = [
        ("discount_amount", "REAL NOT NULL DEFAULT 0.0"),
        ("original_extracted_data", "TEXT"),
        ("is_manually_corrected", "INTEGER NOT NULL DEFAULT 0"),
        ("current_report_id", "TEXT"),
        ("needs_reverification", "INTEGER NOT NULL DEFAULT 0"),
    ]
    for col_name, col_def in migration_cols:
        if col_name not in existing_cols:
            try:
                cur.execute(f"ALTER TABLE supplier_invoices ADD COLUMN {col_name} {col_def}")
            except sqlite3.OperationalError:
                pass

    cur.execute("""
    CREATE TABLE IF NOT EXISTS invoice_payments (
        id TEXT PRIMARY KEY,
        invoice_id TEXT NOT NULL,
        po_id TEXT NOT NULL,
        payment_date TEXT NOT NULL,
        amount_paid REAL NOT NULL,
        payment_method TEXT NOT NULL, -- ACH, Wire, Check, Corporate Card
        reference_number TEXT NOT NULL,
        notes TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY (invoice_id) REFERENCES supplier_invoices (id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS finance_audit_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_type TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        action TEXT NOT NULL,
        details TEXT NOT NULL,
        user_name TEXT NOT NULL,
        timestamp TEXT NOT NULL
    )
    """)

    conn.commit()
    conn.close()

    from .reports import init_reports_table
    init_reports_table(db_path)

def log_finance_audit(
    entity_type: str, 
    entity_id: str, 
    action: str, 
    details: str, 
    user_name: str = "System", 
    db_path: str = DB_PATH
) -> None:
    """Record immutable finance audit trail."""
    conn = get_connection(db_path)
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO finance_audit_logs (entity_type, entity_id, action, details, user_name, timestamp)
    VALUES (?, ?, ?, ?, ?, ?)
    """, (
        entity_type, entity_id, action, details, user_name,
        datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ))
    conn.commit()
    conn.close()

def record_goods_receipt(
    po_id: str,
    quantity_received: int,
    quantity_damaged: int = 0,
    quantity_missing: int = 0,
    received_by: str = "Warehouse Dock Team",
    condition_notes: str = "",
    batch_number: str = "",
    db_path: str = DB_PATH
) -> Dict[str, Any]:
    """Record physical delivery at factory loading dock and adjust stock."""
    init_finance_tables(db_path)
    conn = get_connection(db_path)
    cur = conn.cursor()

    cur.execute("SELECT * FROM purchase_orders WHERE id = ? OR order_reference = ?", (po_id, po_id))
    po_row = cur.fetchone()
    if not po_row:
        conn.close()
        return {"success": False, "error": f"Purchase order '{po_id}' not found."}

    po = dict(po_row)
    gr_id = f"GR-{datetime.datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    cur.execute("""
    INSERT INTO goods_receipts 
    (id, po_id, received_date, received_by, quantity_received, quantity_damaged, quantity_missing, condition_notes, batch_number, created_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        gr_id, po["id"], now_str[:10], received_by, quantity_received,
        quantity_damaged, quantity_missing, condition_notes, batch_number, now_str
    ))

    # Usable good parts are added to factory inventory stock
    usable_quantity = max(0, quantity_received - quantity_damaged)
    if usable_quantity > 0:
        cur.execute("""
        UPDATE inventory_items 
        SET current_stock = current_stock + ? 
        WHERE id = ?
        """, (usable_quantity, po["item_id"]))

    # Update PO status
    new_po_status = "DELIVERED" if (quantity_received >= po["quantity"]) else "PARTIALLY_RECEIVED"
    cur.execute("UPDATE purchase_orders SET status = ? WHERE id = ?", (new_po_status, po["id"]))

    # If an invoice exists for this PO, flag for re-verification
    cur.execute("UPDATE supplier_invoices SET needs_reverification = 1 WHERE po_id = ?", (po["id"],))

    conn.commit()
    conn.close()

    log_finance_audit(
        "GOODS_RECEIPT", gr_id, "RECEIVE_DELIVERY",
        f"Received {quantity_received} units for {po['id']} (Damaged: {quantity_damaged}, Missing: {quantity_missing}). Stock increased by {usable_quantity}.",
        received_by, db_path
    )

    return {
        "success": True,
        "receipt_id": gr_id,
        "po_id": po["id"],
        "item_id": po["item_id"],
        "quantity_received": quantity_received,
        "usable_quantity_added": usable_quantity,
        "quantity_damaged": quantity_damaged,
        "quantity_missing": quantity_missing,
        "po_status": new_po_status
    }

def extract_invoice_document(
    file_name: str,
    raw_text: str = ""
) -> Dict[str, Any]:
    """Deterministic document extraction parser with field-level confidence evaluation."""
    confidence = Decimal("1.00")
    text = raw_text or ""

    IGNORE_WORDS = {
        "and", "or", "to", "for", "the", "a", "an", "is", "of", "in", "with",
        "vendor", "supplier", "date", "total", "subtotal", "tax", "items", "item",
        "quantities", "quantity", "details", "line", "summary", "notes", "missing",
        "scan", "page", "due", "amount", "bill", "invoice", "receipt", "customer",
        "incomplete", "statement"
    }

    # Invoice number regex
    inv_match = re.search(r"(?i)\binvoice\s*(?:#|no\.?|num\.?|number|id)?[:\s#]*([A-Z0-9\-_]+)", text)
    invoice_number = ""
    if inv_match:
        val = inv_match.group(1).strip()
        matched_str = inv_match.group(0)
        if val.lower() not in IGNORE_WORDS:
            if re.search(r"[\d\-]", val) or ":" in matched_str or "#" in matched_str or "no" in matched_str.lower():
                invoice_number = val

    # PO reference regex
    po_match = re.search(r"(?i)\b(?:po|purchase\s*order)\s*(?:#|ref\.?|no\.?|number|id)?[:\s#]*([A-Z0-9\-_]+)", text)
    po_reference = ""
    if po_match:
        val = po_match.group(1).strip()
        matched_str = po_match.group(0)
        if val.lower() not in IGNORE_WORDS:
            if re.search(r"[\d\-]", val) or ":" in matched_str or "#" in matched_str or "ref" in matched_str.lower() or "no" in matched_str.lower():
                po_reference = val

    # Quantity regex
    qty_match = re.search(r"(?i)(?:quantity|qty|units?)[:\s]+(\d+)", text)
    quantity = int(qty_match.group(1)) if qty_match else 0

    # Unit price regex
    price_match = re.search(r"(?i)(?:unit\s*price|rate|price/unit)[:\s]+\$?([0-9,]+(?:\.\d{2})?)", text)
    unit_price = quantize_money(price_match.group(1)) if price_match else Decimal("0.00")

    # Shipping regex
    ship_match = re.search(r"(?i)(?:shipping|freight)[:\s]+\$?([0-9,]+(?:\.\d{2})?)", text)
    shipping = quantize_money(ship_match.group(1)) if ship_match else Decimal("0.00")

    # Tax regex
    tax_match = re.search(r"(?i)(?:tax|vat)[:\s]+\$?([0-9,]+(?:\.\d{2})?)", text)
    tax = quantize_money(tax_match.group(1)) if tax_match else Decimal("0.00")

    # Discount regex
    disc_match = re.search(r"(?i)(?:discount|rebate)[:\s]+\$?([0-9,]+(?:\.\d{2})?)", text)
    discount = quantize_money(disc_match.group(1)) if disc_match else Decimal("0.00")

    # Total regex
    total_match = re.search(r"(?i)(?:total|amount\s*due|grand\s*total)[:\s]+\$?([0-9,]+(?:\.\d{2})?)", text)
    total = quantize_money(total_match.group(1)) if total_match else Decimal("0.00")

    # Missing fields assessment
    missing_fields = []
    if not invoice_number:
        missing_fields.append("invoice_number")
        confidence -= Decimal("0.25")
    if not po_reference:
        missing_fields.append("po_reference")
        confidence -= Decimal("0.25")
    if quantity <= 0:
        missing_fields.append("quantity")
        confidence -= Decimal("0.20")
    if unit_price <= Decimal("0.00"):
        missing_fields.append("unit_price")
        confidence -= Decimal("0.15")
    if total <= Decimal("0.00"):
        sub = quantize_money(Decimal(quantity) * unit_price)
        total = quantize_money(sub + shipping + tax - discount)
        if total <= Decimal("0.00"):
            missing_fields.append("total")
            confidence -= Decimal("0.15")

    conf_float = max(0.1, float(confidence))

    return {
        "success": conf_float >= 0.70 and len(missing_fields) == 0,
        "file_name": file_name,
        "invoice_number": invoice_number,
        "po_reference": po_reference,
        "quantity": quantity,
        "unit_price": float(unit_price),
        "shipping_amount": float(shipping),
        "tax_amount": float(tax),
        "discount_amount": float(discount),
        "total_amount": float(total),
        "confidence": conf_float,
        "missing_fields": missing_fields,
        "raw_text_length": len(text)
    }

def verify_three_way_match(
    invoice_number: str,
    po_id: str,
    supplier_id: str,
    invoiced_quantity: int,
    unit_price: Any,
    total_amount: Any,
    tax_amount: Any = 0.0,
    shipping_amount: Any = 0.0,
    discount_amount: Any = 0.0,
    file_name: str = "manual_entry.pdf",
    raw_content: str = "",
    extraction_confidence: float = 1.0,
    existing_invoice_id: Optional[str] = None,
    db_path: str = DB_PATH
) -> Dict[str, Any]:
    """Execute high-accuracy deterministic 3-way matching using Decimal arithmetic."""
    init_finance_tables(db_path)
    conn = get_connection(db_path)
    cur = conn.cursor()

    # Convert all monetary inputs to Decimal
    unit_price_dec = quantize_money(unit_price)
    shipping_dec = quantize_money(shipping_amount)
    tax_dec = quantize_money(tax_amount)
    discount_dec = quantize_money(discount_amount)
    total_amount_dec = quantize_money(total_amount)
    invoiced_qty_dec = Decimal(invoiced_quantity)

    # 1. Look up Purchase Order
    cur.execute("SELECT * FROM purchase_orders WHERE id = ? OR order_reference = ?", (po_id, po_id))
    po_row = cur.fetchone()
    if not po_row:
        conn.close()
        return {
            "success": False,
            "status": "NEEDS_REVIEW",
            "discrepancies": [f"Referenced purchase order '{po_id}' does not exist in company records."],
            "plain_english_explanation": f"Purchase Order {po_id} could not be found. Please verify the PO number."
        }

    po = dict(po_row)
    actual_po_id = po["id"]
    po_unit_cost_dec = quantize_money(po["unit_cost"])
    po_qty = po["quantity"]

    # 2. Duplicate Invoice Detection
    cur.execute("""
    SELECT id, invoice_number, status, total_amount FROM supplier_invoices 
    WHERE invoice_number = ? AND supplier_id = ?
    """, (invoice_number, supplier_id))
    dup_rows = [dict(r) for r in cur.fetchall()]
    
    # If a record already exists with same number and supplier, but different ID:
    is_duplicate = False
    for d in dup_rows:
        if existing_invoice_id is None or d["id"] != existing_invoice_id:
            is_duplicate = True
            break

    if is_duplicate:
        conn.close()
        return {
            "success": False,
            "status": "NEEDS_REVIEW",
            "discrepancies": [f"Duplicate invoice detected: Invoice '{invoice_number}' already exists in database for supplier '{supplier_id}'."],
            "plain_english_explanation": f"Duplicate invoice blocked. Bill #{invoice_number} from this supplier was already entered previously."
        }

    # 3. Look up Goods Receipts for this PO
    cur.execute("""
    SELECT 
        COALESCE(SUM(quantity_received), 0) as total_received,
        COALESCE(SUM(quantity_damaged), 0) as total_damaged,
        COALESCE(SUM(quantity_missing), 0) as total_missing
    FROM goods_receipts 
    WHERE po_id = ?
    """, (actual_po_id,))
    gr_totals = dict(cur.fetchone())
    total_received = gr_totals["total_received"]
    total_damaged = gr_totals["total_damaged"]
    total_missing = gr_totals["total_missing"]

    # 4. Check Payments already made for this invoice
    inv_id = existing_invoice_id or f"INV-{uuid.uuid4().hex[:8].upper()}"
    cur.execute("""
    SELECT COALESCE(SUM(amount_paid), 0.0) as total_paid 
    FROM invoice_payments 
    WHERE invoice_id = ?
    """, (inv_id,))
    total_paid_dec = quantize_money(cur.fetchone()["total_paid"])

    # Discrepancy Evaluation
    discrepancies = []

    # Check A: Supplier Match
    if po["supplier_id"] != supplier_id:
        discrepancies.append(f"Supplier mismatch: Invoice submitted by '{supplier_id}', but PO was issued to '{po['supplier_id']}'.")

    # Check B: Unit Price Match (within ROUNDING_TOLERANCE)
    price_diff = quantize_money(unit_price_dec - po_unit_cost_dec)
    if abs(price_diff) > ROUNDING_TOLERANCE:
        if price_diff > Decimal("0.00"):
            discrepancies.append(f"Price hike mismatch: Invoiced unit price is ${unit_price_dec:,.2f}, but approved PO price was ${po_unit_cost_dec:,.2f} (+${price_diff:,.2f}/unit).")
        else:
            discrepancies.append(f"Price discount variance: Invoiced price (${unit_price_dec:,.2f}) is lower than PO price (${po_unit_cost_dec:,.2f}).")

    # Check C: Quantity Received vs Invoiced
    if total_received == 0:
        discrepancies.append(f"No delivery recorded: Factory loading dock has not recorded any goods received for PO {po['id']}.")
    elif invoiced_quantity > total_received:
        missing_count = invoiced_quantity - total_received
        discrepancies.append(f"Missing goods discrepancy: Supplier invoiced {invoiced_quantity} units, but warehouse dock only received {total_received} units ({missing_count} units missing).")
    
    if total_damaged > 0:
        discrepancies.append(f"Damaged goods alert: Warehouse dock flagged {total_damaged} damaged units upon delivery.")

    # Check D: Math reconciliation using Decimal
    subtotal_dec = quantize_money(invoiced_qty_dec * unit_price_dec)
    expected_calc_total = quantize_money(subtotal_dec + shipping_dec + tax_dec - discount_dec)
    math_diff = abs(expected_calc_total - total_amount_dec)
    if math_diff > ROUNDING_TOLERANCE:
        discrepancies.append(f"Total arithmetic error: Calculated subtotal+tax (${expected_calc_total:,.2f}) does not match stated invoice total (${total_amount_dec:,.2f}). Difference: ${math_diff:,.2f}.")

    # Check E: Document Extraction Confidence
    if extraction_confidence < 0.70:
        discrepancies.append(f"Uncertain document extraction: Confidence is {extraction_confidence*100:.0f}%. Manual human verification required.")

    # Determine Status
    balance_due_dec = quantize_money(total_amount_dec - total_paid_dec)
    status = "MATCHED"
    plain_english = "All quantities, unit prices, delivery receipts, and payments align 100% with the purchase order."

    if discrepancies:
        status = "NEEDS_REVIEW"
        plain_english = "Review required: " + "; ".join(discrepancies)
    elif total_paid_dec > Decimal("0.00"):
        if balance_due_dec > ROUNDING_TOLERANCE:
            status = "PARTIALLY_PAID"
            plain_english = f"Partially Paid: ${total_paid_dec:,.2f} recorded of ${total_amount_dec:,.2f}. Outstanding balance: ${balance_due_dec:,.2f}."
        elif balance_due_dec < -ROUNDING_TOLERANCE:
            status = "PAYMENT_MISMATCH"
            plain_english = f"Payment Mismatch: Recorded payments (${total_paid_dec:,.2f}) exceed invoice total (${total_amount_dec:,.2f}) by ${abs(balance_due_dec):,.2f} (Overpayment)."
        else:
            status = "MATCHED"
            plain_english = f"Fully Paid: All quantities, prices, and payments (${total_paid_dec:,.2f}) reconciled 100% with purchase order."

    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Store or update in database
    if existing_invoice_id:
        cur.execute("""
        UPDATE supplier_invoices 
        SET invoice_number = ?, invoiced_quantity = ?, unit_price = ?, tax_amount = ?, shipping_amount = ?, discount_amount = ?, total_amount = ?, status = ?, discrepancy_reasons = ?, plain_english_explanation = ?, needs_reverification = 1
        WHERE id = ?
        """, (
            invoice_number, invoiced_quantity, float(unit_price_dec), float(tax_dec),
            float(shipping_dec), float(discount_dec), float(total_amount_dec),
            status, json.dumps(discrepancies), plain_english, existing_invoice_id
        ))
    else:
        # Preserve original extracted data snapshot
        orig_data = json.dumps({
            "invoiced_quantity": invoiced_quantity,
            "unit_price": float(unit_price_dec),
            "tax_amount": float(tax_dec),
            "shipping_amount": float(shipping_dec),
            "discount_amount": float(discount_dec),
            "total_amount": float(total_amount_dec)
        })
        cur.execute("""
        INSERT INTO supplier_invoices 
        (id, invoice_number, po_id, supplier_id, invoice_date, invoiced_quantity, unit_price, tax_amount, shipping_amount, discount_amount, total_amount, file_name, raw_content, extraction_confidence, status, discrepancy_reasons, plain_english_explanation, original_extracted_data, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            inv_id, invoice_number, actual_po_id, supplier_id, now_str[:10],
            invoiced_quantity, float(unit_price_dec), float(tax_dec), float(shipping_dec),
            float(discount_dec), float(total_amount_dec), file_name, raw_content,
            extraction_confidence, status, json.dumps(discrepancies), plain_english,
            orig_data, now_str
        ))

    conn.commit()
    conn.close()

    log_finance_audit(
        "INVOICE", inv_id, "VERIFY_3WAY_MATCH",
        f"Invoice {invoice_number} checked against PO {actual_po_id}. Status: {status}. Total: ${total_amount_dec:,.2f}. Discrepancies count: {len(discrepancies)}.",
        "FinanceCheck Engine", db_path
    )

    return {
        "success": True,
        "invoice_id": inv_id,
        "invoice_number": invoice_number,
        "po_id": actual_po_id,
        "supplier_id": supplier_id,
        "status": status,
        "invoiced_quantity": invoiced_quantity,
        "received_quantity": total_received,
        "damaged_quantity": total_damaged,
        "unit_price": float(unit_price_dec),
        "po_unit_cost": float(po_unit_cost_dec),
        "total_amount": float(total_amount_dec),
        "total_paid": float(total_paid_dec),
        "balance_due": float(max(Decimal("0.00"), balance_due_dec)),
        "discrepancies": discrepancies,
        "plain_english_explanation": plain_english,
        "extraction_confidence": extraction_confidence
    }

def record_invoice_payment(
    invoice_id: str,
    amount_paid: Any,
    payment_method: str = "ACH",
    reference_number: str = "",
    notes: str = "",
    db_path: str = DB_PATH
) -> Dict[str, Any]:
    """Record an authorized payment and update balance status."""
    init_finance_tables(db_path)
    conn = get_connection(db_path)
    cur = conn.cursor()

    cur.execute("SELECT * FROM supplier_invoices WHERE id = ? OR invoice_number = ?", (invoice_id, invoice_id))
    inv_row = cur.fetchone()
    if not inv_row:
        conn.close()
        return {"success": False, "error": f"Invoice '{invoice_id}' not found."}

    inv = dict(inv_row)
    amount_dec = quantize_money(amount_paid)

    pay_id = f"PAY-{datetime.datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ref_no = reference_number or f"REF-{uuid.uuid4().hex[:8].upper()}"

    cur.execute("""
    INSERT INTO invoice_payments 
    (id, invoice_id, po_id, payment_date, amount_paid, payment_method, reference_number, notes, created_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        pay_id, inv["id"], inv["po_id"], now_str[:10], float(amount_dec),
        payment_method, ref_no, notes, now_str
    ))

    # Recalculate total payments for this invoice
    cur.execute("SELECT COALESCE(SUM(amount_paid), 0.0) as total_paid FROM invoice_payments WHERE invoice_id = ?", (inv["id"],))
    new_total_paid_dec = quantize_money(cur.fetchone()["total_paid"])
    total_amount_dec = quantize_money(inv["total_amount"])
    balance_dec = quantize_money(total_amount_dec - new_total_paid_dec)

    # Check review flags: do NOT overwrite if blocked by quantity or pricing mismatch!
    current_status = inv["status"]
    if current_status not in ["NEEDS_REVIEW", "REJECTED"]:
        if abs(balance_dec) <= ROUNDING_TOLERANCE:
            new_status = "MATCHED"
            new_expl = f"PAID IN FULL: All quantities, prices, and payments (${new_total_paid_dec:,.2f}) reconciled 100%."
        elif balance_dec > ROUNDING_TOLERANCE:
            new_status = "PARTIALLY_PAID"
            new_expl = f"Partially Paid: ${new_total_paid_dec:,.2f} recorded of ${total_amount_dec:,.2f}. Outstanding balance: ${balance_dec:,.2f}."
        else:
            new_status = "PAYMENT_MISMATCH"
            new_expl = f"Payment Mismatch: Recorded payments (${new_total_paid_dec:,.2f}) exceed invoice total (${total_amount_dec:,.2f}) by ${abs(balance_dec):,.2f} (Overpayment)."
        
        cur.execute("UPDATE supplier_invoices SET status = ?, plain_english_explanation = ?, needs_reverification = 1 WHERE id = ?", (new_status, new_expl, inv["id"]))

    conn.commit()
    conn.close()

    log_finance_audit(
        "PAYMENT", pay_id, "RECORD_PAYMENT",
        f"Recorded {payment_method} payment of ${amount_dec:,.2f} for invoice {inv['invoice_number']} (Ref: {ref_no}). Remaining balance: ${balance_dec:,.2f}.",
        "Finance Operations", db_path
    )

    return {
        "success": True,
        "payment_id": pay_id,
        "invoice_id": inv["id"],
        "amount_paid": float(amount_dec),
        "total_paid_to_date": float(new_total_paid_dec),
        "balance_remaining": float(balance_dec),
        "reference_number": ref_no
    }

def correct_invoice_data(
    invoice_id: str,
    corrected_by: str,
    corrected_values: Dict[str, Any],
    notes: str = "",
    db_path: str = DB_PATH
) -> Dict[str, Any]:
    """Allows human reviewers to correct extracted values while retaining original data and audit trail."""
    init_finance_tables(db_path)
    conn = get_connection(db_path)
    cur = conn.cursor()

    cur.execute("SELECT * FROM supplier_invoices WHERE id = ? OR invoice_number = ?", (invoice_id, invoice_id))
    inv_row = cur.fetchone()
    if not inv_row:
        conn.close()
        return {"success": False, "error": f"Invoice '{invoice_id}' not found."}

    inv = dict(inv_row)
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # If original extracted data was not saved yet, save current values as original
    orig_data_str = inv.get("original_extracted_data")
    if not orig_data_str:
        orig_data_str = json.dumps({
            "invoiced_quantity": inv["invoiced_quantity"],
            "unit_price": inv["unit_price"],
            "tax_amount": inv.get("tax_amount", 0.0),
            "shipping_amount": inv.get("shipping_amount", 0.0),
            "discount_amount": inv.get("discount_amount", 0.0),
            "total_amount": inv["total_amount"]
        })

    # Prepare corrected values
    new_qty = int(corrected_values.get("invoiced_quantity", inv["invoiced_quantity"]))
    new_price = quantize_money(corrected_values.get("unit_price", inv["unit_price"]))
    new_ship = quantize_money(corrected_values.get("shipping_amount", inv.get("shipping_amount", 0.0)))
    new_tax = quantize_money(corrected_values.get("tax_amount", inv.get("tax_amount", 0.0)))
    new_disc = quantize_money(corrected_values.get("discount_amount", inv.get("discount_amount", 0.0)))
    new_total = quantize_money(corrected_values.get("total_amount", inv["total_amount"]))

    # Invalidate existing report if one was generated
    if inv.get("current_report_id"):
        cur.execute("""
        UPDATE verification_reports 
        SET status = 'SUPERSEDED_BY_REVISION' 
        WHERE invoice_id = ? AND status = 'VALID'
        """, (inv["id"],))

    cur.execute("""
    UPDATE supplier_invoices 
    SET invoiced_quantity = ?, unit_price = ?, shipping_amount = ?, tax_amount = ?, discount_amount = ?, total_amount = ?, original_extracted_data = ?, is_manually_corrected = 1, needs_reverification = 1, resolution_notes = ?
    WHERE id = ?
    """, (
        new_qty, float(new_price), float(new_ship), float(new_tax), float(new_disc),
        float(new_total), orig_data_str, f"Corrected by {corrected_by}: {notes}", inv["id"]
    ))
    conn.commit()
    conn.close()

    log_finance_audit(
        "INVOICE", inv["id"], "CORRECT_EXTRACTION",
        f"Corrected extracted values by {corrected_by}. Notes: {notes}. Previous values archived.",
        corrected_by, db_path
    )

    # Re-verify with corrected values
    return verify_three_way_match(
        invoice_number=inv["invoice_number"],
        po_id=inv["po_id"],
        supplier_id=inv["supplier_id"],
        invoiced_quantity=new_qty,
        unit_price=new_price,
        total_amount=new_total,
        tax_amount=new_tax,
        shipping_amount=new_ship,
        discount_amount=new_disc,
        file_name=inv.get("file_name", "corrected.pdf"),
        extraction_confidence=1.0,
        existing_invoice_id=inv["id"],
        db_path=db_path
    )

def resolve_invoice_discrepancy(
    invoice_id: str,
    resolved_by: str,
    resolution_notes: str,
    resolution_action: str = "ACCEPT_WITH_NOTE",
    db_path: str = DB_PATH
) -> Dict[str, Any]:
    """Human-in-the-loop resolution of a discrepancy with audit preservation."""
    init_finance_tables(db_path)
    conn = get_connection(db_path)
    cur = conn.cursor()

    cur.execute("SELECT * FROM supplier_invoices WHERE id = ? OR invoice_number = ?", (invoice_id, invoice_id))
    inv_row = cur.fetchone()
    if not inv_row:
        conn.close()
        return {"success": False, "error": f"Invoice '{invoice_id}' not found."}

    inv = dict(inv_row)
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    new_status = "RESOLVED" if resolution_action != "REJECT" else "REJECTED"
    new_expl = f"Discrepancy resolved by {resolved_by} ({resolution_action}): {resolution_notes}"

    cur.execute("""
    UPDATE supplier_invoices 
    SET status = ?, plain_english_explanation = ?, resolved_by = ?, resolution_notes = ?, resolved_at = ?, needs_reverification = 1
    WHERE id = ?
    """, (new_status, new_expl, resolved_by, resolution_notes, now_str, inv["id"]))

    conn.commit()
    conn.close()

    log_finance_audit(
        "INVOICE", inv["id"], "RESOLVE_DISCREPANCY",
        f"Status changed to {new_status} by {resolved_by}. Resolution: {resolution_notes}",
        resolved_by, db_path
    )

    return {
        "success": True,
        "invoice_id": inv["id"],
        "status": new_status,
        "resolved_by": resolved_by,
        "resolution_notes": resolution_notes
    }

def can_generate_verified_report(invoice_id: str, db_path: str = DB_PATH) -> Dict[str, Any]:
    """Evaluates the 5 mandatory conditions for verified PDF report generation.
    
    Conditions:
    1. PO, Goods Receipt, and Invoice match successfully.
    2. Ordered, received, and invoiced quantities reconciled with zero unresolved discrepancies.
    3. Prices, taxes, and totals pass deterministic Decimal validation.
    4. Verified payment records confirm full payable amount is PAID IN FULL.
    5. No duplicate-invoice or unresolved review flags remain.
    """
    init_finance_tables(db_path)
    conn = get_connection(db_path)
    cur = conn.cursor()

    cur.execute("""
    SELECT 
        inv.*,
        po.quantity as po_quantity,
        po.unit_cost as po_unit_cost,
        po.shipping_cost as po_shipping_cost,
        po.total_cost as po_total_cost,
        po.status as po_status,
        s.name as supplier_name
    FROM supplier_invoices inv
    JOIN purchase_orders po ON inv.po_id = po.id
    JOIN suppliers s ON inv.supplier_id = s.id
    WHERE inv.id = ? OR inv.invoice_number = ?
    """, (invoice_id, invoice_id))
    row = cur.fetchone()

    if not row:
        conn.close()
        return {
            "eligible": False,
            "checklist": {},
            "unmet_conditions": [f"Invoice '{invoice_id}' not found in database."]
        }

    inv = dict(row)
    po_id = inv["po_id"]
    inv_id = inv["id"]

    # 1. Goods Receipt totals
    cur.execute("""
    SELECT 
        COALESCE(SUM(quantity_received), 0) as total_received,
        COALESCE(SUM(quantity_damaged), 0) as total_damaged,
        COALESCE(SUM(quantity_missing), 0) as total_missing
    FROM goods_receipts 
    WHERE po_id = ?
    """, (po_id,))
    gr_totals = dict(cur.fetchone())

    # 2. Payment totals from verified records
    cur.execute("""
    SELECT 
        COALESCE(SUM(amount_paid), 0.0) as total_paid,
        COUNT(id) as payment_count 
    FROM invoice_payments 
    WHERE invoice_id = ?
    """, (inv_id,))
    pay_totals = dict(cur.fetchone())

    conn.close()

    total_invoiced_dec = quantize_money(inv["total_amount"])
    total_paid_dec = quantize_money(pay_totals["total_paid"])
    balance_dec = quantize_money(total_invoiced_dec - total_paid_dec)
    unit_price_dec = quantize_money(inv["unit_price"])
    po_unit_cost_dec = quantize_money(inv["po_unit_cost"])
    po_qty = inv["po_quantity"]
    invoiced_qty = inv["invoiced_quantity"]
    received_qty = gr_totals["total_received"]
    damaged_qty = gr_totals["total_damaged"]
    missing_qty = gr_totals["total_missing"]

    unmet_conditions = []

    # Condition 1: PO Match
    po_matched = (inv["po_id"] is not None) and (inv["supplier_id"] is not None)
    if not po_matched:
        unmet_conditions.append("Purchase order reference or supplier identifier could not be validated.")

    # Condition 2: Quantity Reconciliation (Ordered == Received == Invoiced, no missing/damaged)
    qty_reconciled = (
        po_qty == invoiced_qty and 
        invoiced_qty == received_qty and 
        damaged_qty == 0 and 
        missing_qty == 0
    )
    if not qty_reconciled:
        if damaged_qty > 0 or missing_qty > 0:
            unmet_conditions.append(f"Unresolved damaged/missing items at dock ({damaged_qty} damaged, {missing_qty} missing).")
        elif invoiced_qty != received_qty:
            unmet_conditions.append(f"Quantity mismatch: Invoiced {invoiced_qty} units but warehouse dock received {received_qty} units.")
        elif po_qty != invoiced_qty:
            unmet_conditions.append(f"Order quantity mismatch: PO quantity is {po_qty} but invoiced {invoiced_qty}.")

    # Condition 3: Financial & Decimal Validation
    subtotal_dec = quantize_money(Decimal(invoiced_qty) * unit_price_dec)
    ship_dec = quantize_money(inv.get("shipping_amount", 0.0))
    tax_dec = quantize_money(inv.get("tax_amount", 0.0))
    disc_dec = quantize_money(inv.get("discount_amount", 0.0))
    expected_total = quantize_money(subtotal_dec + ship_dec + tax_dec - disc_dec)
    
    price_ok = abs(unit_price_dec - po_unit_cost_dec) <= ROUNDING_TOLERANCE
    math_ok = abs(expected_total - total_invoiced_dec) <= ROUNDING_TOLERANCE

    financials_valid = price_ok and math_ok
    if not financials_valid:
        if not price_ok:
            unmet_conditions.append(f"Unit price mismatch: Invoiced ${unit_price_dec:,.2f} vs approved PO price ${po_unit_cost_dec:,.2f}.")
        if not math_ok:
            unmet_conditions.append(f"Arithmetic failure: Stated total ${total_invoiced_dec:,.2f} does not equal calculated charges ${expected_total:,.2f}.")

    # Condition 4: Verified Payment Settlement (PAID IN FULL)
    payment_settled = (
        pay_totals["payment_count"] > 0 and
        total_paid_dec >= total_invoiced_dec - ROUNDING_TOLERANCE and
        abs(balance_dec) <= ROUNDING_TOLERANCE and
        total_paid_dec <= total_invoiced_dec + ROUNDING_TOLERANCE
    )
    if not payment_settled:
        if pay_totals["payment_count"] == 0 or total_paid_dec == Decimal("0.00"):
            unmet_conditions.append("Invoice has no verified payment records in database (UNPAID).")
        elif balance_dec > ROUNDING_TOLERANCE:
            unmet_conditions.append(f"Invoice is only PARTIALLY PAID: ${total_paid_dec:,.2f} of ${total_invoiced_dec:,.2f} settled (Outstanding: ${balance_dec:,.2f}).")
        elif balance_dec < -ROUNDING_TOLERANCE:
            unmet_conditions.append(f"PAYMENT MISMATCH: Recorded payments (${total_paid_dec:,.2f}) exceed invoice total (${total_invoiced_dec:,.2f}) by ${abs(balance_dec):,.2f}.")

    # Condition 5: No Duplicate or Unresolved Flags
    status_clean = inv["status"] in ["MATCHED", "RESOLVED"]
    if inv["status"] == "NEEDS_REVIEW":
        unmet_conditions.append("Transaction has unresolved 'Needs Review' discrepancies flagged in system.")
    elif inv["status"] == "REJECTED":
        unmet_conditions.append("Invoice was rejected by procurement review.")

    checklist = {
        "po_match": {
            "passed": po_matched,
            "title": "Purchase Order Match",
            "details": f"PO {inv['po_id']} valid & issued to {inv['supplier_name']}"
        },
        "receipt_match": {
            "passed": qty_reconciled,
            "title": "Goods Receipt Match",
            "details": f"{received_qty}/{invoiced_qty} units received (Damaged: {damaged_qty}, Missing: {missing_qty})"
        },
        "pricing_tax_match": {
            "passed": financials_valid,
            "title": "Deterministic Financial Math",
            "details": f"Unit price ${unit_price_dec:,.2f} matches PO, calculated total ${total_invoiced_dec:,.2f}"
        },
        "pricing_match": {
            "passed": financials_valid,
            "title": "Deterministic Financial Math",
            "details": f"Unit price ${unit_price_dec:,.2f} matches PO, calculated total ${total_invoiced_dec:,.2f}"
        },
        "payment_reconciled": {
            "passed": payment_settled,
            "title": "Verified Payment Settlement",
            "details": f"${total_paid_dec:,.2f} paid of ${total_invoiced_dec:,.2f} (Status: {'PAID IN FULL' if payment_settled else 'NOT SETTLED'})"
        },
        "payment_settled": {
            "passed": payment_settled,
            "title": "Verified Payment Settlement",
            "details": f"${total_paid_dec:,.2f} paid of ${total_invoiced_dec:,.2f} (Status: {'PAID IN FULL' if payment_settled else 'NOT SETTLED'})"
        },
        "no_unresolved_discrepancies": {
            "passed": status_clean and len(unmet_conditions) == 0,
            "title": "Clean Audit & Review Flags",
            "details": "Zero open duplicate warnings or unresolved disputes"
        },
        "no_discrepancies": {
            "passed": status_clean and len(unmet_conditions) == 0,
            "title": "Clean Audit & Review Flags",
            "details": "Zero open duplicate warnings or unresolved disputes"
        }
    }

    eligible = len(unmet_conditions) == 0

    return {
        "eligible": eligible,
        "checklist": checklist,
        "unmet_conditions": unmet_conditions,
        "invoice_number": inv["invoice_number"],
        "po_id": inv["po_id"],
        "total_amount": float(total_invoiced_dec),
        "total_paid": float(total_paid_dec),
        "balance_due": float(max(Decimal("0.00"), balance_dec))
    }

def seed_sample_finance_records(db_path: str = DB_PATH) -> None:
    """Seed synthetic sample records demonstrating all matching and review cases."""
    init_finance_tables(db_path)
    conn = get_connection(db_path)
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) as cnt FROM supplier_invoices")
    if cur.fetchone()["cnt"] > 0:
        conn.close()
        return

    sample_pos = [
        ("PO-2026-001", "ITEM-001", "SUP-01", 50, 85.0, 120.0, 4370.0, "DELIVERED", "AUTONOMOUS", "2026-10-08", "PO-2026-001", "Standard restocking order"),
        ("PO-2026-002", "ITEM-002", "SUP-02", 36, 62.5, 160.0, 2410.0, "PARTIALLY_RECEIVED", "AUTONOMOUS", "2026-10-09", "PO-2026-002", "CNC End Mills order"),
        ("PO-2026-003", "ITEM-012", "SUP-01", 18, 145.0, 120.0, 2730.0, "DELIVERED", "AUTONOMOUS", "2026-10-10", "PO-2026-003", "High pressure valves"),
        ("PO-2026-004", "ITEM-010", "SUP-02", 22, 220.0, 160.0, 5000.0, "DELIVERED", "AUTONOMOUS", "2026-10-11", "PO-2026-004", "Ball screws"),
    ]
    cur.executemany("""
    INSERT OR REPLACE INTO purchase_orders 
    (id, item_id, supplier_id, quantity, unit_cost, shipping_cost, total_cost, status, approval_type, created_at, order_reference, notes)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, sample_pos)

    # 1. Clean Receipt for PO-2026-001 (50 units)
    cur.execute("""
    INSERT OR REPLACE INTO goods_receipts 
    (id, po_id, received_date, received_by, quantity_received, quantity_damaged, quantity_missing, condition_notes, batch_number, created_at)
    VALUES ('GR-2026-001', 'PO-2026-001', '2026-10-10', 'Dock Officer Adams', 50, 0, 0, 'Inspected, all crates sealed and intact.', 'LOT-9921', '2026-10-10 14:00:00')
    """)

    # 2. Missing Goods Receipt for PO-2026-002 (26 received, 10 missing)
    cur.execute("""
    INSERT OR REPLACE INTO goods_receipts 
    (id, po_id, received_date, received_by, quantity_received, quantity_damaged, quantity_missing, condition_notes, batch_number, created_at)
    VALUES ('GR-2026-002', 'PO-2026-002', '2026-10-11', 'Dock Officer Adams', 26, 0, 10, 'Pallet arrived with missing crate #3. 10 units missing.', 'LOT-8812', '2026-10-11 11:30:00')
    """)

    # 3. Clean Receipt for PO-2026-003 (18 units)
    cur.execute("""
    INSERT OR REPLACE INTO goods_receipts 
    (id, po_id, received_date, received_by, quantity_received, quantity_damaged, quantity_missing, condition_notes, batch_number, created_at)
    VALUES ('GR-2026-003', 'PO-2026-003', '2026-10-11', 'Supervisor Miller', 18, 0, 0, 'All valves pass initial pressure bench test.', 'LOT-7741', '2026-10-11 16:15:00')
    """)

    # 4. Clean Receipt for PO-2026-004 (22 units)
    cur.execute("""
    INSERT OR REPLACE INTO goods_receipts 
    (id, po_id, received_date, received_by, quantity_received, quantity_damaged, quantity_missing, condition_notes, batch_number, created_at)
    VALUES ('GR-2026-004', 'PO-2026-004', '2026-10-12', 'Dock Officer Adams', 22, 0, 0, 'Clean delivery.', 'LOT-6632', '2026-10-12 09:00:00')
    """)

    # Sample Invoice 1: MATCHED & FULLY PAID (Eligible for Verified PDF Report!)
    cur.execute("""
    INSERT INTO supplier_invoices 
    (id, invoice_number, po_id, supplier_id, invoice_date, invoiced_quantity, unit_price, tax_amount, shipping_amount, discount_amount, total_amount, file_name, extraction_confidence, status, discrepancy_reasons, plain_english_explanation, created_at)
    VALUES ('INV-APEX-101', 'INV-APEX-101', 'PO-2026-001', 'SUP-01', '2026-10-10', 50, 85.0, 0.0, 120.0, 0.0, 4370.0, 'invoice_apex_101.pdf', 0.98, 'MATCHED', '[]', 'All quantities, unit prices, delivery receipts, and payments align 100% with the purchase order.', '2026-10-10 15:00:00')
    """)
    cur.execute("""
    INSERT INTO invoice_payments 
    (id, invoice_id, po_id, payment_date, amount_paid, payment_method, reference_number, notes, created_at)
    VALUES ('PAY-101', 'INV-APEX-101', 'PO-2026-001', '2026-10-11', 4370.0, 'ACH', 'ACH-994821', 'Full payment processed', '2026-10-11 10:00:00')
    """)

    # Sample Invoice 2: NEEDS_REVIEW due to Missing Goods (36 invoiced vs 26 received)
    cur.execute("""
    INSERT INTO supplier_invoices 
    (id, invoice_number, po_id, supplier_id, invoice_date, invoiced_quantity, unit_price, tax_amount, shipping_amount, discount_amount, total_amount, file_name, extraction_confidence, status, discrepancy_reasons, plain_english_explanation, created_at)
    VALUES ('INV-TITAN-202', 'INV-TITAN-202', 'PO-2026-002', 'SUP-02', '2026-10-11', 36, 62.5, 0.0, 160.0, 0.0, 2410.0, 'invoice_titan_202.pdf', 0.95, 'NEEDS_REVIEW', '["Missing goods discrepancy: Supplier invoiced 36 units, but warehouse dock only received 26 units (10 units missing)."]', 'Review required: Supplier invoiced 36 units, but warehouse dock only received 26 units (10 missing). Hold payment until credit note.', '2026-10-11 12:00:00')
    """)

    # Sample Invoice 3: PARTIALLY_PAID ($1,500 of $2,730 paid, balance $1,230)
    cur.execute("""
    INSERT INTO supplier_invoices 
    (id, invoice_number, po_id, supplier_id, invoice_date, invoiced_quantity, unit_price, tax_amount, shipping_amount, discount_amount, total_amount, file_name, extraction_confidence, status, discrepancy_reasons, plain_english_explanation, created_at)
    VALUES ('INV-APEX-303', 'INV-APEX-303', 'PO-2026-003', 'SUP-01', '2026-10-11', 18, 145.0, 0.0, 120.0, 0.0, 2730.0, 'invoice_apex_303.pdf', 0.99, 'PARTIALLY_PAID', '[]', 'Partially Paid: $1,500.00 recorded of $2,730.00. Outstanding balance: $1,230.00.', '2026-10-11 17:00:00')
    """)
    cur.execute("""
    INSERT INTO invoice_payments 
    (id, invoice_id, po_id, payment_date, amount_paid, payment_method, reference_number, notes, created_at)
    VALUES ('PAY-303', 'INV-APEX-303', 'PO-2026-003', '2026-10-12', 1500.0, 'Wire', 'WIRE-448291', 'First milestone installment', '2026-10-12 09:30:00')
    """)

    # Sample Invoice 4: PAYMENT_MISMATCH ($5,000 billed vs $5,500 paid -> overpayment)
    cur.execute("""
    INSERT INTO supplier_invoices 
    (id, invoice_number, po_id, supplier_id, invoice_date, invoiced_quantity, unit_price, tax_amount, shipping_amount, discount_amount, total_amount, file_name, extraction_confidence, status, discrepancy_reasons, plain_english_explanation, created_at)
    VALUES ('INV-TITAN-404', 'INV-TITAN-404', 'PO-2026-004', 'SUP-02', '2026-10-12', 22, 220.0, 0.0, 160.0, 0.0, 5000.0, 'invoice_titan_404.pdf', 0.97, 'PAYMENT_MISMATCH', '[]', 'Payment Mismatch: Recorded payments ($5,500.00) exceed invoice total ($5,000.00) by $500.00 (Overpayment).', '2026-10-12 10:00:00')
    """)
    cur.execute("""
    INSERT INTO invoice_payments 
    (id, invoice_id, po_id, payment_date, amount_paid, payment_method, reference_number, notes, created_at)
    VALUES ('PAY-404', 'INV-TITAN-404', 'PO-2026-004', '2026-10-12', 5500.0, 'ACH', 'ACH-551920', 'Overpayment detected by finance', '2026-10-12 11:00:00')
    """)

    conn.commit()
    conn.close()
