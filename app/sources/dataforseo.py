"""DataForSEO (PAID, §5.5): keyword rank / difficulty / ideas, button-only.

This is the money source: every call costs real money. Nothing here is ever
run by the scheduler (registry free=False) or the generic routes (both 409,
§8) — the only entry points are the explicit /api/keywords/* buttons. HTTP
Basic auth with DATAFORSEO_LOGIN/DATAFORSEO_PASSWORD; credentials are never
logged or returned.

Cost accounting (§5.5): per call = Σ of every task's "cost" field, else
0.005/task when absent; every call appends a runs row kind=paid.

Snapshot metrics (§6): df_keyword_rank (key=keyword, 0 = not in top-10 —
the display layer renders 0 as "not in top 10", §10.6) and
df_keyword_difficulty (key=keyword). Keyword ideas are transient intake and
are NOT snapshotted. df_rank_change (§11) fires inside serp() when a
previous rank existed and moved >=3 — only possible on button press.

Backlinks (§10.7): backlinks_summary() snapshots backlinks_refdomains /
backlinks_backlinks (key=smallplushies.com) and fires backlinks_change
(info, §11) when refdomains moved vs the previous snapshot;
backlinks_timeseries() is transient chart data and is NOT snapshotted.
"""
import httpx

from .. import config, db, engine

BASE = "https://api.dataforseo.com/v3"
LOCATION_CODE = 2840          # US market (§5.5)
LANGUAGE_CODE = "en"
OUR_DOMAIN = "smallplushies.com"
TASK_COST = 0.005             # fallback when the response omits task cost


async def _post(path: str, body: list) -> dict:
    async with httpx.AsyncClient(
        timeout=30,
        auth=(config.env("DATAFORSEO_LOGIN"), config.env("DATAFORSEO_PASSWORD")),
    ) as client:
        r = await client.post(f"{BASE}{path}", json=body)
        r.raise_for_status()
        return r.json()


async def paid_call(c, label: str, coro) -> tuple[dict, float]:
    """Await one paid upstream call, log the runs row (kind=paid, §5.5),
    return (response, cost). No retries — a retry is money."""
    try:
        data = await coro
    except Exception as e:
        engine.log_run(c, "dataforseo", "paid", 0.0, "error", f"{label}: {e}")
        raise
    cost = _cost(data)
    engine.log_run(c, "dataforseo", "paid", cost, "ok", None)
    return data, cost


# --- response plumbing (defensive: result shapes vary per endpoint) ---

def _tasks(data: dict) -> list:
    return (data or {}).get("tasks") or []


def _cost(data: dict) -> float:
    """Σ per-task "cost" when present, else 0.005/task (§5.5)."""
    tasks = _tasks(data)
    costs = [t["cost"] for t in tasks if t.get("cost") is not None]
    return round(sum(costs) if costs else TASK_COST * len(tasks), 6)


def _result(data: dict) -> dict | None:
    """First task's first result entry (often a list; take its first dict)."""
    for t in _tasks(data):
        for r in t.get("result") or []:
            if isinstance(r, dict):
                return r
    return None


def _items(data: dict) -> list:
    r = _result(data)
    return (r.get("items") or []) if r else []


def _pick(d: dict, *keys):
    """First non-None field among documented key shapes (varies per endpoint)."""
    for k in keys:
        if d.get(k) is not None:
            return d[k]
    return None


# --- parsers ---

def parse_serp(data: dict) -> dict:
    """our_rank = smallplushies.com's position among the top-10 organic items
    (None when absent); top_domains = first 3 unique organic domains in order;
    features = distinct non-organic item types."""
    items = _items(data)
    organic = [i for i in items if (i.get("type") or "organic") == "organic"]
    our_rank = next(
        (i.get("rank_absolute") or i.get("rank_group")
         for i in organic if OUR_DOMAIN in (i.get("domain") or "")),
        None,
    )
    domains = list(dict.fromkeys(
        i["domain"] for i in organic if i.get("domain")))[:3]
    features = sorted({i["type"] for i in items
                       if i.get("type") and i["type"] != "organic"})
    return {"our_rank": our_rank, "top_domains": domains, "features": features}


