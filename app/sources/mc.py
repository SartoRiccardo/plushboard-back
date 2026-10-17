"""Merchant Center (Merchant API v1, §5.3): product status + serving reports.

Gotchas honored: status is EMBEDDED in the Product resource (productStatus
field) — the dedicated productstatuses/* endpoints 404 in v1 and v1beta and
are never called; the reports table is snake_case product_performance_view
with UNPREFIXED fields (segments.date / metrics.impressions 400); empty
report results arrive as a missing or empty "results" — normalized to zero
rows. Only POST is reports:search, a read-shaped RPC (§1.1).

Snapshot metrics: mc_product_state (key=offerId, approved=2/in_review=1/
disapproved=0), mc_impressions/mc_clicks (key=date). Display text (state,
issues, pending countries, "in review since") lives in the settings JSON
cache `mc_products` — same pattern as gsc.py's `gsc_inspect` cache.
"""
from datetime import date

import httpx

from .. import config, engine
from . import google_auth

BASE = "https://merchantapi.googleapis.com"
SCOPE = "https://www.googleapis.com/auth/content"
PENDING_REVIEW_CODE = "pending_initial_policy_review_free_listings"
STATE_NUM = {"disapproved": 0, "in_review": 1, "approved": 2}
NUM_STATE = {v: k for k, v in STATE_NUM.items()}


def _mid() -> str:
    return config.env("GMC_MERCHANT_ID")


def _token() -> str:
    return google_auth.get_creds([SCOPE]).token


async def _get(path: str) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            f"{BASE}{path}", headers={"Authorization": f"Bearer {_token()}"}
        )
        r.raise_for_status()
        return r.json()


async def _post(path: str, body: dict) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"{BASE}{path}", json=body, headers={"Authorization": f"Bearer {_token()}"}
        )
        r.raise_for_status()
        return r.json()


async def products() -> list[dict]:
    data = await _get(f"/products/v1/accounts/{_mid()}/products")
    # ponytail: single page — the store has 2 products, paginate on nextPageToken if it grows
    return data.get("products", [])


async def report(f: str, t: str) -> list[dict]:
    data = await _post(
        f"/reports/v1/accounts/{_mid()}/reports:search",
        {"query": "SELECT date, impressions, clicks FROM product_performance_view"
                  f" WHERE date BETWEEN '{f}' AND '{t}'"},
    )
    return data.get("results") or []  # empty arrives as missing or []


async def report_products(f: str, t: str) -> list[dict]:
    """Per-product daily rows (verified live: offer_id is a valid unprefixed
    segment). [{date, offer_id, impressions, clicks}] — empty while the store
    isn't serving."""
    data = await _post(
        f"/reports/v1/accounts/{_mid()}/reports:search",
        {"query": "SELECT date, offer_id, impressions, clicks FROM"
                  " product_performance_view"
                  f" WHERE date BETWEEN '{f}' AND '{t}'"},
    )
    return data.get("results") or []


def _offer_id(p: dict) -> str:
    """Name looks like accounts/{id}/products/en~PLUSH~mini-kaguya."""
    return p.get("offerId") or p["name"].rsplit("~", 1)[-1]


def _interpret(p: dict) -> dict:
    """§5.3 state logic -> {state, issues, pending_countries, free_listings,
    since}. Worst state wins when several conditions match."""
    st = p.get("productStatus") or {}
    issues = [
        {"code": i.get("code"), "detail": i.get("detail"), "severity": i.get("severity")}
        for i in st.get("itemLevelIssues") or []
    ]
    free = next(
        (d for d in st.get("destinationStatuses") or []
         if d.get("reportingContext") == "FREE_LISTINGS"), None)
    pending = list((free or {}).get("pendingCountries") or [])
    if any(i["severity"] == "ERROR" for i in issues):
        state = "disapproved"
    elif pending and any(i["code"] == PENDING_REVIEW_CODE for i in issues):
        state = "in_review"
    else:
        state = "approved"
    created = st.get("creationDate") or ""
    return {
        "state": state,
        "issues": issues,
        "pending_countries": pending,
        "free_listings": free is not None,
        "since": created[:10] or None,  # creationDate RFC3339 -> YYYY-MM-DD
    }


# --- refresh ---

def _latest_map(c, metric: str) -> dict:
    return {
        r["key"]: r["value"]
        for r in c.execute(
            "SELECT key, value, MAX(fetched_at) FROM snapshots"
            " WHERE source='mc' AND metric=? GROUP BY key", (metric,)
        )
    }


