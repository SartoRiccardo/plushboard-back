"""Google Search Console (§5.2): performance, sitemaps, on-demand URL inspect.

Gotchas honored: data lags 2-3 days (we surface "data through {max(date)}");
urlInspection has NO webmasters/ prefix and returns indexStatusResult; SA must
be a property Owner (it is). Devices/countries dims are skipped — no screen
consumes them yet.

Snapshot metrics: gsc_clicks/gsc_impr/gsc_pos (key=date), gsc_query(+_impr/_pos,
key=query), gsc_page_clicks(+_impr/_pos, key=path), gsc_index_verdict
(key=path, PASS=2/PARTIAL=1/else=0; full text lives in settings `gsc_inspect`),
gsc_kw_pos (key=tracked keyword, §10.6: one row per refresh, value = position
from the latest date row of the dt-6..dt ["date","query"] fetch — the
fetched_at series IS the position-over-time sparkline for /keywords).

Views read snapshots (6h refresh); only sitemaps are fetched live.
"""
from datetime import date, timedelta
from urllib.parse import quote

import httpx

from .. import config, db, engine
from . import google_auth

BASE = "https://searchconsole.googleapis.com"
STORE_BASE = "https://smallplushies.com"
SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"


def _token() -> str:
    return google_auth.get_creds([SCOPE]).token


async def _post(path: str, body: dict) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"{BASE}{path}", json=body, headers={"Authorization": f"Bearer {_token()}"}
        )
        r.raise_for_status()
        return r.json()


async def query(dims: list[str], f: str, t: str, limit: int = 250) -> list[dict]:
    data = await _post(
        f"/webmasters/v3/sites/{quote(config.env('GSC_SITE_URL'), safe='')}/searchAnalytics/query",
        {"startDate": f, "endDate": t, "dimensions": dims, "rowLimit": limit},
    )
    return data.get("rows", [])


async def sitemaps() -> list[dict]:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            f"{BASE}/webmasters/v3/sites/{quote(config.env('GSC_SITE_URL'), safe='')}/sitemaps",
            headers={"Authorization": f"Bearer {_token()}"},
        )
        r.raise_for_status()
        return r.json().get("sitemap", [])


def _shift(d: str, days: int) -> str:
    return (date.fromisoformat(d) + timedelta(days=days)).isoformat()


async def inspect_fetch(path: str) -> dict:
    """URL Inspection API call only (free, ~2s, 2k/day quota)."""
    data = await _post(
        "/v1/urlInspection/index:inspect",
        {"inspectionUrl": STORE_BASE + path, "siteUrl": config.env("GSC_SITE_URL")},
    )
    res = data["inspectionResult"]["indexStatusResult"]
    verdict = res.get("verdict", "NEUTRAL")
    coverage = res.get("coverageState", "")
    numeric = {"PASS": 2, "PARTIAL": 1}.get(verdict, 0)
    return {"verdict": verdict, "coverage": coverage, "numeric": numeric}


def inspect_store(c, path: str, res: dict) -> bool:
    """Persist one inspection verdict; True when it changed (alerts §11)."""
    prev = engine.latest(c, "gsc", "gsc_index_verdict", path)
    engine.record(c, "gsc", "gsc_index_verdict", path, res["numeric"])
    cache = engine.get_setting(c, "gsc_inspect", {})
    cache[path] = {"verdict": res["verdict"], "coverage": res["coverage"],
                   "checked_at": engine.now_iso()}
    engine.set_setting(c, "gsc_inspect", cache)
    changed = prev is not None and prev["value"] != res["numeric"]
    if changed:
        engine.emit_alert(
            c, "gsc_index_flip", "info",
            f"Search: index status of {path} changed to '{res['coverage']}'.",
            "/search", {"path": path, "verdict": res["verdict"],
                        "coverage": res["coverage"]},
        )
    return changed


async def inspect_url(path: str) -> dict:
    """Fetch + store (the manual 'check now' button path)."""
    res = await inspect_fetch(path)
    with db.db() as c:
        changed = inspect_store(c, path, res)
    return {**res, "changed": changed}


async def inspect_all(ctx: engine.Ctx) -> dict:
    """Inspect every store URL. Runs as its own 12h background job (free API:
    3 URLs x 2/day = 6 calls vs the 2k/day quota); best-effort per URL — a
    failure skips that URL, never fails the job."""
    inspected = 0
    for path in engine.get_setting(ctx.c, "store_urls", []):
        try:
            res = await inspect_fetch(path)
        except Exception:
            continue
        inspect_store(ctx.c, path, res)
        inspected += 1
    return {"inspected": inspected}


# --- refresh ---

