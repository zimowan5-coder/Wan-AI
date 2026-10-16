"""Local one-time setup: creates a strong owner password without third-party services."""
import re
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
env_path = ROOT / ".env"
if not env_path.exists():
    env_path.write_text((ROOT / ".env.example").read_text(encoding="utf-8"), encoding="utf-8")
text = env_path.read_text(encoding="utf-8")
match = re.search(r"(?m)^WANAI_ADMIN_PASSWORD=(.*)$", text)
if match is None:
    text += "\nWANAI_ADMIN_PASSWORD=\n"
    match = re.search(r"(?m)^WANAI_ADMIN_PASSWORD=(.*)$", text)
if not match.group(1).strip():
    password = secrets.token_urlsafe(24)
    text = re.sub(r"(?m)^WANAI_ADMIN_PASSWORD=.*$", "WANAI_ADMIN_PASSWORD=" + password, text)
    env_path.write_text(text, encoding="utf-8")
    print("\n" + "=" * 55)
    print("首次设置成功！管理员用户名：admin")
    print("管理员密码：" + password)
    print("请立即保管好此密码。下次启动不会再自动打印。")
    print("=" * 55 + "\n")
else:
    print("安全配置已存在：使用用户名 admin 和你设置的 .env 密码登录。")
