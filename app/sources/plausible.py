'Plausible client (§5.1) + refresh + view + overview card.\n\nEngine invariant: snapshots store complete-day values only (daily rows,\nbreakdowns over trailing complete days) so watermark deltas stay stable.\nIn-progress values (today, realtime, arbitrary view ranges) are read live\nand never snapshotted.\n\nGotchas honored: period=custom only works on /timeseries (aggregates are\nsummed from daily rows); the filter param is `filters=`, not `filter=`\n(we fetch whole breakdowns and filter client-side).\n'
import httpx

from .. import config, engine

SITE = "smallplushies.com"

# (property param, response field — API strips the "visit:"/"event:" prefix, snapshot metric)
BREAKDOWNS = [
    ("visit:source", "source", "visitors_source"),
    ("event:page", "page", "visitors_page"),
    ("visit:country", "country", "visitors_country"),
    ("visit:device", "device", "visitors_device"),
]


def _base() -> str:
    return config.env("PLAUSIBLE_BASE_URL").rstrip("/")


async def _get(path: str, **params):
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            f"{_base()}/api/v1/stats/{path}",
            params={"site_id": SITE, **params},
            headers={"Authorization": f"Bearer {config.env('PLAUSIBLE_KEY')}"},
        )
        r.raise_for_status()
        return r.json()


async def timeseries(f: str, t: str) -> list[dict]:
    return (await _get("timeseries", period="custom", date=f"{f},{t}",
                       metric="visitors"))["results"]


async def breakdown(prop: str, field: str, f: str, t: str, limit: int = 50) -> list[dict]:
    """Normalized rows: [{key, visitors}]."""
    raw = (await _get("breakdown", property=prop, period="custom",
                      date=f"{f},{t}", metric="visitors", limit=limit))["results"]
    return [{"key": r[field], "visitors": r["visitors"]} for r in raw]


async def realtime() -> int:
    # spec §5.1 says GET /realtime -> {"visitors": n}, but this instance runs
    # the older CE API: GET /realtime/visitors -> bare int (verified 2026-09-30).
    return await _get("realtime/visitors")


# --- refresh (snapshot writes + alerts) ---

async def refresh(ctx: engine.Ctx) -> dict:
    today = engine.rome_today()
    daily = await timeseries(engine.rome_date_str(13), today)

    # breakdowns over trailing 7 complete days
    w_from, w_to = engine.rome_date_str(7), engine.rome_date_str(1)
    bd = {metric: await breakdown(prop, field, w_from, w_to)
          for prop, field, metric in BREAKDOWNS}

    prev_sources = {
        r["key"] for r in ctx.c.execute(
            "SELECT DISTINCT key FROM snapshots WHERE source=? AND metric='visitors_source'",
            (ctx.source,),
        )
    }

    for row in daily:
        ctx.record("visitors_daily", row["date"], row["visitors"])
    for _, _, metric in BREAKDOWNS:
        for row in bd[metric]:
            ctx.record(metric, row["key"], row["visitors"])

    complete = [(r["date"], r["visitors"]) for r in daily if r["date"] < today]
    for i, (d, v) in enumerate(complete):
        prev = [pv for _, pv in complete[max(0, i - 7):i]]
        # ponytail: needs >=4 prior days of history; week one never fires
        if len(prev) >= 4 and v > 0 and v > 2 * (sum(prev) / len(prev)):
            ctx.alert(
                "plausible_spike", "info",
                f"Traffic: {v} visitors on {d}, over 2x the prior 7-day mean.",
                "/traffic", {"date": d, "visitors": v},
            )
    for row in bd["visitors_source"]:
        if row["key"] not in prev_sources:
            ctx.alert(
                "plausible_new_source", "info",
                f"Traffic: new source '{row['key']}' ({row['visitors']} "
                f"{'visitor' if row['visitors'] == 1 else 'visitors'} this week).",
                "/traffic", {"source": row["key"], "visitors": row["visitors"]},
            )
    return {"daily": len(daily)}


# --- views ---