def _latest_map(c, metric: str) -> dict:
    return {
        r["key"]: r["value"]
        for r in c.execute(
            "SELECT key, value, MAX(fetched_at) FROM snapshots"
            " WHERE source='gsc' AND metric=? GROUP BY key", (metric,)
        )
    }


async def refresh(ctx: engine.Ctx) -> dict:
    today = engine.rome_today()
    daily = await query(["date"], _shift(today, -16), today)
    if not daily:
        return {"daily": 0}
    dt = max(r["keys"][0] for r in daily)  # data through — GSC lags 2-3 days

    prev_queries = set(_latest_map(ctx.c, "gsc_query"))
    prev_pages = _latest_map(ctx.c, "gsc_page_clicks")
    w_from = _shift(dt, -6)
    rows_q = await query(["query"], w_from, dt)
    rows_p = await query(["page"], w_from, dt)
    rows_kd = await query(["date", "query"], w_from, dt)

    for r in daily:
        d = r["keys"][0]
        ctx.record("gsc_clicks", d, r["clicks"])
        ctx.record("gsc_impr", d, r["impressions"])
        ctx.record("gsc_pos", d, r["position"])
    for r in rows_q:
        q = r["keys"][0]
        ctx.record("gsc_query", q, r["clicks"])
        ctx.record("gsc_query_impr", q, r["impressions"])
        ctx.record("gsc_query_pos", q, r["position"])
    for r in rows_p:
        p = _norm_path(r["keys"][0])
        ctx.record("gsc_page_clicks", p, r["clicks"])
        ctx.record("gsc_page_impr", p, r["impressions"])
        ctx.record("gsc_page_pos", p, r["position"])

    for r in rows_q:
        q = r["keys"][0]
        if q and q not in prev_queries:
            ctx.alert(
                "gsc_new_query", "info",
                f"Search: new query '{q}' ({r['clicks']:.0f} clicks this week).",
                "/search", {"query": q, "clicks": r["clicks"]},
            )
    for r in rows_p:
        p = _norm_path(r["keys"][0])
        if prev_pages.get(p) == 0 and r["clicks"] > 0:
            ctx.alert(
                "gsc_first_click", "win",
                f"Search: first clicks on {p} ({r['clicks']:.0f} this week).",
                "/search", {"path": p, "clicks": r["clicks"]},
            )
    # gsc_kw_pos (§10.6): one snapshot per tracked keyword per refresh — the
    # position from the most recent date row for that keyword in the window.
    tracked = set(engine.get_setting(ctx.c, "tracked_keywords", []))
    kd_pos: dict[str, tuple[str, float]] = {}
    for r in rows_kd:
        d, q = r["keys"][0], r["keys"][1]
        if q in tracked and (q not in kd_pos or d > kd_pos[q][0]):
            kd_pos[q] = (d, r["position"])
    for q, (_, pos) in sorted(kd_pos.items()):
        ctx.record("gsc_kw_pos", q, pos)
    return {"daily": len(daily), "data_through": dt, "kw_pos": len(kd_pos)}


def _norm_path(url: str) -> str:
    """GSC page keys are absolute URLs; normalize to paths so they match
    store_urls ('https://smallplushies.com/' -> '/')."""
    if url.startswith(STORE_BASE):
        return url[len(STORE_BASE):] or "/"
    return url


def data_through(c) -> str | None:
    row = c.execute(
        "SELECT MAX(key) AS m FROM snapshots WHERE source='gsc' AND metric='gsc_clicks'"
    ).fetchone()
    return row["m"]


# --- view ---

def _daily_rows(c, metric: str, f: str, t: str, anchor: str) -> list[dict]:
    out = []
    for r in c.execute(
        "SELECT DISTINCT key FROM snapshots WHERE source='gsc' AND metric=?"
        " AND key>=? AND key<=? ORDER BY key", (metric, f, t),
    ):
        d = engine.delta(c, "gsc", metric, r["key"], anchor=anchor)
        if d:
            out.append({"date": r["key"], "value": d["value"],
                        "delta": d["delta"]})
    return out


def _breakdown(c, metric: str, impr_metric: str, pos_metric: str,
               limit: int, anchor: str) -> list[dict]:
    'Latest value + delta per key, sorted by clicks, top `limit`.'
    out = []
    for r in c.execute(
        "SELECT DISTINCT key FROM snapshots WHERE source='gsc' AND metric=?",
        (metric,),
    ):
        d = engine.delta(c, "gsc", metric, r["key"], anchor=anchor)
        impr = engine.delta(c, "gsc", impr_metric, r["key"], anchor=anchor)
        pos = engine.delta(c, "gsc", pos_metric, r["key"], anchor=anchor)
        out.append({
            "key": r["key"],
            "clicks": d["value"] if d else 0,
            "delta": d["delta"] if d else None,
            "impr": impr["value"] if impr else 0,
            "impr_delta": impr["delta"] if impr else None,
            "pos": round(pos["value"], 1) if pos and pos["value"] is not None else None,
        })
    out.sort(key=lambda x: x["clicks"], reverse=True)
    return out[:limit]


