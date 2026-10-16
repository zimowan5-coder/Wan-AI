"""Safe single-operator settings wizard for nontechnical owners.

Values are written to local .env using python-dotenv, never stored in SQLite
or rendered back in HTML. Blank fields never wipe existing credentials.
"""
import os
import re
from pathlib import Path
from threading import RLock

from dotenv import set_key
from .revenue import clean_email

_LOCK = RLock()
_ALLOWED = {
    "WANAI_IMAP_HOST", "WANAI_IMAP_PORT", "WANAI_IMAP_USER",
    "WANAI_IMAP_PASSWORD", "WANAI_SMTP_HOST", "WANAI_SMTP_PORT",
    "WANAI_SMTP_USER", "WANAI_SMTP_PASSWORD", "WANAI_FROM_EMAIL",
    "WANAI_CHECKOUT_URL", "STRIPE_WEBHOOK_SECRET", "WANAI_INTAKE_SECRET",
}


def _validate(key, raw):
    val = (raw or "").strip()
    if not val:
        return ""
    if "\x00" in val or "\r" in val or "\n" in val or len(val) > 1024:
        raise ValueError(f"{key} 含非法字符或长度超限")
    if key.endswith("_HOST") and not re.fullmatch(r"[A-Za-z0-9.-]{2,255}", val):
        raise ValueError(f"{key} 必须为合法服务器域名/IP，而非 URL")
    if key.endswith("_PORT") and (not val.isdecimal() or not 1 <= int(val) <= 65535):
        raise ValueError(f"{key} 端口须为 1-65535")
    if key.endswith("_USER") or key == "WANAI_FROM_EMAIL":
        if not clean_email(val):
            raise ValueError(f"{key} 邮箱格式不正确")
    if key == "WANAI_CHECKOUT_URL":
        if not val.startswith(("https://buy.stripe.com/", "https://checkout.stripe.com/")):
            raise ValueError("只能填写 Stripe 官方 HTTPS 付款链接")
    if key == "STRIPE_WEBHOOK_SECRET" and not val.startswith("whsec_"):
        raise ValueError("Stripe Webhook 密钥应以 whsec_ 开头")
    if key == "WANAI_INTAKE_SECRET" and len(val) < 24:
        raise ValueError("表单签名密钥应至少为 24 字符的随机字符串")
    return val


def save_options(options, env_path):
    unknown = set(options) - _ALLOWED
    if unknown:
        raise ValueError("不允许修改其他系统密钥")
    # Validate all before touching the env file, preventing partial invalid writes.
    validated = {k: _validate(k, v) for k, v in options.items()}
    validated = {k: v for k, v in validated.items() if v}
    path = Path(env_path)
    if not path.is_file():
        raise ValueError("配置文件不存在。请先通过正常启动脚本初始化项目。")
    with _LOCK:
        for key, val in validated.items():
            set_key(str(path), key, val, quote_mode="always")
            os.environ[key] = val
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return tuple(sorted(validated))
