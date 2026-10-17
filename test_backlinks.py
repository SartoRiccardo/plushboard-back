"""Backlinks offline checks: §10.7 summary/timeseries parsing, §11 alert,
/api/backlinks composition. Run from backend/: .venv/bin/python test_backlinks.py
The DF HTTP layer is faked — ZERO real DataForSEO calls (it is a paid API)."""
import asyncio
import tempfile
from pathlib import Path

from app import db, engine
from app.sources import dataforseo as df

db.DB_PATH = Path(tempfile.mkdtemp()) / "test.db"
db.init_db()
with db.db() as c:
    engine.seed_settings(c)

CUR = {"path": None, "body": None, "resp": {}}


async def fake_post(path, body):
    CUR["path"], CUR["body"] = path, body
    return CUR["resp"]


df._post = fake_post


def run(coro):
    return asyncio.run(coro)


def rules():
    with db.db() as c:
        return [(r["rule"], r["severity"]) for r in
                c.execute("SELECT rule, severity FROM alerts")]


def alerts_payload(rule):
    import json
    with db.db() as c:
        return json.loads(c.execute(
            "SELECT payload FROM alerts WHERE rule=?", (rule,)
        ).fetchone()["payload"])


def last_run():
    with db.db() as c:
        return dict(c.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone())


def snap(metric):
    with db.db() as c:
        row = engine.latest(c, "dataforseo", metric, df.OUR_DOMAIN)
        return row["value"] if row else None


# --- summary: documented shape (referring_domains/backlinks), first-ever run ---
CUR["resp"] = {"status_code": 20000, "tasks": [{
    "cost": 0.02,
    "result": [{"target": "smallplushies.com", "referring_domains": 120,
                "backlinks": 4567}],
}]}
r = run(df.backlinks_summary())
assert CUR["path"] == "/backlinks/summary/live"
assert CUR["body"] == [{"target": "smallplushies.com"}]
assert r == {"refdomains": 120, "backlinks": 4567, "cost": 0.02}, r
assert snap("backlinks_refdomains") == 120
assert snap("backlinks_backlinks") == 4567
assert rules() == [], "first-ever run is baseline: no alert"
row = last_run()
assert row["source"] == "dataforseo" and row["kind"] == "paid"
assert row["status"] == "ok" and abs(row["cost"] - 0.02) < 1e-9

# --- unchanged: snapshot updated, no re-fire ---
CUR["resp"]["tasks"][0]["result"][0]["referring_domains"] = 120
r = run(df.backlinks_summary())
assert rules() == [], "unchanged count must not alert"
with db.db() as c:  # watermark still settable (MCP keeps it) — window deltas ignore it
    engine.mark_seen(c, "dataforseo")

# --- changed: backlinks_change fires once, info, payload old/new ---
CUR["resp"]["tasks"][0]["result"][0]["referring_domains"] = 123
r = run(df.backlinks_summary())
assert r["refdomains"] == 123
assert rules() == [("backlinks_change", "info")], rules()
p = alerts_payload("backlinks_change")
assert p["facts"] == {"old": 120, "new": 123}, p["facts"]
assert p["page"] == "/backlinks"
assert p["one_liner"] == "Backlinks: referring domains 120 -> 123."

# --- alternate shape: rd_count instead of referring_domains ---
CUR["resp"] = {"tasks": [{"cost": 0.02, "result": [
    {"rd_count": 130, "backlinks": 4600}]}]}
r = run(df.backlinks_summary())
assert r["refdomains"] == 130 and r["backlinks"] == 4600, r
assert snap("backlinks_refdomains") == 130

# --- junk response: error surfaced, cost still logged ---
CUR["resp"] = {"tasks": [{"cost": 0.02, "result": [{"target": "x"}]}]}
r = run(df.backlinks_summary())
assert "error" in r and abs(r["cost"] - 0.02) < 1e-9, r
assert last_run()["status"] == "ok"  # the call itself succeeded

# --- upstream failure: error run row ---
async def boom(path, body):
    raise RuntimeError("401 Unauthorized")

df._post = boom
r = run(df.backlinks_summary())
assert "error" in r and "401" in r["error"]
row = last_run()
assert row["status"] == "error" and "401" in row["error"] and row["cost"] == 0
df._post = fake_post

# --- timeseries: documented shape (new/lost_referring_domains), NO snapshots ---
CUR["resp"] = {"tasks": [{"cost": 0.02, "result": [{"items": [
    {"type": "backlinks_timeseries_new_lost_summary",
     "date": "2026-08-31 00:00:00 +00:00", "new_referring_domains": 5,
     "lost_referring_domains": 2},
    {"type": "backlinks_timeseries_new_lost_summary",
     "date": "2026-09-30 00:00:00 +00:00", "new_referring_domains": 3,
     "lost_referring_domains": 1},
]}]}]}
r = run(df.backlinks_timeseries("2026-09-01", "2026-09-30"))
assert CUR["path"] == "/backlinks/timeseries_new_lost_summary/live"
assert CUR["body"] == [{"target": "smallplushies.com",
                        "date_from": "2026-09-01", "date_to": "2026-09-30"}]
