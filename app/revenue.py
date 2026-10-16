"""Revenue pipeline: verified inbound -> deduplicated lead -> task -> reviewed draft.

No fabricated customers, no scraping personal contacts, no unsolicited mail.
"""
import hashlib
import os
import re
from datetime import datetime, timedelta
from email.header import decode_header, make_header

from .db import now_iso, _lock
from . import tasks

STAGES = ("new", "processing", "awaiting_review", "ready_to_send", "sending",
          "sent", "won", "lost", "failed")
MAX_BODY = 12_000


def clean_email(value):
    """Reject CR/LF, header tricks and invalid destinations."""
    from email.utils import parseaddr
    raw = (value or "").strip()
    if not raw or "\r" in raw or "\n" in raw or len(raw) > 254:
        return ""
    name, addr = parseaddr(raw)
    if addr != raw or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", addr):
        return ""
    return addr


def capture(db, *, source, external_id, sender_email, text, subject="", sender_name=""):
    """Idempotent capture. Only called on authenticated form/webhook/owner inbox."""
    source = re.sub(r"[^a-z0-9_-]", "", (source or "form").lower())[:32] or "form"
    body = (text or "").strip()[:MAX_BODY]
    address = clean_email(sender_email)
    if not address or len(body) < 5:
        raise ValueError("需要有效的邮箱地址和至少 5 字的询盘内容")
    ext = (external_id or "").strip()[:256]
    identity = ext or hashlib.sha256((address.lower()+"\n"+subject+"\n"+body).encode()).hexdigest()
    key = hashlib.sha256((source+"\0"+identity).encode()).hexdigest()
    with _lock:
        cur = db._conn.execute(
            "INSERT OR IGNORE INTO leads(external_key,source,sender_email,sender_name,"
            "subject,body,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (key, source, address, sender_name[:120], subject[:250],
             body, "new", now_iso(), now_iso()))
        inserted = cur.rowcount == 1
        db._conn.commit()
        row = db._conn.execute("SELECT id,task_id FROM leads WHERE external_key=?",
                                (key,)).fetchone()
    lead_id = row["id"]
    if inserted or row["task_id"] is None:
        # Recovery on a previous crash between insert and task creation.
        tid = tasks.create_task(db, type="inquiry_process",
              title=f"真实询盘 #{lead_id} · {subject[:55] or address}",
              payload={"inquiry_text": body, "lead_id": lead_id},
              module_key="inquirypilot", priority=5, max_attempts=2)
        with _lock:
            db._conn.execute(
                "UPDATE leads SET task_id=?, status='processing',updated_at=?"
                " WHERE id=? AND task_id IS NULL", (tid, now_iso(), lead_id))
            db._conn.commit()
        actual = db.query_one("SELECT task_id FROM leads WHERE id=?", (lead_id,))
        if actual["task_id"] != tid:
            tasks.cancel(db, tid)
    return get_lead(db, lead_id), inserted


def get_lead(db, lead_id):
    row = db.query_one("SELECT * FROM leads WHERE id=?", (lead_id,))
    return dict(row) if row else None


def list_leads(db, limit=200):
    return [dict(r) for r in db.query("SELECT * FROM leads ORDER BY id DESC LIMIT ?", (limit,))]


def set_draft(db, lead_id, draft, intent, score):
    if not 0 <= int(score) <= 100:
        raise ValueError("invalid score")
    db.execute("UPDATE leads SET draft=?,intent=?,score=?,status='awaiting_review',"
               "updated_at=? WHERE id=? AND status IN ('new','processing','failed')",
               (draft[:MAX_BODY], intent[:80], int(score), now_iso(), lead_id))


def edit_draft(db, lead_id, text):
    """Edit the approved-to-send payload before reviewer signs off."""
    draft = (text or "").strip()
    if len(draft) < 12 or len(draft) > MAX_BODY:
        raise ValueError("草稿必须为 12-12000 字")
    from .db import _lock
    with _lock:
        cur = db._conn.execute(
            "UPDATE leads SET draft=?,updated_at=? WHERE id=? AND status='awaiting_review'",
            (draft, now_iso(), lead_id))
        db._conn.commit()
    if cur.rowcount != 1:
        raise ValueError("只能修改待审核的草稿")
    return get_lead(db, lead_id)