def parse_difficulty(data: dict) -> dict[str, float]:
    """{keyword: difficulty} from bulk_keyword_difficulty items."""
    return {
        i["keyword"]: i["keyword_difficulty"]
        for i in _items(data)
        if i.get("keyword") is not None and i.get("keyword_difficulty") is not None
    }


def parse_ideas(data: dict) -> list[dict]:
    """Normalized [{keyword, volume, competition}] — fields vary per item;
    keep what's there, skip nulls (§5.5)."""
    out = []
    for i in _items(data):
        if not i.get("keyword"):
            continue
        e: dict = {"keyword": i["keyword"], "volume": i.get("search_volume")}
        comp = i.get("competition")
        if comp is None:
            comp = i.get("competition_level")
        if comp is not None:
            e["competition"] = comp
        out.append({k: v for k, v in e.items() if v is not None})
    return out


# --- paid calls (each opens its own connection, like gsc.inspect_url) ---

async def serp(keyword: str) -> dict:
    """Live SERP check for one keyword (§5.5). Snapshots df_keyword_rank
    (0 = not in top-10) and fires df_rank_change when the rank moves >=3 vs
    the previous snapshot (§11). Paid — button press only."""
    with db.db() as c:
        prev = engine.latest(c, "dataforseo", "df_keyword_rank", keyword)
        try:
            data, cost = await paid_call(
                c, "serp",
                _post("/serp/google/organic/live/advanced", [{
                    "keyword": keyword,
                    "location_code": LOCATION_CODE,
                    "language_code": LANGUAGE_CODE,
                    "depth": 10,
                }]))
        except Exception as e:
            return {"keyword": keyword, "error": str(e)}
        r = parse_serp(data)
        rank = r["our_rank"] or 0.0  # 0 = not in top 10 (§10.6)
        engine.record(c, "dataforseo", "df_keyword_rank", keyword, rank)
        if prev is not None and abs(rank - prev["value"]) >= 3:
            engine.emit_alert(
                c, "df_rank_change", "info",
                f"Keywords: '{keyword}' rank moved {prev['value']:.0f}"
                f" -> {rank:.0f}.",
                "/keywords",
                {"keyword": keyword, "old": prev["value"], "new": rank},
            )
        return {"keyword": keyword, **r, "cost": cost, "rank": rank}


async def difficulty(keywords: list[str]) -> dict:
    """Bulk difficulty for the tracked list (§5.5); snapshots
    df_keyword_difficulty per keyword. Paid — button only."""
    with db.db() as c:
        try:
            data, cost = await paid_call(
                c, "difficulty",
                _post("/dataforseo_labs/google/bulk_keyword_difficulty/live",
                      [{"keywords": keywords,
                        "location_code": LOCATION_CODE,
                        "language_code": LANGUAGE_CODE}]))
        except Exception as e:
            return {"error": str(e)}
        diffs = parse_difficulty(data)
        for kw, d in diffs.items():
            engine.record(c, "dataforseo", "df_keyword_difficulty", kw, d)
        return {"difficulties": diffs, "cost": cost}


async def ideas(keyword: str) -> dict:
    """Keyword-idea intake (§5.5) — transient, NO snapshots (§10.6).
    Paid — button only."""
    with db.db() as c:
        try:
            data, cost = await paid_call(
                c, "ideas",
                _post("/dataforseo_labs/google/keyword_ideas/live", [{
                    "keyword": keyword,
                    "location_code": LOCATION_CODE,
                    "language_code": LANGUAGE_CODE,
                }]))
        except Exception as e:
            return {"error": str(e)}
        return {"ideas": parse_ideas(data), "cost": cost}


# --- backlinks (§10.7, paid — button only like everything above) ---

