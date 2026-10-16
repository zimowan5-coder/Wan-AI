"""Protect owner console, reject CSRF, require signed external ingress."""
import base64
import hashlib
import hmac
import os
from urllib.parse import urlsplit

from fastapi.responses import PlainTextResponse

PUBLIC_ENDPOINTS = {"/healthz", "/api/intake", "/api/billing/stripe"}


def check_signature(secret, raw, presented):
    if not secret or not presented:
        return False
    token = presented.removeprefix("sha256=").strip().lower()
    if len(token) != 64:
        return False
    expected = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, token)


async def owner_guard(request, call_next):
    path = request.url.path
    if path in PUBLIC_ENDPOINTS or path.startswith("/static/"):
        return await call_next(request)
    secret = os.environ.get("WANAI_ADMIN_PASSWORD", "")
    if secret:
        raw = request.headers.get("authorization", "")
        try:
            scheme, encoded = raw.split(" ", 1)
            decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
            username, password = decoded.split(":", 1)
            ok = (scheme.lower() == "basic" and username == "admin"
                  and hmac.compare_digest(password, secret))
        except (ValueError, UnicodeError):
            ok = False
        if not ok:
            return PlainTextResponse("请使用 admin 和你设置的 WANAI_ADMIN_PASSWORD 登录",
                                     status_code=401,
                                     headers={"WWW-Authenticate": 'Basic realm="WanAI Owner"'})
    else:
        # No password: only loopback is allowed, NEVER expose a private business console.
        host = request.client.host if request.client else ""
        if host not in ("127.0.0.1", "::1", "localhost"):
            return PlainTextResponse("未配置管理密码，禁止远程访问", status_code=403)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin:
            parsed = urlsplit(origin)
            if parsed.netloc.lower() != request.headers.get("host", "").lower():
                return PlainTextResponse("跨站写入已拒绝", status_code=403)
        if request.headers.get("sec-fetch-site") == "cross-site":
            return PlainTextResponse("跨站写入已拒绝", status_code=403)
    return await call_next(request)
