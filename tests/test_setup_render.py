"""Browser regression guard for a safe, truly visible first-run wizard."""
import base64
from fastapi.testclient import TestClient
from app.main import create_app

def test_setup_wizard_is_rendered_only_inside_content_block(tmp_path, monkeypatch):
    monkeypatch.setenv("WANAI_ADMIN_PASSWORD", "test-password")
    client = TestClient(create_app(tmp_path / "setup.db"))
    cred = base64.b64encode(b"admin:test-password").decode()
    r = client.get("/setup", headers={"Authorization": "Basic " + cred})
    assert r.status_code == 200
    assert "<title>首次配置 | Wan's AI</title>" in r.text
    assert 'action="/setup/email"' in r.text
    assert 'action="/setup/payments"' in r.text
    assert 'action="/setup/connectors"' not in r.text

def test_research_report_html_bleached(tmp_path):
    import bleach
    raw = '<img src=x onerror="alert(1)"><a href="javascript:alert(1)">click</a>'
    clean = bleach.clean(raw, tags=["a"], attributes={"a":["href"]}, protocols=["http","https"], strip=True)
    assert "<img" not in clean and "javascript:" not in clean
