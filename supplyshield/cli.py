"""CLI interface for SupplyShield to allow seamless API bridging."""

import sys
import json
from supplyshield.db import init_db, get_connection
from supplyshield.agent import SupplyShieldAgent
from supplyshield.evals import run_evaluations
from supplyshield.tools import check_inventory, retrieve_procurement_policies
from supplyshield.finance import (
    init_finance_tables,
    seed_sample_finance_records,
    record_goods_receipt,
    extract_invoice_document,
    verify_three_way_match,
    record_invoice_payment,
    resolve_invoice_discrepancy
)

def handle_command():
    init_db()
    init_finance_tables()
    seed_sample_finance_records()

    if len(sys.argv) < 2:
        print(json.dumps({"error": "No command specified"}))
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "inventory":
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT i.*, s.name as primary_supplier_name 
            FROM inventory_items i
            LEFT JOIN suppliers s ON i.primary_supplier_id = s.id
            ORDER BY (i.current_stock < i.safety_stock) DESC, i.current_stock ASC
        """)
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        print(json.dumps({"success": True, "items": rows}))

    elif cmd == "suppliers":
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT * FROM suppliers")
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        print(json.dumps({"success": True, "suppliers": rows}))

    elif cmd == "policies":
        res = retrieve_procurement_policies("policy rules compliance")
        print(json.dumps(res))

    elif cmd == "investigate":
        if len(sys.argv) < 3:
            print(json.dumps({"error": "Missing item_id"}))
            sys.exit(1)
        item_id = sys.argv[2]
        simulate_failure = len(sys.argv) > 3 and sys.argv[3].lower() == "true"
        agent = SupplyShieldAgent()
        res = agent.investigate_shortage(item_id, simulate_supplier_api_failure=simulate_failure)
        print(json.dumps(res))

    elif cmd == "approve":
        if len(sys.argv) < 3:
            print(json.dumps({"error": "Missing item_id"}))
            sys.exit(1)
        item_id = sys.argv[2]
        notes = sys.argv[3] if len(sys.argv) > 3 else "Approved by human procurement manager."
        agent = SupplyShieldAgent()
        agent.investigate_shortage(item_id)
        po = agent.execute_approval(notes=notes)
        print(json.dumps({"success": True, "purchase_order": po, "agent_state": agent.current_state.value}))

    elif cmd == "evals":
        results = run_evaluations()
        print(json.dumps(results))

    elif cmd == "adjust_stock":
        if len(sys.argv) < 4:
            print(json.dumps({"error": "Missing item_id or stock delta"}))
            sys.exit(1)
        item_id = sys.argv[2]
        delta = int(sys.argv[3])
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT current_stock FROM inventory_items WHERE id = ?", (item_id,))
        row = cur.fetchone()
        if not row:
            conn.close()
            print(json.dumps({"error": "Item not found"}))
            sys.exit(1)
        new_stock = max(0, row["current_stock"] + delta)
        cur.execute("UPDATE inventory_items SET current_stock = ? WHERE id = ?", (new_stock, item_id))
        conn.commit()
        conn.close()
        print(json.dumps({"success": True, "item_id": item_id, "new_stock": new_stock}))

    # ------------------ FINANCECHECK COMMANDS ------------------
    elif cmd == "pos":
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT 
                po.*, 
                i.name as item_name, 
                i.sku,
                s.name as supplier_name,
                COALESCE(SUM(gr.quantity_received), 0) as total_received,
                COALESCE(SUM(gr.quantity_damaged), 0) as total_damaged,
                COALESCE(SUM(gr.quantity_missing), 0) as total_missing,
                COUNT(DISTINCT inv.id) as invoice_count
            FROM purchase_orders po
            LEFT JOIN inventory_items i ON po.item_id = i.id
            LEFT JOIN suppliers s ON po.supplier_id = s.id
            LEFT JOIN goods_receipts gr ON po.id = gr.po_id
            LEFT JOIN supplier_invoices inv ON po.id = inv.po_id
            GROUP BY po.id
            ORDER BY po.created_at DESC
        """)
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        print(json.dumps({"success": True, "purchase_orders": rows}))

    elif cmd == "receipts":
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT 
                gr.*,
                po.order_reference,
                po.quantity as po_quantity,
                i.name as item_name,
                s.name as supplier_name
            FROM goods_receipts gr
            JOIN purchase_orders po ON gr.po_id = po.id
            JOIN inventory_items i ON po.item_id = i.id
            JOIN suppliers s ON po.supplier_id = s.id
            ORDER BY gr.created_at DESC
        """)
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        print(json.dumps({"success": True, "receipts": rows}))

    elif cmd == "invoices":
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT 
                inv.*,
                s.name as supplier_name,
                po.order_reference,
                po.quantity as po_quantity,
                po.unit_cost as po_unit_cost,
                i.name as item_name,
                COALESCE((SELECT SUM(amount_paid) FROM invoice_payments WHERE invoice_id = inv.id), 0.0) as total_paid,
                COALESCE((SELECT SUM(quantity_received) FROM goods_receipts WHERE po_id = inv.po_id), 0) as goods_received_count
            FROM supplier_invoices inv
            JOIN suppliers s ON inv.supplier_id = s.id
            JOIN purchase_orders po ON inv.po_id = po.id
            JOIN inventory_items i ON po.item_id = i.id
            ORDER BY inv.created_at DESC
        """)
        rows = []
        for r in cur.fetchall():
            d = dict(r)
            d["balance_due"] = max(0.0, round(d["total_amount"] - d["total_paid"], 2))
            try:
                d["discrepancy_list"] = json.loads(d.get("discrepancy_reasons") or "[]")
            except Exception:
                d["discrepancy_list"] = []
            rows.append(d)
        conn.close()
        print(json.dumps({"success": True, "invoices": rows}))

    elif cmd == "record_receipt":
        payload = json.loads(sys.argv[2])
        res = record_goods_receipt(
            po_id=payload["po_id"],
            quantity_received=int(payload["quantity_received"]),
            quantity_damaged=int(payload.get("quantity_damaged", 0)),
            quantity_missing=int(payload.get("quantity_missing", 0)),
            received_by=payload.get("received_by", "Dock Team"),
            condition_notes=payload.get("condition_notes", ""),
            batch_number=payload.get("batch_number", "")
        )
        print(json.dumps(res))

    elif cmd == "verify_invoice":
        payload = json.loads(sys.argv[2])
        res = verify_three_way_match(
            invoice_number=payload["invoice_number"],
            po_id=payload["po_id"],
            supplier_id=payload["supplier_id"],
            invoiced_quantity=int(payload["invoiced_quantity"]),
            unit_price=float(payload["unit_price"]),
            total_amount=float(payload["total_amount"]),
            tax_amount=float(payload.get("tax_amount", 0.0)),
            shipping_amount=float(payload.get("shipping_amount", 0.0)),
            file_name=payload.get("file_name", "upload.pdf"),
            raw_content=payload.get("raw_content", ""),
            extraction_confidence=float(payload.get("extraction_confidence", 1.0))
        )
        print(json.dumps(res))

    elif cmd == "record_payment":
        payload = json.loads(sys.argv[2])
        res = record_invoice_payment(
            invoice_id=payload["invoice_id"],
            amount_paid=float(payload["amount_paid"]),
            payment_method=payload.get("payment_method", "ACH"),
            reference_number=payload.get("reference_number", ""),
            notes=payload.get("notes", "")
        )
        print(json.dumps(res))

    elif cmd == "resolve_discrepancy":
        payload = json.loads(sys.argv[2])
        res = resolve_invoice_discrepancy(
            invoice_id=payload["invoice_id"],
            resolved_by=payload["resolved_by"],
            resolution_notes=payload["resolution_notes"],
            resolution_action=payload.get("resolution_action", "ACCEPT_WITH_NOTE")
        )
        print(json.dumps(res))

    elif cmd == "extract_invoice":
        file_name = sys.argv[2] if len(sys.argv) > 2 else "sample_invoice.pdf"
        raw_text = sys.argv[3] if len(sys.argv) > 3 else ""
        res = extract_invoice_document(file_name, raw_text)
        print(json.dumps(res))

    elif cmd == "finance_audit":
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT * FROM finance_audit_logs ORDER BY id DESC LIMIT 50")
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        print(json.dumps({"success": True, "audit_logs": rows}))

    elif cmd == "can_generate_report":
        if len(sys.argv) < 3:
            print(json.dumps({"error": "Missing invoice_id"}))
            sys.exit(1)
        invoice_id = sys.argv[2]
        from supplyshield.finance import can_generate_verified_report
        res = can_generate_verified_report(invoice_id)
        print(json.dumps(res))

    elif cmd == "generate_report":
        if len(sys.argv) < 3:
            print(json.dumps({"error": "Missing invoice_id"}))
            sys.exit(1)
        invoice_id = sys.argv[2]
        created_by = sys.argv[3] if len(sys.argv) > 3 else "Authorized Finance Officer"
        from supplyshield.reports import generate_verified_pdf_report
        res = generate_verified_pdf_report(invoice_id, created_by)
        # Avoid huge JSON payload for stdout if not requested, include base64
        import base64
        if res.get("success") and "pdf_bytes" in res:
            res["pdf_base64"] = base64.b64encode(res["pdf_bytes"]).decode("ascii")
            del res["pdf_bytes"]
        print(json.dumps(res))

    elif cmd == "correct_invoice":
        if len(sys.argv) < 3:
            print(json.dumps({"error": "Missing payload"}))
            sys.exit(1)
        payload = json.loads(sys.argv[2])
        from supplyshield.finance import correct_invoice_data
        res = correct_invoice_data(
            invoice_id=payload["invoice_id"],
            corrected_by=payload.get("corrected_by", "Reviewer"),
            corrected_values=payload.get("corrected_values", {}),
            notes=payload.get("notes", "")
        )
        print(json.dumps(res))

    elif cmd == "reports":
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT id, invoice_id, po_id, version, report_title, verification_hash, created_at, created_by, status 
            FROM verification_reports 
            ORDER BY created_at DESC
        """)
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        print(json.dumps({"success": True, "reports": rows}))

    elif cmd == "get_report_pdf":
        if len(sys.argv) < 3:
            print(json.dumps({"error": "Missing report_id"}))
            sys.exit(1)
        report_id = sys.argv[2]
        from supplyshield.reports import get_report_pdf
        pdf_bytes = get_report_pdf(report_id)
        import base64
        if pdf_bytes:
            print(json.dumps({"success": True, "report_id": report_id, "pdf_base64": base64.b64encode(pdf_bytes).decode("ascii")}))
        else:
            print(json.dumps({"success": False, "error": f"Report '{report_id}' not found"}))

    else:
        print(json.dumps({"error": f"Unknown command {cmd}"}))
        sys.exit(1)

if __name__ == "__main__":
    handle_command()
