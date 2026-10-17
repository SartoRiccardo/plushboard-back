"""MCP server (§8): Streamable-HTTP at /mcp, bearer DASH_MCP_TOKEN (§3 — not
behind OAuth; browsers never touch it). May read anything and write only
dashboard-internal state (settings, watermarks, manual log). It can never
mutate an upstream platform (§1.1): dash_refresh only refreshes free sources.
"""
import json

from mcp.server.mcpserver import MCPServer

from . import config, db, engine
from .sources import SOURCES

mcp = MCPServer("plushboard")


@mcp.tool()
async def dash_overview() -> dict:
    """Composed overview payload: source statuses, unseen alerts, cards,
    today's visitors + realtime, 30-day sparkline."""
    from .api import overview

    return await overview()


@mcp.tool()
async def dash_source(source: str) -> dict:
    """Latest values + watermark deltas for one source, grouped by metric.
    Reads snapshots only — no upstream calls."""
    if source not in SOURCES:
        return {"error": f"unknown source '{source}', one of {sorted(SOURCES)}"}
    with db.db() as c:
        out: dict = {}
        for r in c.execute(
            "SELECT DISTINCT metric, key FROM snapshots WHERE source=?", (source,)
        ):
            d = engine.delta(c, source, r["metric"], r["key"])
            out.setdefault(r["metric"], {})[r["key"] or "_"] = d
        return out


@mcp.tool()
async def dash_history(source: str, metric: str, key: str | None = None) -> list[dict]:
    """Snapshot series for one metric (optionally one key), oldest first."""
    with db.db() as c:
        q = ("SELECT key, value, fetched_at FROM snapshots"
             " WHERE source=? AND metric=?")
        args: list = [source, metric]
        if key is not None:
            q += " AND key=?"
            args.append(key)
        q += " ORDER BY fetched_at"
        return [dict(r) for r in c.execute(q, args)]


@mcp.tool()
async def dash_alerts(seen: bool = False) -> list[dict]:
    """Alert rows, newest first (default: unseen only)."""
    with db.db() as c:
        rows = c.execute(
            "SELECT * FROM alerts WHERE seen=? ORDER BY created_at DESC LIMIT 200",
            (int(seen),),
        ).fetchall()
        return [
            {**json.loads(r["payload"]), "id": r["id"], "rule": r["rule"],
             "severity": r["severity"], "created_at": r["created_at"],
             "seen": bool(r["seen"])}
            for r in rows
        ]


@mcp.tool()
async def dash_mark_seen(source: str | None = None) -> dict:
    """Re-anchor watermark deltas (all sources, or one)."""
    with db.db() as c:
        engine.mark_seen(c, source)
        return {"ok": True}


@mcp.tool()
async def dash_refresh(source: str | None = None) -> dict:
    """Refresh free source(s) now. Paid sources (dataforseo) refuse — §1.4."""
    import asyncio

    from . import engine as eng

    if source is not None:
        if source not in SOURCES:
            return {"error": f"unknown source '{source}'"}
        if not SOURCES[source].free:
            return {"error": f"'{source}' is a paid source — refresh it via its UI button"}
        return {source: await eng.refresh_source(source)}
    free = [sid for sid, s in SOURCES.items() if s.free and s.has_creds()]
    results = await asyncio.gather(*(eng.refresh_source(sid) for sid in free))
    return dict(zip(free, results))


@mcp.tool()
async def dash_list_keywords() -> list[str]:
    """Tracked keywords."""
    with db.db() as c:
        return engine.get_setting(c, "tracked_keywords", [])


@mcp.tool()
async def dash_add_keywords(kws: list[str]) -> dict:
    """Track keywords (appended, deduped)."""
    with db.db() as c:
        tracked = engine.get_setting(c, "tracked_keywords", [])
        for kw in kws:
            if kw and kw not in tracked:
                tracked.append(kw)
        engine.set_setting(c, "tracked_keywords", tracked)
        return {"ok": True, "tracked": tracked}


@mcp.tool()
async def dash_remove_keyword(kw: str) -> dict:
    """Untrack one keyword."""
    with db.db() as c:
        tracked = engine.get_setting(c, "tracked_keywords", [])
        if kw not in tracked:
            return {"error": f"'{kw}' is not tracked"}
        engine.set_setting(c, "tracked_keywords", [k for k in tracked if k != kw])
        return {"ok": True}


@mcp.tool()
async def dash_list_tags() -> list[str]:
    """Tracked GIPHY tags."""
    with db.db() as c:
        return engine.get_setting(c, "giphy_tags", [])


@mcp.tool()
async def dash_set_tags(tags: list[str]) -> dict:
    """Replace the GIPHY tag list."""
    with db.db() as c:
        engine.set_setting(c, "giphy_tags", [t for t in tags if t.strip()])
        return {"ok": True}


@mcp.tool()
async def dash_get_setting(k: str):
    """Read one setting (intervals, enabled, giphy_tags, tracked_keywords,
    store_urls, retention_days, alert_rules)."""
    with db.db() as c:
        return engine.get_setting(c, k)


@mcp.tool()
async def dash_set_setting(k: str, v) -> dict:
    """Write one setting (same keys/validation as the REST PUT)."""
    from .api import _validate_setting

    _validate_setting(k, v)
    with db.db() as c:
        engine.set_setting(c, k, v)
        return {"ok": True}


@mcp.tool()
async def dash_log_manual(kind: str, value: float | None = None,
                          note: str | None = None) -> dict:
    """Append a manual ledger row: kind ∈ sale|backlink|gif_upload."""
    if kind not in ("sale", "backlink", "gif_upload"):
        return {"error": "kind must be sale, backlink or gif_upload"}
    with db.db() as c:
        cur = c.execute(
            "INSERT INTO manual_log(kind, value, note, ts) VALUES(?,?,?,?)",
            (kind, value, note, engine.now_iso()),
        )
        return {"ok": True, "id": cur.lastrowid}
