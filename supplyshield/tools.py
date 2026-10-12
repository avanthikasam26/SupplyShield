"""Procurement tools for SupplyShield autonomous agent."""

import os
import glob
import re
import uuid
import datetime
from pathlib import Path
from typing import Dict, List, Any, Optional

from .db import get_connection, DB_PATH
from .guardrails import (
    validate_spending_limit,
    validate_supplier_eligibility,
    check_prompt_injection,
    sanitize_untrusted_input
)

def check_inventory(item_id: str, db_path: str = DB_PATH) -> Dict[str, Any]:
    """Look up an inventory item by ID, calculating current shortage deficit."""
    conn = get_connection(db_path)
    cur = conn.cursor()
    cur.execute("SELECT * FROM inventory_items WHERE id = ? OR sku = ?", (item_id, item_id))
    row = cur.fetchone()
    conn.close()

    if not row:
        return {
            "success": False,
            "error": f"Item {item_id} not found in inventory."
        }

    item = dict(row)
    current_stock = item["current_stock"]
    safety_stock = item["safety_stock"]
    deficit = max(0, safety_stock - current_stock)
    is_shortage = current_stock < safety_stock

    return {
        "success": True,
        "item_id": item["id"],
        "sku": item["sku"],
        "name": item["name"],
        "category": item["category"],
        "current_stock": current_stock,
        "safety_stock": safety_stock,
        "deficit": deficit,
        "is_shortage": is_shortage,
        "unit_price": item["unit_price"],
        "unit_of_measure": item["unit_of_measure"],
        "primary_supplier_id": item["primary_supplier_id"],
        "last_inspected": item["last_inspected"]
    }

def find_backup_suppliers(item_id: str, db_path: str = DB_PATH, simulate_failure: bool = False) -> Dict[str, Any]:
    """Find all backup supplier options for an item from supplier catalog.
    
    Includes supplier status, stock availability, quoted price, lead time, and raw notes.
    """
    if simulate_failure:
        return {
            "success": False,
            "error": "SUPPLIER_API_TIMEOUT: Connection to external supplier gateway timed out after 5000ms."
        }

    conn = get_connection(db_path)
    cur = conn.cursor()
    query = """
    SELECT 
        sc.id as quote_id,
        sc.item_id,
        sc.supplier_id,
        s.name as supplier_name,
        s.status as supplier_status,
        s.reliability_score,
        s.base_shipping_cost,
        sc.available_stock,
        sc.unit_cost,
        sc.estimated_delivery_days,
        sc.supplier_notes
    FROM supplier_catalog sc
    JOIN suppliers s ON sc.supplier_id = s.id
    WHERE sc.item_id = ?
    ORDER BY s.status = 'APPROVED' DESC, sc.unit_cost ASC
    """
    cur.execute(query, (item_id,))
    rows = cur.fetchall()
    conn.close()

    options = [dict(r) for r in rows]
    return {
        "success": True,
        "item_id": item_id,
        "total_suppliers_found": len(options),
        "suppliers": options
    }

def calculate_costs(item_id: str, supplier_id: str, quantity: int, db_path: str = DB_PATH) -> Dict[str, Any]:
    """Deterministic calculation of order subtotal, shipping, and total landed cost."""
    conn = get_connection(db_path)
    cur = conn.cursor()
    cur.execute("""
    SELECT sc.unit_cost, s.base_shipping_cost, s.name as supplier_name
    FROM supplier_catalog sc
    JOIN suppliers s ON sc.supplier_id = s.id
    WHERE sc.item_id = ? AND sc.supplier_id = ?
    """, (item_id, supplier_id))
    row = cur.fetchone()
    conn.close()

    if not row:
        return {
            "success": False,
            "error": f"No quotation found for item {item_id} from supplier {supplier_id}."
        }

    unit_cost = float(row["unit_cost"])
    base_shipping = float(row["base_shipping_cost"])
    subtotal = round(unit_cost * quantity, 2)
    # Expedited weight/volume surcharge calculation if quantity > 50
    freight_surcharge = round(subtotal * 0.02, 2) if quantity > 50 else 0.0
    shipping_total = round(base_shipping + freight_surcharge, 2)
    total_cost = round(subtotal + shipping_total, 2)

    return {
        "success": True,
        "item_id": item_id,
        "supplier_id": supplier_id,
        "supplier_name": row["supplier_name"],
        "quantity": quantity,
        "unit_cost": unit_cost,
        "subtotal": subtotal,
        "shipping_cost": shipping_total,
        "total_cost": total_cost
    }