async def refresh(ctx: engine.Ctx) -> dict:
    raw = await products()
    entries = {}
    for p in raw:
        e = _interpret(p)
        # v1: title/image live in productAttributes, not at the top level
        attrs = p.get("productAttributes") or {}
        e["title"] = p.get("title") or attrs.get("title")
        e["image"] = attrs.get("imageLink")
        entries[_offer_id(p)] = e

    prev_state = _latest_map(ctx.c, "mc_product_state")
    prev_cache = engine.get_setting(ctx.c, "mc_products", {}) or {}
    served_before = ctx.c.execute(
        "SELECT 1 FROM snapshots WHERE source='mc' AND metric='mc_impressions'"
        " AND value>0 LIMIT 1"
    ).fetchone()

    rows = await report(engine.rome_date_str(7), engine.rome_date_str(1))

    for oid, e in entries.items():
        prev = prev_state.get(oid)
        num = STATE_NUM[e["state"]]
        ctx.record("mc_product_state", oid, num)
        if prev == STATE_NUM["in_review"] and e["state"] != "in_review":
            # mc_review_cleared: left pending_initial_policy_review_free_listings
            since = (prev_cache.get(oid) or {}).get("since")
            days = (
                (date.fromisoformat(engine.rome_today()) - date.fromisoformat(since)).days
                if since else None
            )
            span = f" after {days} days" if days is not None else ""
            ctx.alert(
                "mc_review_cleared", "win",
                f"Merchant: {oid} cleared review{span} — now {e['state']}.",
                "/merchant",
                {"offer_id": oid, "state": e["state"], "since": since, "days": days},
            )
        if e["state"] == "disapproved" and prev not in (None, STATE_NUM["disapproved"]):
            codes = ", ".join(
                i["code"] for i in e["issues"] if i["severity"] == "ERROR"
            )
            ctx.alert(
                "mc_disapproved", "warn",
                f"Merchant: {oid} disapproved ({codes}).",
                "/merchant",
                {"offer_id": oid, "issues": [i["code"] for i in e["issues"]
                                             if i["severity"] == "ERROR"]},
            )

    for r in rows:
        ctx.record("mc_impressions", r["date"], r.get("impressions") or 0)
        ctx.record("mc_clicks", r["date"], r.get("clicks") or 0)
    if served_before is None:
        first = next((r for r in rows if (r.get("impressions") or 0) > 0), None)
        if first:
            ctx.alert(
                "mc_first_serving", "win",
                f"Merchant: first impressions ({first['impressions']:.0f}) on"
                f" {first['date']} — Free Listings is serving.",
                "/merchant",
                {"date": first["date"], "impressions": first["impressions"]},
            )

    engine.set_setting(ctx.c, "mc_products", entries)
    return {"products": len(entries), "serving_rows": len(rows)}


# --- view ---

async def view(c, f: str | None = None, t: str | None = None) -> dict:
    interval = (engine.get_setting(c, "intervals", {}) or {}).get("mc", 21600)
    status = engine.source_status(c, "mc", interval)
    cache = engine.get_setting(c, "mc_products", {}) or {}

    products = []
    for oid in sorted(cache):
        e = cache[oid]
        d = engine.delta(c, "mc", "mc_product_state", oid)
        products.append({
            "offer_id": oid,
            "title": e.get("title"),
            "image": e.get("image"),
            "state": NUM_STATE.get(d["value"], e["state"]) if d else e["state"],
            "since": e.get("since"),
            "issues": e.get("issues") or [],
            "pending_countries": e.get("pending_countries") or [],
        })

    # serving: per-product daily series, live (free report), zero-filled for
    # every known product over the trailing 7 complete days
    serving_ever = c.execute(
        "SELECT 1 FROM snapshots WHERE source='mc' AND metric='mc_impressions'"
        " AND value>0 LIMIT 1"
    ).fetchone() is not None
    w_from, w_to = engine.rome_date_str(7), engine.rome_date_str(1)
    dates = [engine.rome_date_str(n) for n in range(7, 0, -1)]
    by_offer: dict[str, dict[str, dict[str, int]]] = {}
    try:
        for r in await report_products(w_from, w_to):
            by_offer.setdefault(r["offer_id"], {})[r["date"]] = {
                "impressions": int(r.get("impressions") or 0),
                "clicks": int(r.get("clicks") or 0),
            }
    except Exception:
        pass  # source isolation: serving charts degrade to zeros

    serving_products = [
        {
            "offer_id": oid,
            "series": [
                {"date": d,
                 "impressions": by_offer.get(oid, {}).get(d, {}).get("impressions", 0),
                 "clicks": by_offer.get(oid, {}).get(d, {}).get("clicks", 0)}
                for d in dates
            ],
        }
        for oid in sorted(set(by_offer) | set(cache))
    ]

    return {
        **status,
        "products": products,
        "serving": {
            "serving_ever": serving_ever,
            "products": serving_products,
        },
        "account": {
            "merchant_id": _mid(),
            "program_enabled": any(e.get("free_listings") for e in cache.values()),
        },
    }


# --- overview card ---

def card(c, days: int) -> dict:
    cache = engine.get_setting(c, "mc_products", {}) or {}
    base = {
        "source": "mc",
        "label": "Merchant",
        "page": "/merchant",
        "og": f"https://merchant.google.com/mc/products?a={_mid()}",
    }
    if not cache:
        return {**base, "headline": "never fetched", "delta_new": False,
                "lines": ["no data yet"]}

    anchor = engine.window_start(days)
    worst = None  # lowest numeric = worst (disapproved > in_review > approved)
    for oid, e in cache.items():
        d = engine.delta(c, "mc", "mc_product_state", oid, anchor=anchor)
        state = NUM_STATE.get(d["value"], e["state"]) if d else e["state"]
        if worst is None or STATE_NUM[state] < STATE_NUM[worst[0]]:
            worst = (state, e)
    state, e = worst
    if state == "in_review":
        headline = f"In review since {e['since']}" if e.get("since") else "In review"
    else:
        headline = state.capitalize()

    lines = [
        f"{oid}: {i['code']}"
        for oid, ce in sorted(cache.items())
        for i in (ce.get("issues") or [])
    ][:3]
    if not lines:
        if c.execute(
            "SELECT 1 FROM snapshots WHERE source='mc' AND metric='mc_impressions'"
            " AND value>0 LIMIT 1"
        ).fetchone():
            first = c.execute(
                "SELECT MIN(key) AS d FROM snapshots"
                " WHERE source='mc' AND metric='mc_impressions' AND value>0"
            ).fetchone()
            lines = [f"Serving since {first['d']}"]
        else:
            lines = ["Not serving yet"]
    return {**base, "headline": headline, "delta_new": False, "lines": lines}