assert r == {"series": [
    {"date": "2026-08-31 00:00:00 +00:00", "new_refdomains": 5,
     "lost_refdomains": 2},
    {"date": "2026-09-30 00:00:00 +00:00", "new_refdomains": 3,
     "lost_refdomains": 1},
], "cost": 0.02}, r
with db.db() as c:
    assert c.execute("SELECT 1 FROM snapshots WHERE metric LIKE 'backlinks_%'"
                     " AND value NOT IN (120, 123, 130, 4567, 4600)"
                     ).fetchone() is None  # timeseries never snapshotted

# --- alternate shape: only new/lost_backlinks present -> used in their place ---
CUR["resp"] = {"tasks": [{"cost": 0.02, "result": [{"items": [
    {"date": "2026-09-30 00:00:00 +00:00", "new_backlinks": 248,
     "lost_backlinks": 173},
]}]}]}
r = run(df.backlinks_timeseries("2026-09-01", "2026-09-30"))
assert r["series"] == [{"date": "2026-09-30 00:00:00 +00:00",
                        "new_refdomains": 248, "lost_refdomains": 173}], r

# --- timeseries failure ---
df._post = boom
r = run(df.backlinks_timeseries("2026-09-01", "2026-09-30"))
assert "error" in r
df._post = fake_post

# --- overview card: refdomains line appended, other lines untouched ---
# (fresh db: no rank snapshots -> "not checked"; every snapshot here is hours
#  old, so the 7d window has no baseline and the refdomains line reads "(new)")
with db.db() as c:
    card = df.card(c, 7)
assert card["headline"] == "11 tracked keywords"
assert card["lines"][-1] == "refdomains: 130 (new)", card["lines"]
assert card["lines"][:-1] == ["touhou plush: not checked",
                              "touhou plushies: not checked",
                              "touhou project plush: not checked"]

# --- /api/backlinks composition (through the real router) ---
from fastapi.testclient import TestClient

from app.auth import serializer
from app.main import app

cookie = {"plush_session": serializer.dumps({"email": "rikki.sarto@gmail.com"})}
with TestClient(app) as tc:
    tc.cookies.update(cookie)

    # summary button route (faked upstream) + 502 path
    CUR["resp"] = {"tasks": [{"cost": 0.02, "result": [
        {"referring_domains": 131, "backlinks": 4700}]}]}
    r = tc.post("/api/backlinks/summary")
    assert r.status_code == 200 and r.json()["refdomains"] == 131, r.text
    df._post = boom
    r = tc.post("/api/backlinks/summary")
    assert r.status_code == 502, r.status_code
    df._post = fake_post

    # timeseries route: 400 without body fields (no upstream call), 200 when set
    CUR["path"] = None
    r = tc.post("/api/backlinks/timeseries", json={"from": "2026-09-01"})
    assert r.status_code == 400 and CUR["path"] is None, (r.status_code, CUR)
    r = tc.post("/api/backlinks/timeseries", json={})
    assert r.status_code == 400
    CUR["resp"] = {"tasks": [{"cost": 0.02, "result": [{"items": [
        {"date": "2026-09-30 00:00:00 +00:00", "new_backlinks": 248,
         "lost_backlinks": 173},
    ]}]}]}
    r = tc.post("/api/backlinks/timeseries",
                json={"from": "2026-09-01", "to": "2026-09-30"})
    assert r.status_code == 200 and r.json()["series"][0]["new_refdomains"] == 248

    # composition: snapshot fields + null series + manual backlink rows
    # (all snapshots are minutes old — the 7d window has no baseline yet, so
    # every delta is None and everything reads as new)
    with db.db() as c:
        c.execute("INSERT INTO manual_log(kind, value, note, ts)"
                  " VALUES('backlink', NULL, 'discord drop', '2026-09-30T10:00:00+00:00')")
        mid = c.execute("SELECT id FROM manual_log WHERE kind='backlink'"
                        ).fetchone()["id"]
    r = tc.get("/api/backlinks").json()
    assert r["refdomains"] == 131 and r["backlinks"] == 4700, r
    assert r["refdomains_delta"] is None and r["refdomains_new"] is True, r
    assert r["backlinks_delta"] is None, r
    assert r["series"] is None
    assert r["manual"] == [{"id": mid, "note": "discord drop", "value": None,
                            "ts": "2026-09-30T10:00:00+00:00"}], r["manual"]
    with db.db() as c:  # then remove it
        c.execute("DELETE FROM manual_log WHERE id=?", (mid,))
    assert tc.get("/api/backlinks").json()["manual"] == []

    # paid guard still holds on the generic route
    assert tc.post("/api/dataforseo/refresh").status_code == 409

print("backlinks ok")