def get_review_leads(db):
    return {r["approval_id"]: dict(r) for r in db.query(
        "SELECT t.approval_id,l.id,l.sender_email,l.subject,l.draft "
        "FROM leads l JOIN tasks t ON t.id=l.task_id "
        "WHERE t.approval_id IS NOT NULL AND l.status='awaiting_review'")}

def approve_draft(db, task_id, approved):
    stage = "ready_to_send" if approved else "lost"
    db.execute("UPDATE leads SET status=?,updated_at=? WHERE task_id=? AND status='awaiting_review'",
               (stage, now_iso(), task_id))


def mark_outcome(db, lead_id, status):
    if status not in ("won", "lost"):
        raise ValueError("unsupported outcome")
    lead = get_lead(db, lead_id)
    if not lead or lead["status"] not in ("sent", "won", "lost"):
        raise ValueError("仅已发送的询盘可以记录成交/流失")
    db.execute("UPDATE leads SET status=?,updated_at=? WHERE id=?",
               (status, now_iso(), lead_id))


def metrics(db):
    rows = db.query("SELECT status,COUNT(*) AS n FROM leads GROUP BY status")
    counts = {r["status"]: r["n"] for r in rows}
    total = sum(counts.values())
    sent = sum(counts.get(x, 0) for x in ("sent", "won", "lost"))
    won = counts.get("won", 0)
    return {"leads": total, "sent": sent, "won": won,
            "conversion_pct": round(100*won/sent, 1) if sent else None,
            "awaiting_review": counts.get("awaiting_review", 0),
            "ready_to_send": counts.get("ready_to_send", 0)}


def readiness():
    """Configuration-only checks, never label untested networks as healthy."""
    env = os.environ
    return {
        "inbound_webhook": bool(env.get("WANAI_INTAKE_SECRET")),
        "inbound_imap": all(env.get(k) for k in ("WANAI_IMAP_HOST","WANAI_IMAP_USER","WANAI_IMAP_PASSWORD")),
        "smtp": all(env.get(k) for k in ("WANAI_SMTP_HOST","WANAI_SMTP_USER","WANAI_SMTP_PASSWORD","WANAI_FROM_EMAIL")),
        "payment_link": bool(env.get("WANAI_CHECKOUT_URL")),
        "payment_webhook": bool(env.get("STRIPE_WEBHOOK_SECRET")),
        "admin_auth": bool(env.get("WANAI_ADMIN_PASSWORD")),
    }

def next_action(db):
    """One deterministic suggested action: prevent owner's decision overload."""
    setup = readiness()
    pulse = metrics(db)
    if not setup["admin_auth"]:
        return {"title": "先保护你的控制台", "why": "当前未配置管理密码；只有本地访问可用。",
                "url": "/setup"}
    if pulse["awaiting_review"]:
        return {"title": f"审核 {pulse['awaiting_review']} 条询盘草稿",
                "why": "真正能带来客户回复的动作，优先于继续生成 AI 报告。", "url": "/approvals"}
    if pulse["ready_to_send"]:
        return {"title": f"确认发送 {pulse['ready_to_send']} 条已审批回复",
                "why": "已经审核的草稿不会自动发送，请检查后逐封发出。", "url": "/leads"}
    if not setup["inbound_imap"] and not setup["inbound_webhook"] and not pulse["leads"]:
        return {"title": "接入第一条真实询盘", "why": "系统目前没有获授权的客户来信源。",
                "url": "/setup"}
    if not pulse["leads"]:
        return {"title": "寻找第一位愿意试用的客户", "why": "本阶段唯一目标是获取真实用户反馈，而不是增加 Agent。",
                "url": "/offer"}
    if not setup["smtp"]:
        return {"title": "配置安全的发送邮箱", "why": "已有询盘但不能交付跟进。",
                "url": "/setup"}
    if not setup["payment_webhook"]:
        return {"title": "完成真实收款验签", "why": "先测试小额付款，避免把模拟订单当实际收入。",
                "url": "/setup"}
    return {"title": "查看每周真实成交和人工成本", "why": "只保留提高回复质量、付费转化和净收款的实验。",
            "url": "/leads"}
