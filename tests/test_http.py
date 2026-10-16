"""API smoke test: public storefront only; private owner actions require auth and origin."""
import base64
from fastapi.testclient import TestClient
from app.main import create_app

def client(tmp_path, monkeypatch):
    monkeypatch.setenv("WANAI_ADMIN_PASSWORD", "correct-horse-battery-staple")
    monkeypatch.delenv("WANAI_CHECKOUT_URL", raising=False)
    return TestClient(create_app(tmp_path / "http.db"))

def authorization(password="correct-horse-battery-staple"):
    token = base64.b64encode(("admin:" + password).encode()).decode()
    return {"Authorization": "Basic " + token}

def test_public_offer_and_private_leads(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    assert c.get("/healthz").status_code == 200
    offer = c.get("/offer")
    assert offer.status_code == 200
    assert "Stripe" not in offer.text or "付款" in offer.text
    assert c.get("/leads").status_code == 401
    assert c.get("/leads", headers=authorization("wrong")).status_code == 401
    response = c.get("/leads", headers=authorization())
    assert response.status_code == 200
    assert "真实询盘" in response.text

def test_origin_protected_mutation(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    payload = {"sender_email": "buyer@example.com", "text": "Need 1000 pcs quote"}
    assert c.post("/leads", data=payload, headers=authorization(), follow_redirects=False).status_code == 403
    ok = c.post("/leads", data=payload,
                headers={**authorization(), "Origin": "http://testserver"}, follow_redirects=False)
    assert ok.status_code == 303
    assert ok.headers["location"].startswith("/leads/")
    assert c.get(ok.headers["location"], headers=authorization()).status_code == 200

def test_unsigned_intake_rejected(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    monkeypatch.setenv("WANAI_INTAKE_SECRET", "unit-secret")
    assert c.post("/api/intake", json={"email":"buyer@example.com", "message":"hello world"}).status_code == 401
