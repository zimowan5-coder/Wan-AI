"""Safe, opt-in inbox polling and operator-approved SMTP replies."""
import imaplib
import os
import hashlib
import smtplib
from email import message_from_bytes
from email.message import EmailMessage
from email.header import decode_header, make_header
from email.utils import parseaddr, make_msgid

from . import revenue
from .db import now_iso


def _decoded(value):
    try:
        return str(make_header(decode_header(value or "")))[:250]
    except (UnicodeError, LookupError):
        return ""


def poll_inbox(db, limit=25):
    """Read-only IMAP using UIDs. Repeated polling is safe; messages remain unread."""
    env = os.environ
    if not all(env.get(k) for k in ("WANAI_IMAP_HOST","WANAI_IMAP_USER","WANAI_IMAP_PASSWORD")):
        return {"enabled": False, "new": 0}
    port = int(env.get("WANAI_IMAP_PORT", "993"))
    # Require TLS, never downgrade or call arbitrary IMAP from lead data.
    count = 0
    mailbox = imaplib.IMAP4_SSL(env["WANAI_IMAP_HOST"], port, timeout=20)
    try:
        mailbox.login(env["WANAI_IMAP_USER"], env["WANAI_IMAP_PASSWORD"])
        mailbox.select("INBOX", readonly=True)
        status, data = mailbox.uid("search", None, "UNSEEN")
        if status != "OK":
            raise RuntimeError("IMAP search failed")
        _, validity_data = mailbox.response("UIDVALIDITY")
        validity = (validity_data[0] or b"unknown").decode("ascii", errors="ignore") if validity_data else "unknown"
        for uid in (data[0] or b"").split():
            if count >= max(1, min(limit, 100)):
                break
            external_id = f"{env['WANAI_IMAP_USER']}:{validity}:{uid.decode('ascii','ignore')}"
            key = hashlib.sha256(("imap\0" + external_id).encode()).hexdigest()
            if db.query_one("SELECT id FROM leads WHERE external_key=?", (key,)):
                continue
            status, parts = mailbox.uid("fetch", uid, "(BODY.PEEK[])")
            if status != "OK":
                continue
            blob = next((p[1] for p in parts if isinstance(p, tuple)), None)
            if not blob:
                continue
            msg = message_from_bytes(blob)
            if msg.get("Auto-Submitted", "no").lower() != "no" or msg.get("List-Unsubscribe"):
                continue
            address = revenue.clean_email(parseaddr(msg.get("Reply-To") or msg.get("From") or "")[1])
            if not address or address.lower() == env.get("WANAI_IMAP_USER", "").lower():
                continue
            body = ""
            for part in msg.walk():
                if part.get_content_type() == "text/plain" and not part.get_filename():
                    raw = part.get_payload(decode=True) or b""
                    body = raw[:24_000].decode(part.get_content_charset() or "utf-8", errors="replace")
                    break
            if not body:
                continue
            subject = _decoded(msg.get("Subject"))
            _, inserted = revenue.capture(
                db, source="imap",
                external_id=external_id,
                sender_email=address, text=body, subject=subject,
                sender_name=_decoded(parseaddr(msg.get("From") or "")[0]))
            count += int(inserted)
    finally:
        try:
            mailbox.logout()
        except Exception:
            pass
    return {"enabled": True, "new": count}


def send_approved(db, lead_id):
    """Explicit owner action only. SMTP TLS, fail-closed and CAS send claim."""
    from .db import _lock
    env = os.environ
    required = ("WANAI_SMTP_HOST", "WANAI_SMTP_USER", "WANAI_SMTP_PASSWORD", "WANAI_FROM_EMAIL")
    if not all(env.get(k) for k in required):
        raise ValueError("SMTP 未配置；尚未发送")
    sender = revenue.clean_email(env["WANAI_FROM_EMAIL"])
    if not sender:
        raise ValueError("发件地址不合法")
    with _lock:
        row = db._conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
        if not row or row["status"] != "ready_to_send" or not row["draft"]:
            raise ValueError("只能发送已审批且未发送的回复")
        cur = db._conn.execute(
            "UPDATE leads SET status='sending',updated_at=? WHERE id=? AND status='ready_to_send'",
            (now_iso(), lead_id))
        db._conn.commit()
        if cur.rowcount != 1:
            raise ValueError("询盘已由其他操作处理")
        recipient, draft, subject = row["sender_email"], row["draft"], row["subject"]
    email = EmailMessage()
    email["From"] = sender
    email["To"] = recipient
    email["Subject"] = ("Re: " + subject)[:250] if subject else "Re: Your inquiry"
    email["Message-ID"] = make_msgid(domain=sender.split("@")[-1])
    email.set_content(draft)
    try:
        with smtplib.SMTP(env["WANAI_SMTP_HOST"], int(env.get("WANAI_SMTP_PORT", "587")), timeout=20) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(env["WANAI_SMTP_USER"], env["WANAI_SMTP_PASSWORD"])
            server.send_message(email)
    except Exception:
        # SMTP may deliver before failing. Keep ambiguous sends quarantined to prevent duplicate email.
        db.execute("UPDATE leads SET status='failed',updated_at=? WHERE id=? AND status='sending'",
                   (now_iso(), lead_id))
        raise
    db.execute("UPDATE leads SET status='sent',sent_at=?,updated_at=? WHERE id=? AND status='sending'",
               (now_iso(), now_iso(), lead_id))
    return email["Message-ID"] or "sent"
