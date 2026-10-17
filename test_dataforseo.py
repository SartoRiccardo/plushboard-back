"""DataForSEO offline checks: §5.5 parsing/cost, §10.6 snapshots, §11 alert.
Run from backend/: .venv/bin/python test_dataforseo.py
The HTTP layer is faked — ZERO real DataForSEO calls (it is a paid API)."""
import asyncio
import tempfile
from pathlib import Path

from app import db, engine
from app.sources import SOURCES, dataforseo as df

db.DB_PATH = Path(tempfile.mkdtemp()) / "test.db"
db.init_db()
with db.db() as c:
    engine.seed_settings(c)

CUR = {"path": None, "body": None, "resp": {}}


async def fake_post(path, body):
    CUR["path"], CUR["body"] = path, body
    return CUR["resp"]


df._post = fake_post


def organic(pos, domain):
    return {"type": "organic", "rank_group": pos, "rank_absolute": pos,
            "domain": domain, "url": f"https://{domain}/x"}


def resp(items, task_costs=(0.002,)):
    return {"status_code": 20000, "cost": sum(task_costs),
            "tasks": [{"status_code": 20000, "cost": cost,
                       "result": [{"items": items}]} for cost in task_costs]}


def run(coro):
    return asyncio.run(coro)


def rules():
    with db.db() as c:
        return [r["rule"] for r in c.execute("SELECT rule FROM alerts")]


def last_run():
    with db.db() as c:
        return dict(c.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone())


# registry: paid source is never auto-refreshed, scheduler + refresh guard skip it
src = SOURCES["dataforseo"]
assert src.free is False and src.refresh is None and src.view is None
assert src.card is not None

# --- serp: our domain at position 4, cost summed over tasks ---
items = [organic(1, "etsy.com"), organic(2, "amazon.com"), organic(3, "reddit.com"),
         organic(4, "smallplushies.com"), organic(5, "etsy.com"),
         {"type": "featured_snippet", "rank_absolute": 1, "domain": "wikipedia.org"}]
CUR["resp"] = resp(items, task_costs=(0.002, 0.003))
r = run(df.serp("touhou plush"))
assert CUR["path"] == "/serp/google/organic/live/advanced"
assert CUR["body"] == [{"keyword": "touhou plush", "location_code": 2840,
                        "language_code": "en", "depth": 10}]
assert abs(r["cost"] - 0.005) < 1e-9, r["cost"]
assert r["our_rank"] == 4 and r["rank"] == 4
assert r["top_domains"] == ["etsy.com", "amazon.com", "reddit.com"], r["top_domains"]
assert r["features"] == ["featured_snippet"]
with db.db() as c:
    row = engine.latest(c, "dataforseo", "df_keyword_rank", "touhou plush")
    assert row["value"] == 4
    assert engine.latest(c, "dataforseo", "df_keyword_difficulty", "touhou plush") is None
run_row = last_run()
assert run_row["source"] == "dataforseo" and run_row["kind"] == "paid"
assert run_row["status"] == "ok" and abs(run_row["cost"] - 0.005) < 1e-9

# baseline -> no alert yet
assert rules() == []

# --- rank change: |Δ|=2 must NOT fire ---
CUR["resp"] = resp([organic(p, d) for p, d in
                    [(1, "etsy.com"), (2, "smallplushies.com"), (3, "amazon.com")]])
r = run(df.serp("touhou plush"))
assert r["rank"] == 2
assert rules() == [], "Δ=2 must not alert"

# --- |Δ|=3 exactly: fires once, payload has keyword/old/new ---
CUR["resp"] = resp([organic(p, d) for p, d in
                    [(1, "etsy.com"), (2, "amazon.com"), (3, "reddit.com"),
                     (4, "wikipedia.org"), (5, "smallplushies.com")]])
r = run(df.serp("touhou plush"))
assert r["rank"] == 5
assert rules() == ["df_rank_change"]
with db.db() as c:
    p = __import__("json").loads(
        c.execute("SELECT payload FROM alerts WHERE rule='df_rank_change'")
        .fetchone()["payload"])