def _with_delta(c, metric: str, rows: list[dict], anchor: str) -> list[dict]:
    out = []
    for row in rows:
        d = engine.delta(c, "plausible", metric, row["key"], anchor=anchor)
        out.append({
            "key": row["key"],
            "visitors": row["visitors"],
            "delta": d["delta"] if d else None,
            "new": bool(d and d["new"]),
        })
    return out


def _since_seen(c, days: int = 7) -> dict:
    """Window summary for headline + view: visitors over the trailing `days`,
    anchored to the window start so it moves without mark_seen."""
    d = engine.since_seen(c, "plausible", "visitors_daily",
                          f=engine.rome_date_str(days - 1),
                          anchor=engine.window_start(days))
    return {"visitors": d["value"], "new": d["new"]}


async def view(c, f: str, t: str) -> dict:
    interval = (engine.get_setting(c, "intervals", {}) or {}).get("plausible", 900)
    status = engine.source_status(c, "plausible", interval)
    anchor = engine.window_start(7)  # table deltas: fixed 7d window
    try:
        daily = await timeseries(f, t)
        bd = {metric: await breakdown(prop, field, f, t, limit=20)
              for prop, field, metric in BREAKDOWNS}
        rt: int | None = await realtime()
        error = None
    except Exception as e:
        # source isolation: degrade to last-good snapshots
        error = str(e)
        rt = None
        daily = [
            {"date": r["key"], "visitors": r["value"]}
            for r in c.execute(
                "SELECT key, value FROM snapshots WHERE source='plausible' AND metric='visitors_daily'"
                " AND key BETWEEN ? AND ? GROUP BY key HAVING fetched_at=MAX(fetched_at) ORDER BY key",
                (f, t),
            )
        ]
        bd = {
            metric: [
                {"key": r["key"], "visitors": r["value"]}
                for r in c.execute(
                    "SELECT key, value FROM snapshots WHERE source='plausible' AND metric=?"
                    " GROUP BY key HAVING fetched_at=MAX(fetched_at) ORDER BY value DESC LIMIT 20",
                    (metric,),
                )
            ]
            for _, _, metric in BREAKDOWNS
        }

    daily_out = [{"date": r["date"], "visitors": r["visitors"]} for r in daily]

    store_urls = engine.get_setting(c, "store_urls", [])
    pages = _with_delta(c, "visitors_page", bd["visitors_page"], anchor)
    product = [p for p in pages if p["key"] in store_urls]

    return {
        **status,
        "error": error,
        "range": {"from": f, "to": t},
        "since_seen": _since_seen(c),
        "daily": daily_out,
        "sources": _with_delta(c, "visitors_source", bd["visitors_source"], anchor),
        "pages": pages,
        "countries": _with_delta(c, "visitors_country", bd["visitors_country"], anchor),
        "devices": _with_delta(c, "visitors_device", bd["visitors_device"], anchor),
        "product_pages": {
            "visitors": sum(p["visitors"] for p in product),
            "delta": sum(p["delta"] or 0 for p in product) or None,
        },
        "realtime": rt,
    }


# --- overview card (snapshot-only: no upstream calls) ---

def card(c, days: int) -> dict:
    since = _since_seen(c, days)
    anchor = engine.window_start(days)
    changes = []
    for r in c.execute(
        "SELECT DISTINCT key FROM snapshots WHERE source='plausible' AND metric='visitors_source'"
    ):
        d = engine.delta(c, "plausible", "visitors_source", r["key"], anchor=anchor)
        if d and ((d["new"] or d["delta"])):
            changes.append((r["key"], d))
    changes.sort(key=lambda kc: abs(kc[1]["delta"] or 0), reverse=True)
    lines = []
    for key, d in changes[:3]:
        mark = "new" if d["new"] else f"{d['delta']:+.0f}"
        lines.append(f"{key}: {mark}")
    return {
        "source": "plausible",
        "label": "Traffic",
        "page": "/traffic",
        "og": f"{_base()}/{SITE}",
        "headline": f"{since['visitors']:.0f} "
                    f"{'visitor' if since['visitors'] == 1 else 'visitors'} "
                    f"{engine.window_phrase(days)}",
        "delta_new": False,
        "lines": lines,
    }
