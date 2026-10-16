"""Nontechnical setup never echoes existing passwords and never overwrites blank keys."""
from pathlib import Path
import pytest
from app.settings_ui import save_options


def test_valid_email_settings_and_blank_preserved(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    p.write_text("WANAI_IMAP_HOST=old.example.com\n", encoding="utf-8")
    saved = save_options({"WANAI_IMAP_HOST": "imap.example.com",
        "WANAI_IMAP_PORT": "993", "WANAI_SMTP_PASSWORD": "app-password"}, p)
    assert "WANAI_IMAP_HOST" in saved
    save_options({"WANAI_IMAP_HOST": "", "WANAI_SMTP_HOST": "smtp.example.com"}, p)
    s = p.read_text(encoding="utf-8")
    assert "imap.example.com" in s
    assert "smtp.example.com" in s
    assert "app-password" in s
    assert "old.example.com" not in s


def test_settings_reject_unsafe_values_without_partial_write(tmp_path):
    p = tmp_path / ".env"
    p.write_text("# safe\n", encoding="utf-8")
    with pytest.raises(ValueError):
        save_options({"WANAI_SMTP_HOST": "smtp.example.com",
                      "WANAI_SMTP_PASSWORD": "bad\ninjection"}, p)
    assert p.read_text() == "# safe\n"
    with pytest.raises(ValueError):
        save_options({"WANAI_CHECKOUT_URL": "https://evil.example/pay"}, p)
