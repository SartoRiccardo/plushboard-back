"""GIPHY (GIF Engine distribution, §5.4): tag saturation + channel indexing.

Gotchas honored: pagination.total_count CAPS AT 500 — value >= 500 means the
tag is saturated, not a real count (view exposes saturated: true, no fake
precision). Channel indexing is evidenced only by /channels/search
total_count > 0 OR the @sartouhou gif search returning hits; the
giphy.com/sartouhou page resolving proves nothing and is ignored (§5.4).
Honest limitation: GIPHY exposes no public view counts via API — reach is
tag-saturation deltas + presence only (§5.4, shown in the UI).

Snapshot metrics (§6): giphy_tag_count (key=tag), giphy_channel_indexed
(key=sartouhou, 0/1), giphy_gif (key=gif id, 1.0 — delta gives per-GIF "new").
Display text (gif title/username) lives in the settings JSON cache
`giphy_gifs`, the channel-search count in `giphy_channel` — same
text-in-settings pattern as mc.py's `mc_products`. Read-only GETs (§1.1).
"""
import httpx

from .. import config, engine

BASE = "https://api.giphy.com/v1"
CHANNEL = "sartouhou"
SATURATED = 500


def _key() -> str:
    return config.env("GIPHY_API_KEY")


async def _get(path: str, **params) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{BASE}{path}", params={"api_key": _key(), **params})
        r.raise_for_status()
        return r.json()


def _total(d: dict) -> int:
    return (d.get("pagination") or {}).get("total_count") or 0


async def tag_count(tag: str) -> int:
    return _total(await _get("/gifs/search", q=tag, limit=1, rating="g"))


async def channel_count() -> int:
    return _total(await _get("/channels/search", q=CHANNEL))


async def channel_gifs() -> list[dict]:
    """@sartouhou search hits: [{id, title, username}]."""
    d = await _get("/gifs/search", q=f"@{CHANNEL}", limit=25)
    return [
        {
            "id": str(g["id"]),
            "title": g.get("title") or None,
            "username": g.get("username") or (g.get("user") or {}).get("username"),
        }
        for g in d.get("data") or []
    ]


# --- refresh ---

def _latest_map(c, metric: str) -> dict:
    return {
        r["key"]: r["value"]
        for r in c.execute(
            "SELECT key, value, MAX(fetched_at) FROM snapshots"
            " WHERE source='giphy' AND metric=? GROUP BY key", (metric,)
        )
    }


async def refresh(ctx: engine.Ctx) -> dict:
    tags = engine.get_setting(ctx.c, "giphy_tags", [])
    prev_tags = _latest_map(ctx.c, "giphy_tag_count")
    for tag in tags:
        n = await tag_count(tag)
        ctx.record("giphy_tag_count", tag, n)
        if tag in prev_tags and prev_tags[tag] != n:
            ctx.alert(
                "giphy_tag_moved", "info",
                f"GIPHY: tag '{tag}' moved from {prev_tags[tag]:.0f}"
                f" to {n:.0f} results.",
                "/geo", {"tag": tag, "old": prev_tags[tag], "new": n},
            )

    chan = await channel_count()
    gifs = await channel_gifs()
    indexed = chan > 0 or len(gifs) > 0
    prev_chan = _latest_map(ctx.c, "giphy_channel_indexed").get(CHANNEL)
    ctx.record("giphy_channel_indexed", CHANNEL, 1.0 if indexed else 0.0)
    if indexed and prev_chan == 0:
        ctx.alert(
            "giphy_indexed", "win",
            f"GIPHY: @{CHANNEL} is indexed — {len(gifs)} gifs found"
            f" (channel search: {chan}).",
            "/geo", {"channel": CHANNEL, "channel_count": chan, "gifs": len(gifs)},
        )

    for g in gifs:
        ctx.record("giphy_gif", g["id"], 1.0)
    engine.set_setting(ctx.c, "giphy_gifs",
                       {g["id"]: {"title": g["title"], "username": g["username"]}
                        for g in gifs})
    engine.set_setting(ctx.c, "giphy_channel", {"count": chan})
    return {"tags": len(tags), "gifs": len(gifs), "indexed": indexed}


# --- view ---

async def view(c, f: str | None = None, t: str | None = None) -> dict:
    interval = (engine.get_setting(c, "intervals", {}) or {}).get("giphy", 21600)
    status = engine.source_status(c, "giphy", interval)
    anchor = engine.window_start(7)  # table deltas: fixed 7d window

    tags = []
    for tag in engine.get_setting(c, "giphy_tags", []):
        d = engine.delta(c, "giphy", "giphy_tag_count", tag, anchor=anchor)
        if d is None:
            continue
        n = int(d["value"])
        tags.append({"tag": tag, "count": n, "delta": d["delta"], "new": d["new"],
                     "saturated": n >= SATURATED})
    tags.sort(key=lambda tg: tg["count"])  # most-saturated last

    chan = engine.delta(c, "giphy", "giphy_channel_indexed", CHANNEL, anchor=anchor)
    cache = engine.get_setting(c, "giphy_gifs", {}) or {}
    gifs = [
        {"id": gid, "title": e.get("title"), "username": e.get("username"),
         "new": bool((engine.delta(c, "giphy", "giphy_gif", gid, anchor=anchor)
                      or {}).get("new"))}
        for gid, e in sorted(cache.items())
    ]
    return {
        **status,
        "channel": {
            "indexed": bool(chan and chan["value"]),
            "channel_count": int((engine.get_setting(c, "giphy_channel", {}) or {})
                                 .get("count") or 0),
            "gifs": gifs,
        },
        "tags": tags,
    }


# --- overview card ---

def card(c, days: int) -> dict:
    base = {"source": "giphy", "label": "GEO", "page": "/geo",
            "og": f"https://giphy.com/{CHANNEL}"}
    anchor = engine.window_start(days)
    chan = engine.delta(c, "giphy", "giphy_channel_indexed", CHANNEL, anchor=anchor)
    if chan is None:
        return {**base, "headline": "never fetched", "delta_new": False,
                "lines": ["no data yet"]}
    headline = "Channel indexed" if chan["value"] else "Channel not indexed"

    movers = []
    for tag in engine.get_setting(c, "giphy_tags", []):
        d = engine.delta(c, "giphy", "giphy_tag_count", tag, anchor=anchor)
        if d and not d["new"] and d["delta"]:
            movers.append((tag, d["delta"]))
    movers.sort(key=lambda td: abs(td[1]), reverse=True)
    lines = [f"{tag} {d:+.0f}" for tag, d in movers[:3]]
    gifs = engine.get_setting(c, "giphy_gifs", {}) or {}
    lines.append(f"{len(gifs)} GIFs found")
    return {**base, "headline": headline, "delta_new": False, "lines": lines}
