"""GIPHY offline checks: §5.4 saturation/indexing + §11 alert transitions.
Run from backend/: .venv/bin/python test_giphy.py
Upstream calls are faked — hits no network, needs no credentials."""
import asyncio
import tempfile
from pathlib import Path

from app import db, engine
from app.sources import giphy

db.DB_PATH = Path(tempfile.mkdtemp()) / "test.db"
db.init_db()
with db.db() as c:
    engine.seed_settings(c)
    engine.set_setting(c, "giphy_tags", ["nareko", "dissolve", "touhou"])

CUR = {"tags": {}, "chan": 0, "gifs": []}


async def fake_tag_count(tag):
    return CUR["tags"][tag]


async def fake_channel_count():
    return CUR["chan"]


async def fake_channel_gifs():
    return CUR["gifs"]


giphy.tag_count = fake_tag_count
giphy.channel_count = fake_channel_count
giphy.channel_gifs = fake_channel_gifs


async def _rules():
    with db.db() as c:
        return [r["rule"] for r in c.execute("SELECT rule FROM alerts")]


def once():
    return asyncio.run(engine.refresh_source("giphy"))


def view():
    with db.db() as c:
        return asyncio.run(giphy.view(c))


def card():
    with db.db() as c:
        return giphy.card(c, 7)


# never refreshed: view is empty, card says so
v = view()
assert v["status"] == "never"
assert v["channel"] == {"indexed": False, "channel_count": 0, "gifs": []}
assert v["tags"] == []
assert card()["headline"] == "never fetched"

# baseline: everything "new", no alerts; saturation flag at the 500 cap
CUR["tags"] = {"nareko": 46, "dissolve": 158, "touhou": 500}
assert once() == {"ok": True, "tags": 3, "gifs": 0, "indexed": False}
assert asyncio.run(_rules()) == []
v = view()
assert v["status"] == "fresh" and v["channel"]["indexed"] is False
assert [t["tag"] for t in v["tags"]] == ["nareko", "dissolve", "touhou"]  # count asc
assert [t["saturated"] for t in v["tags"]] == [False, False, True]
assert all(t["new"] for t in v["tags"]) and all(t["delta"] is None for t in v["tags"])
assert card()["headline"] == "Channel not indexed"
assert card()["lines"] == ["0 GIFs found"]

# channel appears: giphy_indexed wins once (previous 0 snapshot existed)
CUR["chan"], CUR["gifs"] = 1, [{"id": "g1", "title": "Nareko", "username": "sartouhou"}]
once()
assert asyncio.run(_rules()) == ["giphy_indexed"]
once()
assert asyncio.run(_rules()) == ["giphy_indexed"], "must fire once"
v = view()
assert v["channel"]["indexed"] is True and v["channel"]["channel_count"] == 1
assert v["channel"]["gifs"] == [{"id": "g1", "title": "Nareko",
                                 "username": "sartouhou", "new": True}]
assert card()["headline"] == "Channel indexed"
assert card()["delta_new"] is False  # delta_new is dead — window cards never set it

# age every snapshot so far past the 7d window: the deltas below are taken vs
# these values (watermarks now only feed mark_seen/MCP; views anchor to windows)
with db.db() as c:
    c.execute("UPDATE snapshots SET fetched_at='2026-08-01T00:00:00.000000+00:00'")

# tag moved: one alert with old/new in payload
CUR["tags"]["nareko"] = 45
once()
with db.db() as c:
    a = c.execute("SELECT payload FROM alerts WHERE rule='giphy_tag_moved'").fetchone()
    p = __import__("json").loads(a["payload"])
    assert p["facts"] == {"tag": "nareko", "old": 46, "new": 45}, p
v = view()
by_tag = {t["tag"]: t for t in v["tags"]}
assert by_tag["nareko"]["delta"] == -1 and by_tag["nareko"]["new"] is False
assert card()["lines"][:1] == ["nareko -1"]

# gif delta: anchored gif no longer new, a fresh one is
CUR["gifs"].append({"id": "g2", "title": None, "username": "sartouhou"})
once()
v = view()
new = {g["id"]: g["new"] for g in v["channel"]["gifs"]}
assert new == {"g1": False, "g2": True}, new

# watermark 0->...->0: no giphy_indexed re-fire
CUR["chan"], CUR["gifs"] = 0, []
once()
assert asyncio.run(_rules()) == ["giphy_indexed", "giphy_tag_moved"]

print("giphy ok")
