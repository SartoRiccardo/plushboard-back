"""Auth-surface smoke check. Run from backend/: .venv/bin/python test_smoke.py"""
from fastapi.testclient import TestClient

from app.auth import serializer
from app.main import app

with TestClient(app) as c:
    # unauthenticated: API 401, pages redirect to the login screen
    assert c.get("/api/me").status_code == 401
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"].endswith("/auth/login")

    # /auth/login serves the SPA screen (no OAuth redirect loop)
    r = c.get("/auth/login", follow_redirects=False)
    assert r.status_code == 200

    # /auth/google builds the Google consent redirect with a signed state
    r = c.get("/api/auth/google", follow_redirects=False)
    assert r.status_code == 307 and "accounts.google.com" in r.headers["location"]
    assert "state=" in r.headers["location"]

    # garbage oauth state rejected
    assert c.get("/api/auth/callback?code=x&state=bogus").status_code == 400

    # valid session cookie: API + SPA fallback both open
    cookie = serializer.dumps({"email": "rikki.sarto@gmail.com"})
    assert c.get("/api/me", cookies={"plush_session": cookie}).json() == {
        "email": "rikki.sarto@gmail.com"
    }
    assert c.get("/traffic", cookies={"plush_session": cookie}).status_code == 200

    # tampered cookie rejected
    assert c.get("/api/me", cookies={"plush_session": cookie + "x"}).status_code == 401

    # dev bypass: advertised + working only when APP_ENV=local, refused otherwise
    from app import config

    assert c.get("/api/auth/mode").json()["dev_bypass"] is (config.APP_ENV == "local")
    saved_env = config.APP_ENV
    config.APP_ENV = "prod"
    assert c.get("/api/auth/mode").json() == {"dev_bypass": False}
    assert c.post("/api/auth/dev-login", follow_redirects=False).status_code == 404
    config.APP_ENV = "local"
    r = c.post("/api/auth/dev-login", follow_redirects=False)
    assert r.status_code == 200 and "plush_session" in r.headers.get("set-cookie", "")
    config.APP_ENV = saved_env

print("smoke ok")