assert p["facts"] == {"keyword": "touhou plush", "old": 2, "new": 5}, p["facts"]
assert p["page"] == "/keywords"

# repeat unchanged -> no re-fire
CUR["resp"] = resp([organic(p, d) for p, d in
                    [(1, "etsy.com"), (2, "amazon.com"), (3, "reddit.com"),
                     (4, "wikipedia.org"), (5, "smallplushies.com")]])
run(df.serp("touhou plush"))
assert rules() == ["df_rank_change"]

# --- absent from top-10: stored 0; 5 -> 0 is Δ=5 -> fires ---
CUR["resp"] = resp([organic(p, d) for p, d in
                    [(1, "etsy.com"), (2, "amazon.com"), (3, "reddit.com")]])
r = run(df.serp("touhou plush"))
assert r["our_rank"] is None and r["rank"] == 0, r
with db.db() as c:
    assert engine.latest(c, "dataforseo", "df_keyword_rank", "touhou plush")["value"] == 0
assert rules() == ["df_rank_change", "df_rank_change"]

# --- cost fallback when tasks carry no cost field: 0.005 per task ---
assert abs(df._cost({"tasks": [{}, {}]}) - 0.01) < 1e-9
assert df._cost({"tasks": []}) == 0.0
assert abs(df._cost({"tasks": [{"cost": 0.0}, {"cost": 0.0015}]}) - 0.0015) < 1e-9

# --- upstream failure: error run row persists (commits despite failure) ---
async def boom(path, body):
    raise RuntimeError("401 Unauthorized")

df._post = boom
r = run(df.serp("touhou plush"))
assert "error" in r and "401" in r["error"]
row = last_run()
assert row["status"] == "error" and "401" in row["error"] and row["cost"] == 0
df._post = fake_post

# --- difficulty: parsed + snapshotted per keyword ---
CUR["resp"] = {"tasks": [{"cost": 0.002, "result": [{"items": [
    {"keyword": "touhou plush", "keyword_difficulty": 31},
    {"keyword": "nareko plush", "keyword_difficulty": 12},
    {"keyword": "cirno plush"},
]}]}]}
r = run(df.difficulty(["touhou plush", "nareko plush", "cirno plush"]))
assert CUR["path"] == "/dataforseo_labs/google/bulk_keyword_difficulty/live"
assert r == {"difficulties": {"touhou plush": 31, "nareko plush": 12},
             "cost": 0.002}
with db.db() as c:
    assert engine.latest(c, "dataforseo", "df_keyword_difficulty",
                         "nareko plush")["value"] == 12
    assert engine.latest(c, "dataforseo", "df_keyword_difficulty",
                         "cirno plush") is None

# --- ideas: normalized, nulls skipped, NO snapshots ---
CUR["resp"] = {"tasks": [{"cost": 0.002, "result": [{"items": [
    {"keyword": "touhou plush bulk", "search_volume": 90, "competition": 0.21},
    {"keyword": "touhou fumo", "search_volume": None, "competition_level": "LOW"},
    {"keyword": None, "search_volume": 10},
]}]}]}
r = run(df.ideas("touhou plush"))
assert CUR["path"] == "/dataforseo_labs/google/keyword_ideas/live"
assert r == {"cost": 0.002, "ideas": [
    {"keyword": "touhou plush bulk", "volume": 90, "competition": 0.21},
    {"keyword": "touhou fumo", "competition": "LOW"},
]}, r
with db.db() as c:
    assert c.execute("SELECT 1 FROM snapshots WHERE metric LIKE 'df_ideas%'"
                     ).fetchone() is None

# --- card: tracked count + rank/not-checked lines ---
with db.db() as c:
    card = df.card(c, 7)
assert card["source"] == "dataforseo" and card["label"] == "Keywords"
assert card["page"] == "/keywords" and card["og"] == "https://app.dataforseo.com/"
assert card["headline"] == "11 tracked keywords" and card["delta_new"] is False
assert card["lines"] == ["touhou plush: not in top 10",
                         "touhou plushies: not checked",
                         "touhou project plush: not checked"], card["lines"]

print("dataforseo ok")
