"""SupplyShield Verified PDF Report Generation Module.

Generates immutable, accounting-grade A4 PDF reports only when:
1. PO, Goods Receipt, and Invoice match successfully.
2. Quantities are 100% reconciled with no open discrepancy.
3. Prices, taxes, and totals pass deterministic Decimal validation.
4. Verified payment records confirm the full amount is PAID IN FULL.
5. No duplicate-invoice or unresolved review flags remain.
"""

import math
import os
import re
import uuid
import hashlib
import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, List, Any, Optional, Tuple

from .db import get_connection, DB_PATH
from .guardrails import redact_log

def init_reports_table(db_path: str = DB_PATH) -> None:
    """Ensure verification_reports table exists."""
    conn = get_connection(db_path)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS verification_reports (
        id TEXT PRIMARY KEY,
        invoice_id TEXT NOT NULL,
        po_id TEXT NOT NULL,
        version INTEGER NOT NULL DEFAULT 1,
        report_title TEXT NOT NULL,
        file_path TEXT,
        pdf_content BLOB,
        verification_hash TEXT NOT NULL,
        created_at TEXT NOT NULL,
        created_by TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'VALID', -- VALID, SUPERSEDED_BY_REVISION
        FOREIGN KEY (invoice_id) REFERENCES supplier_invoices (id),
        FOREIGN KEY (po_id) REFERENCES purchase_orders (id)
    )
    """)
    conn.commit()
    conn.close()

def escape_pdf_text(text: str) -> str:
    """Escape parentheses and backslashes for PDF string literal and sanitize unicode."""
    if not text:
        return ""
    clean = str(text).replace("—", " -- ").replace("–", "-").replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    clean = clean.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return clean.encode("ascii", "replace").decode("ascii")

def build_pdf_document(
    report_id: str,
    version: int,
    gen_time_str: str,
    supplier_name: str,
    supplier_id: str,
    redacted_account: str,
    po_reference: str,
    invoice_number: str,
    order_date: str,
    invoice_date: str,
    receipt_date: str,
    item_name: str,
    item_sku: str,
    ordered_qty: int,
    received_qty: int,
    invoiced_qty: int,
    unit_price: Decimal,
    shipping: Decimal,
    tax: Decimal,
    discount: Decimal,
    total_amount: Decimal,
    total_paid: Decimal,
    payment_records: List[Dict[str, Any]]
) -> bytes:
    """Construct standard PDF 1.4 binary stream with subtle diagonal watermark and structured layout."""
    
    # Page dimensions: A4 (595.28 x 841.89 pt)
    w = 595.28
    h = 841.89

    stream_cmds = []

    # 1. Subtle Diagonal Watermark (45 degrees across page)
    # Renders in faint grey: 0.91 0.93 0.95
    angle = 45 * math.pi / 180
    cos_a = math.cos(angle)
    sin_a = math.sin(angle)
    stream_cmds.append("q")
    stream_cmds.append("0.91 0.93 0.95 rg")
    stream_cmds.append(f"{cos_a:.4f} {sin_a:.4f} {-sin_a:.4f} {cos_a:.4f} 110 260 cm")
    stream_cmds.append("BT")
    stream_cmds.append("/F2 28 Tf")
    stream_cmds.append(f"({escape_pdf_text('SUPPLYSHIELD -- VERIFIED RECORD')}) Tj")
    stream_cmds.append("ET")
    stream_cmds.append("Q")

    # 2. Top Header Navy Banner
    stream_cmds.append("0.08 0.18 0.38 rg") # Navy blue
    stream_cmds.append("35 770 525 45 re f")
    stream_cmds.append("BT /F2 16 Tf 1 1 1 rg 48 788 Td (SupplyShield -- Purchase & Payment Verification Report) Tj ET")
    stream_cmds.append("BT /F1 9 Tf 0.85 0.90 0.98 rg 48 775 Td (AUTONOMOUS PROCUREMENT & FINANCECHECK AUDIT ARCHIVE) Tj ET")

    # 3. Report Metadata Strip
    stream_cmds.append("0.96 0.97 0.98 rg 35 730 525 32 re f")
    stream_cmds.append("0.85 0.88 0.92 RG 1 w 35 730 525 32 re S")
    stream_cmds.append(f"BT /F2 9 Tf 0.1 0.15 0.2 rg 45 742 Td (Report ID: {escape_pdf_text(report_id)}  |  Version: v{version}.0  |  Generated: {escape_pdf_text(gen_time_str)}) Tj ET")

    # 4. Transaction Identification Section
    stream_cmds.append("0.1 0.2 0.35 rg 35 700 525 18 re f")
    stream_cmds.append("BT /F2 10 Tf 1 1 1 rg 45 705 Td (1. Transaction Identifiers & Parties) Tj ET")

    y = 675
    left_x = 45
    right_x = 310

    stream_cmds.append(f"BT /F1 9 Tf 0.2 0.2 0.2 rg {left_x} {y} Td (Supplier:) Tj /F2 9 Tf 50 0 Td ({escape_pdf_text(supplier_name)} [{escape_pdf_text(supplier_id)}]) Tj ET")
    stream_cmds.append(f"BT /F1 9 Tf 0.2 0.2 0.2 rg {right_x} {y} Td (Purchase Order:) Tj /F2 9 Tf 80 0 Td ({escape_pdf_text(po_reference)}) Tj ET")

    y -= 16
    stream_cmds.append(f"BT /F1 9 Tf 0.2 0.2 0.2 rg {left_x} {y} Td (Vendor Account:) Tj /F2 9 Tf 75 0 Td ({escape_pdf_text(redacted_account)}) Tj ET")
    stream_cmds.append(f"BT /F1 9 Tf 0.2 0.2 0.2 rg {right_x} {y} Td (Supplier Invoice:) Tj /F2 9 Tf 80 0 Td ({escape_pdf_text(invoice_number)}) Tj ET")

    y -= 16
    stream_cmds.append(f"BT /F1 9 Tf 0.2 0.2 0.2 rg {left_x} {y} Td (Order Date:) Tj /F2 9 Tf 60 0 Td ({escape_pdf_text(order_date or 'N/A')}) Tj ET")
    stream_cmds.append(f"BT /F1 9 Tf 0.2 0.2 0.2 rg {right_x} {y} Td (Invoice Date:) Tj /F2 9 Tf 70 0 Td ({escape_pdf_text(invoice_date or 'N/A')}) Tj ET")

    y -= 16
    stream_cmds.append(f"BT /F1 9 Tf 0.2 0.2 0.2 rg {left_x} {y} Td (Dock Receipt Date:) Tj /F2 9 Tf 90 0 Td ({escape_pdf_text(receipt_date or 'N/A')}) Tj ET")

    # 5. Reconciled Quantities & Financial Line Items
    y -= 30
    stream_cmds.append(f"0.1 0.2 0.35 rg 35 {y} 525 18 re f")
    stream_cmds.append(f"BT /F2 10 Tf 1 1 1 rg 45 {y+5} Td (2. Reconciled Line Items & Charges) Tj ET")

    # Table Header
    y -= 20
    stream_cmds.append(f"0.92 0.94 0.97 rg 35 {y} 525 18 re f")
    stream_cmds.append(f"BT /F2 8 Tf 0.1 0.1 0.1 rg 42 {y+5} Td (Item Description & SKU) Tj ET")
    stream_cmds.append(f"BT /F2 8 Tf 0.1 0.1 0.1 rg 210 {y+5} Td (Ordered) Tj ET")
    stream_cmds.append(f"BT /F2 8 Tf 0.1 0.1 0.1 rg 255 {y+5} Td (Received) Tj ET")
    stream_cmds.append(f"BT /F2 8 Tf 0.1 0.1 0.1 rg 305 {y+5} Td (Invoiced) Tj ET")
    stream_cmds.append(f"BT /F2 8 Tf 0.1 0.1 0.1 rg 360 {y+5} Td (Unit Price) Tj ET")
    stream_cmds.append(f"BT /F2 8 Tf 0.1 0.1 0.1 rg 425 {y+5} Td (Shipping) Tj ET")
    stream_cmds.append(f"BT /F2 8 Tf 0.1 0.1 0.1 rg 480 {y+5} Td (Final Total) Tj ET")

    # Row 1
    y -= 22
    stream_cmds.append(f"0.98 0.99 1.0 rg 35 {y} 525 20 re f")
    stream_cmds.append(f"0.85 0.88 0.92 RG 0.5 w 35 {y} 525 20 re S")
    stream_cmds.append(f"BT /F2 8 Tf 0.1 0.1 0.1 rg 42 {y+6} Td ({escape_pdf_text(item_name[:26])}) Tj /F1 7 Tf 0 -8 Td ([SKU: {escape_pdf_text(item_sku)}]) Tj ET")
    stream_cmds.append(f"BT /F1 8 Tf 0.1 0.1 0.1 rg 215 {y+6} Td ({ordered_qty}) Tj ET")
    stream_cmds.append(f"BT /F1 8 Tf 0.1 0.1 0.1 rg 262 {y+6} Td ({received_qty}) Tj ET")
    stream_cmds.append(f"BT /F2 8 Tf 0.05 0.45 0.15 rg 312 {y+6} Td ({invoiced_qty}) Tj ET")
    stream_cmds.append(f"BT /F1 8 Tf 0.1 0.1 0.1 rg 365 {y+6} Td (${unit_price:,.2f}) Tj ET")
    stream_cmds.append(f"BT /F1 8 Tf 0.1 0.1 0.1 rg 430 {y+6} Td (${shipping:,.2f}) Tj ET")
    stream_cmds.append(f"BT /F2 8 Tf 0.1 0.1 0.1 rg 485 {y+6} Td (${total_amount:,.2f}) Tj ET")

    # Financial Breakdown Summary Box
    y -= 45
    stream_cmds.append(f"0.97 0.98 0.99 rg 290 {y} 270 38 re f")
    stream_cmds.append(f"0.85 0.88 0.92 RG 0.5 w 290 {y} 270 38 re S")
    stream_cmds.append(f"BT /F1 8 Tf 0.3 0.3 0.3 rg 300 {y+24} Td (Subtotal: ${unit_price * Decimal(invoiced_qty):,.2f}  |  Freight: ${shipping:,.2f}) Tj ET")
    stream_cmds.append(f"BT /F1 8 Tf 0.3 0.3 0.3 rg 300 {y+12} Td (Taxes: ${tax:,.2f}  |  Discounts Applied: ${discount:,.2f}) Tj ET")
    stream_cmds.append(f"BT /F2 9 Tf 0.08 0.18 0.38 rg 300 {y} Td (Validated Invoice Total: ${total_amount:,.2f}) Tj ET")

    # 6. Payment Reconciliation Section
    y -= 35
    stream_cmds.append(f"0.1 0.2 0.35 rg 35 {y} 525 18 re f")
    stream_cmds.append(f"BT /F2 10 Tf 1 1 1 rg 45 {y+5} Td (3. Verified Payment Transactions & Settlement) Tj ET")

    y -= 20
    stream_cmds.append(f"0.92 0.94 0.97 rg 35 {y} 525 18 re f")
    stream_cmds.append(f"BT /F2 8 Tf 0.1 0.1 0.1 rg 42 {y+5} Td (Payment Date) Tj ET")
    stream_cmds.append(f"BT /F2 8 Tf 0.1 0.1 0.1 rg 140 {y+5} Td (Method) Tj ET")
    stream_cmds.append(f"BT /F2 8 Tf 0.1 0.1 0.1 rg 240 {y+5} Td (Reference #) Tj ET")
    stream_cmds.append(f"BT /F2 8 Tf 0.1 0.1 0.1 rg 390 {y+5} Td (Amount Paid) Tj ET")
    stream_cmds.append(f"BT /F2 8 Tf 0.1 0.1 0.1 rg 480 {y+5} Td (Status) Tj ET")

    for pay in payment_records:
        y -= 16
        stream_cmds.append(f"0.85 0.88 0.92 RG 0.5 w 35 {y} 525 16 re S")
        stream_cmds.append(f"BT /F1 8 Tf 0.1 0.1 0.1 rg 42 {y+4} Td ({escape_pdf_text(pay.get('payment_date', 'N/A'))}) Tj ET")
        stream_cmds.append(f"BT /F1 8 Tf 0.1 0.1 0.1 rg 140 {y+4} Td ({escape_pdf_text(pay.get('payment_method', 'ACH'))}) Tj ET")
        stream_cmds.append(f"BT /F2 8 Tf 0.1 0.1 0.1 rg 240 {y+4} Td ({escape_pdf_text(pay.get('reference_number', 'REF-NONE'))}) Tj ET")
        stream_cmds.append(f"BT /F1 8 Tf 0.1 0.1 0.1 rg 390 {y+4} Td (${Decimal(str(pay.get('amount_paid', 0))):,.2f}) Tj ET")
        stream_cmds.append(f"BT /F2 8 Tf 0.05 0.5 0.15 rg 480 {y+4} Td (CLEARED) Tj ET")

    # Payment settlement line
    y -= 22
    outstanding = max(Decimal("0.00"), total_amount - total_paid)
    stream_cmds.append(f"0.92 0.98 0.94 rg 35 {y} 525 18 re f")
    stream_cmds.append(f"0.4 0.8 0.5 RG 1 w 35 {y} 525 18 re S")
    stream_cmds.append(f"BT /F2 8 Tf 0.05 0.45 0.15 rg 45 {y+5} Td (Total Verified Paid: ${total_paid:,.2f}  |  Outstanding Balance: ${outstanding:,.2f}  |  Payment Status: PAID IN FULL) Tj ET")

    # 7. Verification Results Summary Table
    y -= 35
    stream_cmds.append(f"0.1 0.2 0.35 rg 35 {y} 525 18 re f")
    stream_cmds.append(f"BT /F2 10 Tf 1 1 1 rg 45 {y+5} Td (4. Deterministic Verification Checklist) Tj ET")

    check_items = [
        ("Purchase Order Verification", f"MATCHED & APPROVED ({po_reference})"),
        ("Goods Receipt Verification", f"MATCHED & VERIFIED ({received_qty}/{ordered_qty} UNITS RECEIVED AT DOCK)"),
        ("Line Item Price & Tax Verification", f"MATCHED (CALCULATED DECIMAL EQUALITY TO ${total_amount:,.2f})"),
        ("Final Payment Settlement", f"PAID IN FULL (${total_paid:,.2f} / ${total_amount:,.2f} SETTLED)"),
        ("Overall Audit Status", "VERIFIED -- ALL CHECKS PASSED")
    ]

    for label, val in check_items:
        y -= 16
        stream_cmds.append(f"0.98 0.99 1.0 rg 35 {y} 525 16 re f")
        stream_cmds.append(f"0.85 0.88 0.92 RG 0.5 w 35 {y} 525 16 re S")
        stream_cmds.append(f"BT /F1 8 Tf 0.2 0.2 0.2 rg 45 {y+4} Td ({escape_pdf_text(label)}:) Tj ET")
        stream_cmds.append(f"BT /F2 8 Tf 0.05 0.45 0.15 rg 240 {y+4} Td ({escape_pdf_text(val)}) Tj ET")

    # 8. Legal Disclaimer & Filing Note
    y -= 36
    stream_cmds.append(f"0.96 0.97 0.98 rg 35 {y} 525 28 re f")
    stream_cmds.append(f"0.85 0.88 0.92 RG 0.5 w 35 {y} 525 28 re S")
    stream_cmds.append(f"BT /F1 7.5 Tf 0.35 0.35 0.35 rg 42 {y+16} Td (FILING NOTICE: This report is generated from SupplyShield's recorded transaction data and is intended for filing and future reference.) Tj ET")
    stream_cmds.append(f"BT /F1 7.5 Tf 0.35 0.35 0.35 rg 42 {y+6} Td (Verification hash: {escape_pdf_text(hashlib.sha256(f'{report_id}-{invoice_number}-{total_paid}'.encode()).hexdigest())}) Tj ET")

    # Compile stream into standard PDF 1.4 objects
    content_stream = "\n".join(stream_cmds).encode("latin1")
    stream_len = len(content_stream)

    objects = []
    # 1: Catalog
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    # 2: Pages
    objects.append(b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    # 3: Page
    objects.append(
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595.28 841.89] /Contents 4 0 R "
        b"/Resources << /Font << "
        b"/F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> "
        b"/F2 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >> "
        b">> >> >>"
    )
    # 4: Stream
    objects.append(
        b"<< /Length " + str(stream_len).encode("latin1") + b" >>\nstream\n" +
        content_stream +
        b"\nendstream"
    )

    # Build PDF with xref table
    out = [b"%PDF-1.4\n"]
    offsets = [0]
    curr_offset = len(out[0])

    for i, obj in enumerate(objects, start=1):
        offsets.append(curr_offset)
        header = f"{i} 0 obj\n".encode("latin1")
        footer = b"\nendobj\n"
        chunk = header + obj + footer
        out.append(chunk)
        curr_offset += len(chunk)

    xref_offset = curr_offset
    out.append(b"xref\n")
    out.append(f"0 {len(objects) + 1}\n".encode("latin1"))
    out.append(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        out.append(f"{off:010d} 00000 n \n".encode("latin1"))

    out.append(b"trailer\n")
    out.append(f"<< /Size {len(objects) + 1} /Root 1 0 R >>\n".encode("latin1"))
    out.append(b"startxref\n")
    out.append(f"{xref_offset}\n".encode("latin1"))
    out.append(b"%%EOF\n")

    return b"".join(out)

def generate_verified_pdf_report(
    invoice_id: str,
    created_by: str = "Authorized Operations Supervisor",
    db_path: str = DB_PATH
) -> Dict[str, Any]:
    """Evaluates all 5 mandatory verification conditions and produces a signed PDF report.
    
    If any condition is not met, returns clear error and unmet conditions.
    """
    init_reports_table(db_path)
    from .finance import can_generate_verified_report, to_decimal, quantize_money

    # Verify all 5 conditions deterministically
    check = can_generate_verified_report(invoice_id, db_path)
    if not check["eligible"]:
        return {
            "success": False,
            "error": "Invoice cannot generate a verified PDF report because one or more verification criteria failed.",
            "unmet_conditions": check["unmet_conditions"],
            "checklist": check["checklist"]
        }

    conn = get_connection(db_path)
    cur = conn.cursor()

    # Load invoice details
    cur.execute("""
    SELECT 
        inv.*,
        po.item_id,
        po.quantity as po_quantity,
        po.unit_cost as po_unit_cost,
        po.shipping_cost as po_shipping_cost,
        po.created_at as po_created_at,
        po.order_reference,
        s.name as supplier_name,
        s.payment_account,
        i.name as item_name,
        i.sku as item_sku
    FROM supplier_invoices inv
    JOIN purchase_orders po ON inv.po_id = po.id
    JOIN suppliers s ON inv.supplier_id = s.id
    JOIN inventory_items i ON po.item_id = i.id
    WHERE inv.id = ? OR inv.invoice_number = ?
    """, (invoice_id, invoice_id))
    inv_row = cur.fetchone()

    if not inv_row:
        conn.close()
        return {"success": False, "error": f"Invoice '{invoice_id}' not found."}

    inv = dict(inv_row)
    actual_inv_id = inv["id"]
    po_id = inv["po_id"]

    # Load goods receipts
    cur.execute("""
    SELECT 
        SUM(quantity_received) as total_received,
        MAX(received_date) as last_received_date
    FROM goods_receipts
    WHERE po_id = ?
    """, (po_id,))
    gr_row = cur.fetchone()
    total_received = (gr_row["total_received"] or 0)
    receipt_date = (gr_row["last_received_date"] or "")

    # Load verified payments
    cur.execute("""
    SELECT payment_date, amount_paid, payment_method, reference_number 
    FROM invoice_payments 
    WHERE invoice_id = ? 
    ORDER BY payment_date ASC
    """, (actual_inv_id,))
    payments = [dict(r) for r in cur.fetchall()]

    total_paid = sum(quantize_money(to_decimal(p["amount_paid"])) for p in payments)
    total_invoiced = quantize_money(to_decimal(inv["total_amount"]))
    unit_price = quantize_money(to_decimal(inv["unit_price"]))
    shipping = quantize_money(to_decimal(inv.get("shipping_amount", 0)))
    tax = quantize_money(to_decimal(inv.get("tax_amount", 0)))
    discount = quantize_money(to_decimal(inv.get("discount_amount", 0)))

    # Determine report version
    cur.execute("""
    SELECT MAX(version) as max_v FROM verification_reports 
    WHERE invoice_id = ?
    """, (actual_inv_id,))
    max_v_row = cur.fetchone()
    current_version = (max_v_row["max_v"] or 0) + 1

    # Mark previous reports as superseded
    cur.execute("""
    UPDATE verification_reports 
    SET status = 'SUPERSEDED_BY_REVISION' 
    WHERE invoice_id = ? AND status = 'VALID'
    """, (actual_inv_id,))

    report_id = f"RPT-2026-{uuid.uuid4().hex[:8].upper()}"
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC")
    report_title = f"Purchase & Payment Verification Report - {inv['invoice_number']} (v{current_version})"

    # Redact sensitive bank account
    clean_account = redact_log(inv.get("payment_account", ""))

    # Generate binary PDF
    pdf_bytes = build_pdf_document(
        report_id=report_id,
        version=current_version,
        gen_time_str=now_str,
        supplier_name=inv["supplier_name"],
        supplier_id=inv["supplier_id"],
        redacted_account=clean_account,
        po_reference=inv["order_reference"],
        invoice_number=inv["invoice_number"],
        order_date=inv.get("po_created_at", "")[:10],
        invoice_date=inv.get("invoice_date", "")[:10],
        receipt_date=receipt_date,
        item_name=inv["item_name"],
        item_sku=inv["item_sku"],
        ordered_qty=inv["po_quantity"],
        received_qty=total_received,
        invoiced_qty=inv["invoiced_quantity"],
        unit_price=unit_price,
        shipping=shipping,
        tax=tax,
        discount=discount,
        total_amount=total_invoiced,
        total_paid=total_paid,
        payment_records=payments
    )

    v_hash = hashlib.sha256(pdf_bytes).hexdigest()

    # Save report metadata & content
    cur.execute("""
    INSERT INTO verification_reports 
    (id, invoice_id, po_id, version, report_title, file_path, pdf_content, verification_hash, created_at, created_by, status)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'VALID')
    """, (
        report_id, actual_inv_id, po_id, current_version, report_title,
        f"{report_id}.pdf", pdf_bytes, v_hash, now_str, created_by
    ))

    # Update invoice record with report reference
    cur.execute("""
    UPDATE supplier_invoices 
    SET current_report_id = ?, needs_reverification = 0 
    WHERE id = ?
    """, (report_id, actual_inv_id))

    conn.commit()
    conn.close()

    return {
        "success": True,
        "report_id": report_id,
        "version": current_version,
        "report_title": report_title,
        "invoice_number": inv["invoice_number"],
        "po_reference": inv["order_reference"],
        "total_amount": float(total_invoiced),
        "total_paid": float(total_paid),
        "created_at": now_str,
        "pdf_bytes": pdf_bytes,
        "pdf_size_bytes": len(pdf_bytes),
        "verification_hash": v_hash,
        "checklist": check["checklist"]
    }

def get_report_pdf(report_id: str, db_path: str = DB_PATH) -> Optional[bytes]:
    """Retrieve raw PDF bytes by report ID."""
    conn = get_connection(db_path)
    cur = conn.cursor()
    cur.execute("SELECT pdf_content FROM verification_reports WHERE id = ?", (report_id,))
    row = cur.fetchone()
    conn.close()
    if row and row["pdf_content"]:
        return row["pdf_content"]
    return None
