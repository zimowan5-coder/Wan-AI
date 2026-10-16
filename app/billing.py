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
    if kind == "charge.refunded":
        return _handle_charge_refunded(db, event, data)
    if kind not in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
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
    intent = str(data.get("payment_intent") or "")
    if intent and not intent.startswith("pi_"):
        raise ValueError("Invalid payment intent")
    with _lock, db._conn:
        existing = db._conn.execute("SELECT event_id FROM payment_events WHERE session_id=? OR event_id=?",
                                     (session_id, event_id)).fetchone()
        if existing:
            return {"accepted": True, "processed": False, "reason": "duplicate"}
        ts = now_iso()
        db._conn.execute(
            "INSERT INTO payment_events(event_id,session_id,amount_minor,currency,"
            "livemode,email,payment_intent,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (event_id, session_id, amount_minor, currency, int(live), email, intent, ts))
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
        if intent:
            _reconcile_refunds(db, intent, ts)
    return {"accepted": True, "processed": True,
            "real_revenue": bool(live and currency == "CNY")}



def _handle_charge_refunded(db, event, charge):
    """Record cumulative charge.refunded; out-of-order and duplicates are safe."""
    charge_id = str(charge.get("id") or "")
    intent = str(charge.get("payment_intent") or "")
    currency = str(charge.get("currency") or "").upper()
    minor = charge.get("amount_refunded")
    if charge.get("object") != "charge" or not charge_id.startswith("ch_") or not intent.startswith("pi_"):
        raise ValueError("Malformed charge.refunded")
    if not isinstance(minor, int) or isinstance(minor, bool) or minor < 0:
        raise ValueError("Invalid refund value")
    if currency != "CNY":
        return {"accepted": True, "processed": False, "reason": "foreign refund requires reconciliation"}
    live = int(bool(event.get("livemode")))
    with _lock, db._conn:
        row = db._conn.execute(
            "SELECT * FROM charge_refunds WHERE charge_id=?", (charge_id,)).fetchone()
        if row and (row["payment_intent"] != intent or row["currency"] != currency or row["livemode"] != live):
            raise ValueError("Inconsistent refund event")
        cumulative = max(minor, row["cumulative_minor"] if row else 0)
        ts = now_iso()
        db._conn.execute(
            "INSERT INTO charge_refunds(charge_id,payment_intent,currency,livemode,"
            "cumulative_minor,recorded_minor,updated_at) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(charge_id) DO UPDATE SET cumulative_minor=excluded.cumulative_minor,"
            "updated_at=excluded.updated_at",
            (charge_id, intent, currency, live, cumulative, 0, ts))
        accounted = _reconcile_refunds(db, intent, ts)
    return {"accepted": True, "processed": bool(accounted),
            "reason": "refunded amount reconciled" if accounted else "pending or duplicate"}


def _reconcile_refunds(db, intent, ts):
    """Match to a paid Checkout session. Sum recorded refunds never exceeds sale."""
    payment = db._conn.execute(
        "SELECT amount_minor,livemode FROM payment_events "
        "WHERE payment_intent=? AND currency='CNY' LIMIT 1", (intent,)).fetchone()
    if not payment:
        return 0  # refund may arrive before checkout; hold and reconcile on checkout event
    refunded = db._conn.execute(
        "SELECT COALESCE(SUM(recorded_minor),0) AS n FROM charge_refunds "
        "WHERE payment_intent=? AND currency='CNY' AND livemode=?",
        (intent, payment["livemode"])).fetchone()["n"]
    remaining = max(0, payment["amount_minor"] - refunded)
    rows = db._conn.execute(
        "SELECT * FROM charge_refunds WHERE payment_intent=? AND currency='CNY'"
        " AND livemode=? ORDER BY charge_id", (intent, payment["livemode"])).fetchall()
    done = 0
    for row in rows:
        due = min(max(0, row["cumulative_minor"] - row["recorded_minor"]), remaining)
        if due <= 0:
            continue
        db._conn.execute(
            "INSERT INTO ledger(ts,kind,amount_cny,currency,category,description,"
            "ref_type,ref_id,is_simulated) VALUES(?,?,?,?,?,?,?,?,?)",
            (ts, "refund", round(due / 100, 2), "CNY", "stripe",
             f"Stripe 退款 {row['charge_id']}，订单对应付款 {intent}",
             "stripe_charge", None, 0 if payment["livemode"] else 1))
        db._conn.execute(
            "UPDATE charge_refunds SET recorded_minor=recorded_minor+?,updated_at=?"
            " WHERE charge_id=?", (due, ts, row["charge_id"]))
        remaining -= due
        done += due
    return done


def verify_stripe_event(raw, signature):
    key = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
    if not key:
        raise ValueError("STRIPE_WEBHOOK_SECRET 未配置")
    import stripe
    return stripe.Webhook.construct_event(raw, signature, key)
