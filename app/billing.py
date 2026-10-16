"""Stripe-hosted Checkout verification and exactly-once ledger accounting.

Never mark test-mode, unverifiable or non-CNY payment as real income.
Only a signed checkout.session.completed event with paid status is eligible.
"""
import os
import json
from .db import _lock, now_iso


def handle_event(db, event):
    event_id = str(event.get("id", ""))
    kind = event.get("type", "")
    data = event.get("data", {}).get("object", {})
    if not event_id.startswith("evt_") or len(event_id) > 180:
        raise ValueError("Invalid event ID")
    if kind != "checkout.session.completed":
        return {"accepted": True, "processed": False, "reason": "ignored type"}
    if data.get("object") != "checkout.session" or data.get("payment_status") != "paid":
        return {"accepted": True, "processed": False, "reason": "not paid"}
    session_id = str(data.get("id", ""))
    if not session_id.startswith("cs_") or len(session_id) > 180:
        raise ValueError("Invalid checkout session")
    amount_minor = data.get("amount_total")
    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int) or amount_minor <= 0:
        raise ValueError("Invalid amount")
    currency = str(data.get("currency", "")).upper()
    if len(currency) != 3 or not currency.isalpha():
        raise ValueError("Invalid currency")
    live = bool(event.get("livemode", False))
    details = data.get("customer_details") or {}
    email = str(details.get("email") or "")[:250]
    item = str((data.get("metadata") or {}).get("offer") or "InquiryPilot 询盘处理服务")[:250]
    with _lock, db._conn:
        existing = db._conn.execute("SELECT event_id FROM payment_events WHERE session_id=? OR event_id=?",
                                     (session_id, event_id)).fetchone()
        if existing:
            return {"accepted": True, "processed": False, "reason": "duplicate"}
        ts = now_iso()
        db._conn.execute(
            "INSERT INTO payment_events(event_id,session_id,amount_minor,currency,"
            "livemode,email,created_at) VALUES(?,?,?,?,?,?,?)",
            (event_id, session_id, amount_minor, currency, int(live), email, ts))
        # Must never convert currencies using an estimated FX rate.
        if currency != "CNY":
            return {"accepted": True, "processed": False, "reason": "non-CNY - requires reconciliation"}
        amount_cny = round(amount_minor / 100, 2)
        db._conn.execute(
            "INSERT INTO orders(module_key,customer,item,amount_cny,status,is_simulated,created_at,notes)"
            " VALUES(?,?,?,?,?,?,?,?)",
            ("inquirypilot", email, item, amount_cny, "paid",
             0 if live else 1, ts, f"Stripe checkout session {session_id}"))
        order_id = db._conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        db._conn.execute(
            "INSERT INTO ledger(ts,kind,amount_cny,currency,category,description,"
            "ref_type,ref_id,is_simulated) VALUES(?,?,?,?,?,?,?,?,?)",
            (ts, "revenue", amount_cny, "CNY", "stripe",
             f"已验证 Stripe Checkout {session_id}", "order", order_id,
             0 if live else 1))
    return {"accepted": True, "processed": True,
            "real_revenue": bool(live and currency == "CNY")}


def verify_stripe_event(raw, signature):
    key = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
    if not key:
        raise ValueError("STRIPE_WEBHOOK_SECRET 未配置")
    import stripe
    return stripe.Webhook.construct_event(raw, signature, key)