async def view(c, f: str | None = None, t: str | None = None) -> dict:
    interval = (engine.get_setting(c, "intervals", {}) or {}).get("gsc", 21600)
    status = engine.source_status(c, "gsc", interval)
    dt = data_through(c)
    if dt is None:
        return {**status, "error": None, "data_through": None, "empty": True}

    anchor = engine.window_start(7)  # totals + table deltas: fixed 7d window
    if not f:
        f = _shift(dt, -6)  # fixed 7d default — never past data-through
    t = min(t or dt, dt)  # never past data-through

    daily = _daily_rows(c, "gsc_clicks", f, t, anchor)
    impr = _daily_rows(c, "gsc_impr", f, t, anchor)
    pos = _daily_rows(c, "gsc_pos", f, t, anchor)

    def _sum(rs):
        return sum(r["value"] for r in rs), sum(r["delta"] or 0 for r in rs)

    clicks_total, clicks_delta = _sum(daily)
    impr_total, impr_delta = _sum(impr)
    ctr = clicks_total / impr_total if impr_total else 0
    ctr_base = (
        (clicks_total - clicks_delta) / (impr_total - impr_delta)
        if impr_total - impr_delta > 0 else None
    )
    pos_now = [r["value"] for r in pos if r["value"]]
    pos_base = [
        r["value"] - r["delta"] for r in pos if r["value"] and r["delta"] is not None
    ]
    # ponytail: unweighted mean of daily avg positions, impression-weighted if it matters
    position = sum(pos_now) / len(pos_now) if pos_now else None
    position_delta = (
        position - sum(pos_base) / len(pos_base) if pos_now and pos_base else None
    )

    queries = _breakdown(c, "gsc_query", "gsc_query_impr", "gsc_query_pos", 20, anchor)
    pages = _breakdown(c, "gsc_page_clicks", "gsc_page_impr", "gsc_page_pos", 50, anchor)
    store_urls = engine.get_setting(c, "store_urls", [])
    inspect_cache = engine.get_setting(c, "gsc_inspect", {})
    for p in pages:
        p["known"] = p["key"] in store_urls
        p["index"] = inspect_cache.get(p["key"])

    try:
        smaps = [
            {"path": s["path"], "last_submitted": s.get("lastSubmitted"),
             "is_pending": s.get("isPending", False), "errors": s.get("errors", 0),
             "warnings": s.get("warnings", 0)}
            for s in await sitemaps()
        ]
    except Exception:
        smaps = None

    return {
        **status,
        "error": None,
        "data_through": dt,
        "range": {"from": f, "to": t},
        "totals": {
            "clicks": clicks_total, "clicks_delta": clicks_delta,
            "impressions": impr_total, "impressions_delta": impr_delta,
            "ctr": ctr,
            "ctr_delta": ctr - ctr_base if ctr_base is not None else None,
            "position": position, "position_delta": position_delta,
        },
        "daily": [
            {"date": r["date"], "clicks": r["value"],
             "impr": next((i["value"] for i in impr if i["date"] == r["date"]), 0)}
            for r in daily
        ],
        "queries": queries,
        "pages": pages,
        "sitemaps": smaps,
    }


# --- overview card ---

def card(c, days: int) -> dict:
    anchor = engine.window_start(days)
    clicks = engine.since_seen(c, "gsc", "gsc_clicks",
                               f=engine.rome_date_str(days - 1), anchor=anchor)
    dt = data_through(c)
    changes = []
    for r in c.execute(
        "SELECT DISTINCT key FROM snapshots WHERE source='gsc' AND metric='gsc_query'"
    ):
        d = engine.delta(c, "gsc", "gsc_query", r["key"], anchor=anchor)
        if d and (d["new"] or d["delta"]):
            changes.append((r["key"], d))
    changes.sort(key=lambda qd: abs(qd[1]["delta"] or 0), reverse=True)
    lines = [f"data through {dt}" if dt else "no data yet"]
    for q, d in changes[:3]:
        mark = "new" if d["new"] else f"{d['delta']:+.0f} clicks"
        lines.append(f"{q}: {mark}")
    return {
        "source": "gsc",
        "label": "Search",
        "page": "/search",
        "og": "https://search.google.com/search-console?resource_id="
              + quote(config.env("GSC_SITE_URL"), safe=""),
        "headline": f"{clicks['value']:.0f} "
                    f"{'click' if clicks['value'] == 1 else 'clicks'} "
                    f"{engine.window_phrase(days)}",
        "delta_new": False,
        "lines": lines,
    }
