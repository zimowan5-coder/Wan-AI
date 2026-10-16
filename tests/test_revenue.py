"""Revenue workflow integration tests: no external credentials or network calls."""
import hashlib
import hmac
import pytest
from app.db import Database
from app import revenue, billing, approvals, worker, tasks
from app.security import check_signature
from app.mailer import send_approved

@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "business.db")
    yield d
    d.close()

def add_lead(db, external="inquiry-1"):
    return revenue.capture(db, source="webhook", external_id=external,
        sender_email="buyer@example.org", text="Need 1000 pcs, quote please.",
        subject="RFQ")

def test_capture_idempotent(db):
    first, created = add_lead(db)
    second, again = add_lead(db)
    assert created and not again
    assert first["id"] == second["id"]
    assert db.query_one("SELECT COUNT(*) AS n FROM tasks")["n"] == 1

def test_approval_never_sends(db):
    lead, _ = add_lead(db)
    ok, _ = worker.run_task_once(db, lead["task_id"])
    assert ok
    task = tasks.get_task(db, lead["task_id"])
    assert task["status"] == "awaiting_approval"
    assert revenue.get_lead(db, lead["id"])["status"] == "awaiting_review"
    approvals.decide(db, task["approval_id"], True)
    assert revenue.get_lead(db, lead["id"])["status"] == "ready_to_send"
    assert revenue.get_lead(db, lead["id"])["sent_at"] is None

def test_rejected_draft(db):
    lead, _ = add_lead(db)
    worker.run_task_once(db, lead["task_id"])
    approvals.decide(db, tasks.get_task(db, lead["task_id"])["approval_id"], False)
    assert revenue.get_lead(db, lead["id"])["status"] == "lost"
    with pytest.raises(ValueError):
        send_approved(db, lead["id"])

def test_signatures():
    raw = b'{"event_id":"abc"}'
    signature = hmac.new(b"secret", raw, hashlib.sha256).hexdigest()
    assert check_signature("secret", raw, "sha256=" + signature)
    assert not check_signature("secret", b"tampered", signature)
    assert not check_signature("", raw, signature)

def event(event_id="evt_paid", session="cs_paid", live=True, currency="cny"):
    return {"id": event_id, "livemode": live, "type": "checkout.session.completed",
        "data": {"object": {"object": "checkout.session", "id": session,
            "payment_status": "paid", "amount_total": 19900, "currency": currency}}}

def test_verified_live_accounting_is_once(db):
    assert billing.handle_event(db, event())["real_revenue"]
    assert not billing.handle_event(db, event())["processed"]
    from app.finance import totals
    assert totals(db)["revenue_real"] == 199
    assert db.query_one("SELECT COUNT(*) AS n FROM orders")["n"] == 1

def test_testmode_and_foreign_currency_are_not_real(db):
    billing.handle_event(db, event("evt_test", "cs_test", live=False))
    billing.handle_event(db, event("evt_usd", "cs_usd", currency="usd"))
    from app.finance import totals
    assert totals(db)["revenue_real"] == 0
    assert totals(db)["revenue_sim"] == 199
    assert db.query_one("SELECT COUNT(*) AS n FROM orders")["n"] == 1

def test_send_requires_smtp(db, monkeypatch):
    lead, _ = add_lead(db)
    worker.run_task_once(db, lead["task_id"])
    approvals.decide(db, tasks.get_task(db, lead["task_id"])["approval_id"], True)
    for key in ("WANAI_SMTP_HOST", "WANAI_SMTP_USER", "WANAI_SMTP_PASSWORD", "WANAI_FROM_EMAIL"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ValueError):
        send_approved(db, lead["id"])
    assert revenue.get_lead(db, lead["id"])["status"] == "ready_to_send"

def test_reject_bad_email_and_fake_outcome(db):
    with pytest.raises(ValueError):
        revenue.capture(db, source="manual", external_id="x",
            sender_email="bad\nmail@example.org", text="need quote")
    with pytest.raises(ValueError):
        revenue.mark_outcome(db, 1, "won")

def paid_with_intent(event_id="evt_i_paid", session="cs_i_paid", intent="pi_i_paid", live=True):
    e = event(event_id, session, live=live)
    e["data"]["object"]["payment_intent"] = intent
    return e


def refund_event(minor, event_id="evt_refund_1", intent="pi_i_paid"):
    return {"id": event_id, "type": "charge.refunded", "livemode": True,
      "data": {"object": {"object": "charge", "id": "ch_i_paid",
        "payment_intent": intent, "currency": "cny", "amount_refunded": minor}}}


def test_refund_events_idempotent_and_partial(db):
    billing.handle_event(db, paid_with_intent())
    assert billing.handle_event(db, refund_event(5000))["processed"]
    assert not billing.handle_event(db, refund_event(5000))["processed"]
    assert billing.handle_event(db, refund_event(12000))["processed"]
    assert not billing.handle_event(db, refund_event(8000))["processed"]
    from app.finance import totals
    assert totals(db)["revenue_real"] == 199
    assert totals(db)["refund_real"] == 120
    assert totals(db)["net_real"] == 79


def test_refund_before_checkout_reconciles(db):
    assert not billing.handle_event(db, refund_event(3000))["processed"]
    billing.handle_event(db, paid_with_intent())
    from app.finance import totals
    assert totals(db)["refund_real"] == 30
    assert not billing.handle_event(db, refund_event(3000))["processed"]


def test_async_paid_checkout(db):
    evt = paid_with_intent()
    evt["type"] = "checkout.session.async_payment_succeeded"
    assert billing.handle_event(db, evt)["real_revenue"]


def test_migration_adds_payment_intent(tmp_path):
    import sqlite3
    path = tmp_path / "legacy-v02.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE payment_events(event_id TEXT PRIMARY KEY,"
                 "session_id TEXT UNIQUE, amount_minor INTEGER, currency TEXT,"
                 "livemode INTEGER, email TEXT, created_at TEXT)")
    conn.commit()
    conn.close()
    migrated = Database(path)
    cols = [r[1] for r in migrated.query("PRAGMA table_info(payment_events)")]
    assert "payment_intent" in cols
    migrated.close()
