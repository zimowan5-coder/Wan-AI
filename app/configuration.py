"""Constrained, owner-authenticated setup for non-programmers.

Never accept arbitrary env var names, never render existing secret values.
Configuration changes persist to the local .env file; back it up securely.
"""
import os
import re
import threading
from pathlib import Path
from urllib.parse import urlparse

from dotenv import set_key
from .revenue import clean_email

_CONFIG_LOCK = threading.Lock()
ALLOWED = (
    "WANAI_IMAP_HOST", "WANAI_IMAP_USER", "WANAI_IMAP_PASSWORD",
    "WANAI_SMTP_HOST", "WANAI_SMTP_USER", "WANAI_SMTP_PASSWORD",
    "WANAI_FROM_EMAIL", "WANAI_CHECKOUT_URL", "STRIPE_WEBHOOK_SECRET",
)
DOMAINS = re.compile(r"^(?=.{4,253}$)[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)+$")


def configure(root, **values):
    changed = []
    checked = {}
    for key in ALLOWED:
        raw = str(values.get(key) or "").strip()
        if not raw:
            continue  # Leave previously saved secret alone.
        if len(raw) > 1024 or "\n" in raw or "\r" in raw or "\x00" in raw:
            raise ValueError("配置值格式或长度不合法")
        if key.endswith("_HOST"):
            if not DOMAINS.fullmatch(raw) or raw.lower().endswith((".local", ".internal")):
                raise ValueError("邮箱服务器必须是有效的公网域名")
        if key in ("WANAI_IMAP_USER", "WANAI_SMTP_USER", "WANAI_FROM_EMAIL"):
            if not clean_email(raw):
                raise ValueError("邮箱地址格式错误")
        if key == "WANAI_CHECKOUT_URL":
            parsed = urlparse(raw)
            if parsed.scheme != "https" or parsed.hostname not in ("buy.stripe.com","checkout.stripe.com"):
                raise ValueError("付款链接必须来自 Stripe 官方 HTTPS 地址")
        if key == "STRIPE_WEBHOOK_SECRET" and not raw.startswith("whsec_"):
            raise ValueError("Stripe Webhook 密钥一般以 whsec_ 开头")
        checked[key] = raw
    path = Path(root) / ".env"
    if not path.is_file():
        raise ValueError("请先运行脚本自动创建 .env")
    with _CONFIG_LOCK:
        for key, value in checked.items():
            set_key(str(path), key, value, quote_mode="always")
            os.environ[key] = value
            changed.append(key)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return changed
