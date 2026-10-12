"""SupplyShield — Autonomous AI Procurement Agent Streamlit Dashboard.

A clean, elegant single-page dashboard adhering strictly to Section 7 design guidelines:
- White background, dark charcoal text, restrained navy accents, subtle borders
- Vertical arrangement:
  1. Header
  2. Inventory Overview
  3. Shortage Investigation
  4. Agent Execution
  5. Supplier Comparison
  6. Decision Summary
  7. Simulated Purchase Order
  8. Evaluation Results
  9. Execution Logs
"""

import streamlit as st
import sqlite3
import time
import os
from pathlib import Path

from supplyshield.db import init_db, get_connection, DB_PATH
from supplyshield.agent import SupplyShieldAgent, AgentState
from supplyshield.evals import run_evaluations
from supplyshield.guardrails import MAX_AUTONOMOUS_SPEND

# Initialize database if needed
init_db()

# Streamlit Page Configuration
st.set_page_config(
    page_title="SupplyShield — Autonomous AI Procurement Agent",
    page_icon="🛡️",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# Custom minimal, restrained styling
st.markdown("""
<style>
    /* Clean, professional styling */
    .stApp {
        background-color: #FAFAFA;
        color: #1E293B;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    }
    .metric-card {
        background: #FFFFFF;
        border: 1px solid #E2E8F0;
        border-radius: 8px;
        padding: 16px;
        text-align: center;
    }
    .metric-val {
        font-size: 24px;
        font-weight: 700;
        color: #0F172A;
    }
    .metric-lbl {
        font-size: 13px;
        color: #64748B;
        margin-top: 4px;
    }
    .badge-ready {
        background: #DCFCE7;
        color: #166534;
        padding: 4px 10px;
        border-radius: 4px;
        font-weight: 600;
        font-size: 13px;
    }
    .badge-approval {
        background: #FEF3C7;
        color: #92400E;
        padding: 4px 10px;
        border-radius: 4px;
        font-weight: 600;
        font-size: 13px;
    }
    .badge-blocked {
        background: #FEE2E2;
        color: #991B1B;
        padding: 4px 10px;
        border-radius: 4px;
        font-weight: 600;
        font-size: 13px;
    }
    .section-title {
        font-size: 18px;
        font-weight: 600;
        color: #0F172A;
        border-bottom: 2px solid #E2E8F0;
        padding-bottom: 8px;
        margin-top: 24px;
        margin-bottom: 16px;
    }
</style>
""", unsafe_allow_html=True)

# 1. HEADER
col_h1, col_h2 = st.columns([3, 1])
with col_h1:
    st.markdown("## 🛡️ SupplyShield")
    st.markdown("**Autonomous AI Procurement Agent** — Safe, Guardrailed Factory Inventory Replenishment")
with col_h2:
    st.markdown("""
    <div style="text-align: right; padding-top: 10px;">
        <span style="background: #E0E7FF; color: #3730A3; padding: 4px 12px; border-radius: 12px; font-size: 12px; font-weight: 600;">
            Demo Mode Active (Safe Simulation)
        </span>
    </div>
    """, unsafe_allow_html=True)

# 2. INVENTORY OVERVIEW
conn = get_connection()
cur = conn.cursor()
cur.execute("SELECT COUNT(*) as total, SUM(CASE WHEN current_stock < safety_stock THEN 1 ELSE 0 END) as low_stock, SUM(CASE WHEN current_stock = 0 THEN 1 ELSE 0 END) as zero_stock FROM inventory_items")
inv_stats = dict(cur.fetchone())

cur.execute("SELECT COUNT(*) as pending FROM purchase_orders WHERE status = 'PENDING_APPROVAL'")
po_stats = dict(cur.fetchone())
conn.close()

st.markdown('<div class="section-title">1. Inventory Overview</div>', unsafe_allow_html=True)
m1, m2, m3, m4 = st.columns(4)
with m1:
    st.markdown(f"""<div class="metric-card"><div class="metric-val">{inv_stats['total']}</div><div class="metric-lbl">Total Catalog Items</div></div>""", unsafe_allow_html=True)
with m2:
    st.markdown(f"""<div class="metric-card"><div class="metric-val" style="color: #D97706;">{inv_stats['low_stock']}</div><div class="metric-lbl">Low Stock Items</div></div>""", unsafe_allow_html=True)
with m3:
    st.markdown(f"""<div class="metric-card"><div class="metric-val" style="color: #DC2626;">{inv_stats['zero_stock']}</div><div class="metric-lbl">Critical Shortages (Stock = 0)</div></div>""", unsafe_allow_html=True)
with m4:
    st.markdown(f"""<div class="metric-card"><div class="metric-val" style="color: #2563EB;">{po_stats['pending']}</div><div class="metric-lbl">Pending Approvals</div></div>""", unsafe_allow_html=True)

# Session state initialization
if "agent_result" not in st.session_state:
    st.session_state["agent_result"] = None
if "agent_instance" not in st.session_state:
    st.session_state["agent_instance"] = None
if "eval_result" not in st.session_state:
    st.session_state["eval_result"] = None

# 3. SHORTAGE INVESTIGATION
st.markdown('<div class="section-title">2. Shortage Investigation</div>', unsafe_allow_html=True)

conn = get_connection()
cur = conn.cursor()
cur.execute("SELECT id, sku, name, current_stock, safety_stock, category FROM inventory_items ORDER BY current_stock ASC")
all_items = [dict(r) for r in cur.fetchall()]
conn.close()

item_options = {
    f"{item['id']} — {item['name']} (Stock: {item['current_stock']}/{item['safety_stock']})": item['id']
    for item in all_items
}

c_sel, c_btn = st.columns([3, 1])
with c_sel:
    selected_label = st.selectbox("Select Factory Component to Audit:", list(item_options.keys()))
    chosen_item_id = item_options[selected_label]
with c_btn:
    st.write("")
    st.write("")
    investigate_clicked = st.button("🔍 Investigate Shortage", type="primary", use_container_width=True)

if investigate_clicked:
    agent = SupplyShieldAgent()
    with st.spinner("Executing agent harness: TRIAGE → ASSESS_IMPACT → FIND_ALTERNATIVES → VALIDATE..."):
        result = agent.investigate_shortage(chosen_item_id)
        st.session_state["agent_result"] = result
        st.session_state["agent_instance"] = agent

res = st.session_state.get("agent_result")
agent_inst = st.session_state.get("agent_instance")

if res:
    # 4. AGENT EXECUTION
    st.markdown('<div class="section-title">3. Agent Execution & State Machine</div>', unsafe_allow_html=True)
    
    state_col, steps_col, time_col, cost_col = st.columns(4)
    with state_col:
        badge_class = "badge-ready" if res["state"] in ["READY_TO_ORDER", "COMPLETE"] else ("badge-approval" if res["state"] == "APPROVAL_REQUIRED" else "badge-blocked")
        st.markdown(f"**Current State:** <span class='{badge_class}'>{res['state']}</span>", unsafe_allow_html=True)
    with steps_col:
        st.markdown(f"**Completed Steps:** `{res['step_count']} / 5`")
    with time_col:
        st.markdown(f"**Execution Latency:** `{res['elapsed_time_ms']} ms`")
    with cost_col:
        st.markdown(f"**Est. Model Cost:** `${res.get('estimated_cost_usd', 0.0):.6f}`")

    # Reasoning and decision explanation
    st.markdown("**Agent Decision Explanation & Audit Citation:**")
    st.info(res.get("decision_explanation", res.get("reason", "Investigation completed.")))

    # Policy citations from RAG
    if res.get("policy_citations"):
        with st.expander("📚 Retrieved RAG Policies & Compliance Rules"):
            for pol in res["policy_citations"]:
                st.markdown(f"- **{pol['policy_id']} ({pol['title']})**: *{pol['relevant_snippet']}*")

    # 5. SUPPLIER COMPARISON TABLE
    st.markdown('<div class="section-title">4. Supplier Comparison</div>', unsafe_allow_html=True)
    evaluated = res.get("evaluated_options", [])
    if evaluated:
        comparison_rows = []
        for o in evaluated:
            status_text = f"✅ {o['status']}" if o['status'] == "APPROVED" else f"⚠️ {o['status']}"
            comparison_rows.append({
                "Supplier ID": o["supplier_id"],
                "Supplier Name": o["supplier_name"],
                "Status": status_text,
                "Reliability": f"{o['reliability_score']*100:.0f}%",
                "Available Stock": f"{o['available_stock']} units",
                "Unit Price": f"${o['unit_cost']:.2f}",
                "Lead Time": f"{o['estimated_delivery_days']} days",
                "Shipping": f"${o['shipping_cost']:.2f}",
                "Total Landed Cost": f"${o['total_cost']:,.2f}",
                "Injection Detected": "🚨 YES (Defused)" if o.get("injection_detected") else "None"
            })
        st.dataframe(comparison_rows, use_container_width=True)
    else:
        st.caption("No alternative supplier quotes available for comparison.")

    # 6. DECISION SUMMARY
    st.markdown('<div class="section-title">5. Decision Summary & Guardrail Verdict</div>', unsafe_allow_html=True)
    compliance = res.get("compliance", {})
    if compliance:
        c1, c2, c3 = st.columns(3)
        with c1:
            sup_chk = compliance.get("supplier_check", {})
            st.metric("Supplier Qualification", "PASS" if sup_chk.get("eligible") else "FAIL", sup_chk.get("status"))
        with c2:
            spd_chk = compliance.get("spending_check", {})
            st.metric("Autonomous Cap (<$10k)", "PASS" if spd_chk.get("under_cap") else "EXCEEDED", f"${spd_chk.get('total_cost', 0):,.2f}")
        with c3:
            inj_chk = compliance.get("injection_check", {})
            st.metric("Prompt Injection Check", "CLEAN" if not inj_chk.get("detected") else "ATTACK DEFUSED", "Untrusted filter active")

    # 7. SIMULATED PURCHASE ORDER
    st.markdown('<div class="section-title">6. Simulated Purchase Order</div>', unsafe_allow_html=True)
    cand = res.get("selected_candidate")
    po_data = res.get("purchase_order")

    if po_data:
        st.success(f"🎉 **Purchase Order Generated:** Reference `{po_data['order_reference']}` | Status: `{po_data['status']}` | Total: `${po_data['total_cost']:,.2f}`")
    elif cand and res["state"] in ["READY_TO_ORDER", "APPROVAL_REQUIRED"]:
        st.write(f"Proposed order: **{res.get('deficit')} units** from **{cand['supplier_name']}** for **${cand['total_cost']:,.2f}**.")
        approval_btn = st.button("✅ Authorize Simulated Purchase Order (Safe Demo)", type="primary")
        if approval_btn and agent_inst:
            po_res = agent_inst.execute_approval()
            st.session_state["agent_result"] = agent_inst.investigation_result
            st.rerun()
    elif res["state"] == "BLOCKED":
        st.error("❌ Action Blocked: Cannot issue purchase order. Mandatory human intervention or supplier vetting required.")

# 8. EVALUATION RESULTS
st.markdown('<div class="section-title">7. Evaluation Results (Golden Set)</div>', unsafe_allow_html=True)
ev_col1, ev_col2 = st.columns([1, 3])
with ev_col1:
    run_eval_clicked = st.button("🧪 Run Golden Set Evals (6 Scenarios)", use_container_width=True)

if run_eval_clicked or st.session_state["eval_result"] is None:
    st.session_state["eval_result"] = run_evaluations()

eval_res = st.session_state["eval_result"]
if eval_res:
    p1, p2, p3 = st.columns(3)
    with p1:
        st.metric("Golden Set Pass Rate", f"{eval_res['pass_rate_pct']}%", f"{eval_res['passed_count']}/{eval_res['total_scenarios']} passed")
    with p2:
        st.metric("Evaluation Duration", f"{eval_res['total_duration_ms']} ms")
    with p3:
        st.metric("Failed Scenarios", f"{eval_res['failed_count']}")

    eval_table = []
    for s in eval_res["scenarios"]:
        eval_table.append({
            "Scenario ID": s["scenario_id"],
            "Scenario Name": s["name"],
            "Result": "✅ PASSED" if s["passed"] else "❌ FAILED",
            "Expected State": s["expected_state"],
            "Actual State": s["actual_state"],
            "Steps": s["steps_taken"],
            "Duration": f"{s['duration_ms']} ms",
            "Notes": s["details"]
        })
    st.dataframe(eval_table, use_container_width=True)

# 9. TECHNICAL EXECUTION LOGS
st.markdown('<div class="section-title">8. Technical Execution Logs</div>', unsafe_allow_html=True)
with st.expander("🔍 View Redacted Audit Logs & Tool Calls (Click to expand)"):
    if res and res.get("logs"):
        for log_entry in res["logs"]:
            st.text(f"[{log_entry['timestamp']}] Step {log_entry['step']} ({log_entry['state']}) {log_entry['action']}: {log_entry['details']}")
    else:
        st.caption("Perform an investigation to inspect live step logs and redacted trace tokens.")