async def backlinks_summary() -> dict:
    """Backlink overview for OUR_DOMAIN (§5.5, §10.7). Snapshots
    backlinks_refdomains / backlinks_backlinks and fires backlinks_change
    (info, §11) when refdomains changed vs the previous snapshot — only
    possible on button press. Paid.
    Documented result fields (docs.dataforseo.com/v3/backlinks/summary/live):
    referring_domains, backlinks; rd_count kept as a defensive alternate."""
    with db.db() as c:
        prev = engine.latest(c, "dataforseo", "backlinks_refdomains", OUR_DOMAIN)
        try:
            data, cost = await paid_call(
                c, "backlinks_summary",
                _post("/backlinks/summary/live", [{"target": OUR_DOMAIN}]))
        except Exception as e:
            return {"error": str(e)}
        r = _result(data) or {}
        rd = _pick(r, "referring_domains", "rd_count")
        bl = _pick(r, "backlinks")
        if rd is None and bl is None:
            return {"error": "no backlinks summary in response", "cost": cost}
        if rd is not None:
            engine.record(c, "dataforseo", "backlinks_refdomains", OUR_DOMAIN, rd)
            if prev is not None and rd != prev["value"]:
                engine.emit_alert(
                    c, "backlinks_change", "info",
                    f"Backlinks: referring domains {prev['value']:.0f}"
                    f" -> {rd:.0f}.",
                    "/backlinks",
                    {"old": prev["value"], "new": rd},
                )
        if bl is not None:
            engine.record(c, "dataforseo", "backlinks_backlinks", OUR_DOMAIN, bl)
        return {"refdomains": rd, "backlinks": bl, "cost": cost}


async def backlinks_timeseries(f: str, t: str) -> dict:
    """New/lost referring domains per period for OUR_DOMAIN (§5.5, §10.7).
    Transient chart data — NO snapshots (like ideas). Paid — button only.
    Documented item fields (docs.dataforseo.com/v3/backlinks/
    timeseries_new_lost_summary/live): date, new_referring_domains,
    lost_referring_domains — some shapes only carry new_backlinks/
    lost_backlinks, which are normalized in their place (kept honest, not
    silently mixed: whichever pair the response has wins, refdomains first)."""
    with db.db() as c:
        try:
            data, cost = await paid_call(
                c, "backlinks_timeseries",
                _post("/backlinks/timeseries_new_lost_summary/live",
                      [{"target": OUR_DOMAIN, "date_from": f, "date_to": t}]))
        except Exception as e:
            return {"error": str(e)}
        series = [
            {"date": i.get("date"),
             "new_refdomains": _pick(i, "new_referring_domains", "new_backlinks"),
             "lost_refdomains": _pick(i, "lost_referring_domains", "lost_backlinks")}
            for i in _items(data) if i.get("date") is not None
        ]
        return {"series": series, "cost": cost}


# --- overview card ---

def card(c, days: int) -> dict:
    'Overview card (§10.1): tracked count + up to 3 keywords with their\nstored rank ("kw: #7") or not-checked state, plus a refdomains line when\na backlinks summary has been run (§10.7). No deltas on keywords (paid\nsource); the refdomains line shows its watermark delta via engine.delta.'
    kws = engine.get_setting(c, "tracked_keywords", [])
    lines = []
    for kw in kws[:3]:
        r = engine.latest(c, "dataforseo", "df_keyword_rank", kw)
        if r is None:
            lines.append(f"{kw}: not checked")
        elif r["value"]:
            lines.append(f"{kw}: #{r['value']:.0f}")
        else:
            lines.append(f"{kw}: not in top 10")
    d = engine.delta(c, "dataforseo", "backlinks_refdomains", OUR_DOMAIN,
                     anchor=engine.window_start(days))
    if d is not None:
        line = f"refdomains: {d['value']:.0f}"
        if d["new"]:
            line += " (new)"
        elif d["delta"]:
            line += f" ({d['delta']:+.0f})"
        lines.append(line)
    return {
        "source": "dataforseo",
        "label": "Keywords",
        "page": "/keywords",
        "og": "https://app.dataforseo.com/",
        "headline": f"{len(kws)} tracked keywords",
        "delta_new": False,
        "lines": lines,
    }