def retrieve_procurement_policies(query: str, policy_dir: Optional[str] = None) -> Dict[str, Any]:
    """RAG tool: retrieve relevant rules from local Markdown policy files based on query keywords.
    
    Returns matched policy excerpts, policy IDs, and supporting citations.
    """
    if policy_dir is None:
        # Defaults to supplyshield/policies
        base_dir = Path(__file__).resolve().parent
        policy_dir = str(base_dir / "policies")

    files = glob.glob(os.path.join(policy_dir, "*.md"))
    results = []
    
    query_terms = [t.lower() for t in re.split(r"\W+", query) if len(t) > 2]

    for file_path in sorted(files):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
                
            # Score file based on term matches
            score = 0
            lowered = content.lower()
            for term in query_terms:
                score += lowered.count(term)
                
            # Extract Policy ID if present
            pid_match = re.search(r"Policy ID:\s*([A-Z0-9\-]+)", content)
            policy_id = pid_match.group(1) if pid_match else Path(file_path).stem
            
            # Extract title
            title_match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
            title = title_match.group(1) if title_match else Path(file_path).stem

            # Extract relevant paragraphs
            paragraphs = [p.strip() for p in content.split("\n\n") if p.strip()]
            relevant_snippets = []
            for p in paragraphs:
                p_low = p.lower()
                if any(t in p_low for t in query_terms):
                    relevant_snippets.append(p)

            results.append({
                "policy_id": policy_id,
                "title": title,
                "file": Path(file_path).name,
                "score": score,
                "snippets": relevant_snippets[:3] if relevant_snippets else [paragraphs[0] if paragraphs else ""],
                "full_text": content
            })
        except Exception as e:
            continue

    # Sort by relevance score
    results.sort(key=lambda x: x["score"], reverse=True)

    return {
        "success": True,
        "query": query,
        "total_policies_scanned": len(files),
        "matched_policies": results
    }

def validate_compliance(
    item_id: str, 
    supplier_id: str, 
    quantity: int, 
    total_cost: float,
    supplier_status: str,
    raw_supplier_note: str
) -> Dict[str, Any]:
    """Hard guardrail validation of spending limits, supplier qualification, and prompt injection."""
    
    # 1. Supplier Eligibility Guardrail
    is_supplier_eligible, supplier_msg = validate_supplier_eligibility(supplier_status)

    # 2. Spending Limit Guardrail ($10,000 threshold)
    is_under_spending_cap, spend_msg = validate_spending_limit(total_cost)

    # 3. Prompt Injection Defense
    injection_detected, matched_patterns = check_prompt_injection(raw_supplier_note)
    sanitized_note = sanitize_untrusted_input(raw_supplier_note)

    # Overall compliance resolution
    passed_all_automated = (
        is_supplier_eligible and
        is_under_spending_cap and
        (not injection_detected)
    )

    action_recommendation = "READY_TO_ORDER"
    if not is_supplier_eligible:
        action_recommendation = "BLOCKED" if supplier_status.upper() == "PROHIBITED" else "APPROVAL_REQUIRED"
    elif not is_under_spending_cap:
        action_recommendation = "APPROVAL_REQUIRED"
    elif injection_detected:
        action_recommendation = "APPROVAL_REQUIRED"

    return {
        "success": True,
        "passed_all_automated": passed_all_automated,
        "action_recommendation": action_recommendation,
        "supplier_check": {
            "eligible": is_supplier_eligible,
            "status": supplier_status,
            "message": supplier_msg
        },
        "spending_check": {
            "under_cap": is_under_spending_cap,
            "total_cost": total_cost,
            "limit": 10000.00,
            "message": spend_msg
        },
        "injection_check": {
            "detected": injection_detected,
            "patterns": matched_patterns,
            "sanitized_note": sanitized_note
        }
    }

def create_simulated_purchase_order(
    item_id: str,
    supplier_id: str,
    quantity: int,
    unit_cost: float,
    shipping_cost: float,
    total_cost: float,
    approval_type: str,
    notes: str = "",
    db_path: str = DB_PATH
) -> Dict[str, Any]:
    """Generates a simulated purchase order with safety reference ID.
    
    Strict safety requirement: this is a simulation only, never connected to real payment gateways.
    """
    po_id = f"PO-SIM-{uuid.uuid4().hex[:8].upper()}"
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = get_connection(db_path)
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO purchase_orders 
    (id, item_id, supplier_id, quantity, unit_cost, shipping_cost, total_cost, status, approval_type, created_at, order_reference, notes)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        po_id, item_id, supplier_id, quantity, unit_cost, shipping_cost, total_cost,
        "SIMULATED_ORDERED", approval_type, timestamp, po_id, notes
    ))
    conn.commit()
    conn.close()

    return {
        "success": True,
        "po_id": po_id,
        "order_reference": po_id,
        "item_id": item_id,
        "supplier_id": supplier_id,
        "quantity": quantity,
        "unit_cost": unit_cost,
        "shipping_cost": shipping_cost,
        "total_cost": total_cost,
        "approval_type": approval_type,
        "status": "SIMULATED_ORDERED",
        "timestamp": timestamp,
        "message": f"Simulated purchase order {po_id} generated safely. No actual financial transaction occurred."
    }
