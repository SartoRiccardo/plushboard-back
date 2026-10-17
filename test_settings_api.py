"""Settings/maintenance routes check. Run from backend/:
.venv/bin/python test_settings_api.py"""
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from app import db, engine
from app.auth import serializer
from app.main import app

db.DB_PATH = Path(tempfile.mkdtemp()) / "test.db"
db.init_db()

cookie = {"plush_session": serializer.dumps({"email": "rikki.sarto@gmail.com"})}

with TestClient(app) as c:
    # GET: settings + credentials shape (no values, ever)
    r = c.get("/api/settings", cookies=cookie)
    assert r.status_code == 200
    body = r.json()
    assert set(body["settings"]) == set(engine.SEEDS)
    assert all(set(x) == {"var", "set"} for x in body["credentials"])
    assert all(isinstance(x["set"], bool) for x in body["credentials"])
    assert "PLAUSIBLE_KEY" in {x["var"] for x in body["credentials"]}

    # PUT: validation
    assert c.put("/api/settings", json={"k": "bogus", "v": 1}, cookies=cookie).status_code == 400
    assert c.put("/api/settings", json={"k": "retention_days", "v": -1}, cookies=cookie).status_code == 400
    assert c.put("/api/settings", json={"k": "intervals", "v": {"plausible": -5}}, cookies=cookie).status_code == 400
    assert c.put("/api/settings", json={"k": "enabled", "v": {"gsc": "yes"}}, cookies=cookie).status_code == 400
    r = c.put("/api/settings", json={"k": "retention_days", "v": 90}, cookies=cookie)
    assert r.status_code == 200 and r.json()["value"] == 90

    # purge: old row deleted, fresh row stays
    with db.db() as conn:
        old = "2020-01-01T00:00:00.000000+00:00"
        engine.record(conn, "plausible", "visitors_daily", "2020-01-01", 1)
        conn.execute("UPDATE snapshots SET fetched_at=? WHERE key='2020-01-01'", (old,))
        engine.record(conn, "plausible", "visitors_daily", "2026-10-01", 5)
    n = c.post("/api/settings/purge", cookies=cookie).json()["deleted"]
    assert n >= 1
    with db.db() as conn:
        keys = {r["key"] for r in conn.execute("SELECT key FROM snapshots")}
    assert "2020-01-01" not in keys and "2026-10-01" in keys

    # wipe: typed confirmation required; settings survive, watermarks gone
    assert c.post("/api/settings/wipe", json={"confirm": "no"}, cookies=cookie).status_code == 400
    with db.db() as conn:
        engine.mark_seen(conn, "plausible")
    assert c.post("/api/settings/wipe", json={"confirm": "wipe"}, cookies=cookie).status_code == 200
    with db.db() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM snapshots").fetchone()["n"] == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM watermarks").fetchone()["n"] == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM settings").fetchone()["n"] > 0

    # export: csv header + 400 on bogus table
    with db.db() as conn:
        engine.record(conn, "plausible", "visitors_daily", "2026-10-01", 5)
    r = c.get("/api/export?table=snapshots", cookies=cookie)
    assert r.status_code == 200 and r.text.splitlines()[0].startswith("id,")
    assert "visitors_daily" in r.text
    assert c.get("/api/export?table=users", cookies=cookie).status_code == 400

print("settings api ok")
