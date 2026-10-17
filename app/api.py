import asyncio
import csv
import io
import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from . import config, db, engine, finance
from .auth import require_user
from .sources import SOURCES, dataforseo, plausible

router = APIRouter(prefix="/api", dependencies=[Depends(require_user)])


@router.get("/me")
async def me(user: str = Depends(require_user)):
    return {"email": user}


@router.get("/overview")
async def overview(days: int = Query(7)):
    if days not in (1, 7, 30):
        raise HTTPException(400, "days must be one of 1, 7, 30")
    with db.db() as c:
        intervals = engine.get_setting(c, "intervals", {})
        enabled = engine.get_setting(c, "enabled", {})
        sources = {}
        for sid, src in SOURCES.items():
            st = engine.source_status(c, sid, intervals.get(sid, 900))
            sources[sid] = {
                **st,
                "label": src.label,
                "free": src.free,
                "enabled": enabled.get(sid, True),
                "creds": src.has_creds(),
                "interval": intervals.get(sid, 900),
            }
        today_visitors = None
        rt = None
        try:
            rows = await plausible.timeseries(engine.rome_today(), engine.rome_today())
            today_visitors = rows[-1]["visitors"] if rows else 0
            rt = await plausible.realtime()
        except Exception:
            pass
        spark = [
            {"date": r["key"], "visitors": r["value"]}
            for r in c.execute(
                "SELECT key, value, MAX(fetched_at) FROM snapshots"
                " WHERE source='plausible' AND metric='visitors_daily'"
                " GROUP BY key ORDER BY key DESC LIMIT 30"
            )
        ][::-1]
        alerts = [
            {**json.loads(r["payload"]), "id": r["id"], "rule": r["rule"],
             "severity": r["severity"], "created_at": r["created_at"]}
            for r in c.execute(
                "SELECT * FROM alerts WHERE seen=0 ORDER BY created_at DESC LIMIT 50"
            )
        ]
        return {
            "sources": sources,
            "alerts": alerts,
            "cards": [
                src.card(c, days)
                for src in SOURCES.values() if src.card and src.has_creds()
            ],
            "today": {"visitors": today_visitors, "realtime": rt},
            "sparkline": spark,
        }


@router.get("/history/{source}/{metric}")
async def history(source: str, metric: str, key: str | None = None,
                  From: str | None = Query(None, alias="from"),
                  to: str | None = None):
    q = "SELECT key, value, fetched_at FROM snapshots WHERE source=? AND metric=?"
    args: list = [source, metric]
    if key:
        q += " AND key=?"
        args.append(key)
    if From:
        q += " AND fetched_at>=?"
        args.append(From)
    if to:
        q += " AND fetched_at<=?"
        args.append(to)
    q += " ORDER BY fetched_at"
    with db.db() as c:
        return [dict(r) for r in c.execute(q, args)]


@router.get("/alerts")
async def alerts(seen: bool = False):
    with db.db() as c:
        rows = c.execute(
            "SELECT * FROM alerts WHERE seen=? ORDER BY created_at DESC LIMIT 200",
            (int(seen),),
        ).fetchall()
        return [
            {**json.loads(r["payload"]), "id": r["id"], "rule": r["rule"],
             "severity": r["severity"], "created_at": r["created_at"], "seen": r["seen"]}
            for r in rows
        ]


class SeenBody(BaseModel):
    id: int | None = None


@router.post("/alerts/seen")
async def alerts_seen(body: SeenBody):
    with db.db() as c:
        if body.id:
            c.execute("UPDATE alerts SET seen=1 WHERE id=?", (body.id,))
        else:
            c.execute("UPDATE alerts SET seen=1 WHERE seen=0")
        return {"ok": True}


class WatermarkBody(BaseModel):
    source: str | None = None


@router.post("/watermarks/seen")
async def watermarks_seen(body: WatermarkBody):
    with db.db() as c:
        engine.mark_seen(c, body.source)
        return {"ok": True}


@router.post("/refresh")
async def refresh_all():
    free = [sid for sid, s in SOURCES.items() if s.free and s.has_creds()]
    results = await asyncio.gather(*(engine.refresh_source(sid) for sid in free))
    return dict(zip(free, results))


# manual_log + keywords/runs routes stay ahead of the /{source}/... ones below (§9)
MANUAL_KINDS = {"sale", "backlink", "gif_upload"}


@router.get("/manual_log")
async def manual_log_list(kind: str | None = None):
    q = "SELECT id, kind, value, note, ts FROM manual_log"
    args: list = []
    if kind:
        q += " WHERE kind=?"
        args.append(kind)
    q += " ORDER BY ts DESC, id DESC LIMIT 100"
    with db.db() as c:
        return [dict(r) for r in c.execute(q, args)]


class ManualLogBody(BaseModel):
    kind: str
    value: float | None = None
    note: str | None = None


@router.post("/manual_log")
async def manual_log_add(body: ManualLogBody):
    if body.kind not in MANUAL_KINDS:
        raise HTTPException(400, f"kind must be one of {sorted(MANUAL_KINDS)}")
    with db.db() as c:
        cur = c.execute(
            "INSERT INTO manual_log(kind, value, note, ts) VALUES(?,?,?,?)",
            (body.kind, body.value, body.note, engine.now_iso()),
        )
        return {"ok": True, "id": cur.lastrowid}


# Keywords (§10.6). DataForSEO is PAID (§5.5): never auto-refreshed — the only
# upstream calls are the button routes below; everything else reads snapshots.

def _series(c, source: str, metric: str, key: str, limit: int = 30) -> list[dict]:
    rows = c.execute(
        "SELECT value, fetched_at FROM snapshots"
        " WHERE source=? AND metric=? AND key=?"
        " ORDER BY fetched_at DESC, id DESC LIMIT ?",
        (source, metric, key, limit),
    ).fetchall()
    return [{"ts": r["fetched_at"], "value": r["value"]} for r in rows][::-1]


def _kw_row(c, kw: str) -> dict:
    g = engine.latest(c, "gsc", "gsc_kw_pos", kw)
    d = engine.latest(c, "dataforseo", "df_keyword_rank", kw)
    diff = engine.latest(c, "dataforseo", "df_keyword_difficulty", kw)
    return {
        "keyword": kw,
        "gsc_pos": g["value"] if g else None,
        "gsc_pos_ts": g["fetched_at"] if g else None,
        "gsc_series": _series(c, "gsc", "gsc_kw_pos", kw),
        "df_rank": d["value"] if d else None,
        "df_rank_ts": d["fetched_at"] if d else None,
        "df_rank_series": _series(c, "dataforseo", "df_keyword_rank", kw),
        "difficulty": diff["value"] if diff else None,
    }


@router.get("/keywords")
async def keywords_list():
    with db.db() as c:
        return {"tracked": [
            _kw_row(c, kw) for kw in engine.get_setting(c, "tracked_keywords", [])
        ]}


class KwsBody(BaseModel):
    kws: list[str]


@router.post("/keywords")
async def keywords_add(body: KwsBody):
    with db.db() as c:
        tracked = engine.get_setting(c, "tracked_keywords", [])
        for kw in body.kws:
            if kw and kw not in tracked:
                tracked.append(kw)
        engine.set_setting(c, "tracked_keywords", tracked)
        return {"ok": True, "tracked": tracked}


@router.delete("/keywords/{kw}")
async def keywords_remove(kw: str):
    with db.db() as c:
        tracked = engine.get_setting(c, "tracked_keywords", [])
        if kw not in tracked:
            raise HTTPException(404, f"'{kw}' is not tracked")
        tracked = [k for k in tracked if k != kw]
        engine.set_setting(c, "tracked_keywords", tracked)
        return {"ok": True, "tracked": tracked}


class KwBody(BaseModel):
    keyword: str


@router.post("/keywords/serp")
async def keywords_serp(body: KwBody):
    with db.db() as c:
        tracked = engine.get_setting(c, "tracked_keywords", [])
    if body.keyword not in tracked:
        raise HTTPException(400, f"'{body.keyword}' is not tracked")
    r = await dataforseo.serp(body.keyword)
    if "error" in r:
        raise HTTPException(502, r["error"])
    return r


@router.post("/keywords/difficulty")
async def keywords_difficulty():
    with db.db() as c:
        tracked = engine.get_setting(c, "tracked_keywords", [])
    if not tracked:
        raise HTTPException(400, "no tracked keywords")
    r = await dataforseo.difficulty(tracked)
    if "error" in r:
        raise HTTPException(502, r["error"])
    return r


@router.post("/keywords/ideas")
async def keywords_ideas(body: KwBody):
    if not body.keyword.strip():
        raise HTTPException(400, "keyword required")  # paid endpoint: no garbage upstream
    r = await dataforseo.ideas(body.keyword)
    if "error" in r:
        raise HTTPException(502, r["error"])
    return r


# Backlinks (§10.7). Same paid rules as keywords (§5.5): snapshots land only
# via the button routes below; GET /api/backlinks reads stored state and the
# manual ledger — it never calls upstream, and the timeseries is never stored.

@router.get("/backlinks")
async def backlinks_view():
    with db.db() as c:
        anchor = engine.window_start(7)  # table deltas: fixed 7d window
        rd = engine.delta(c, "dataforseo", "backlinks_refdomains",
                          dataforseo.OUR_DOMAIN, anchor=anchor)
        bl = engine.delta(c, "dataforseo", "backlinks_backlinks",
                          dataforseo.OUR_DOMAIN, anchor=anchor)
        return {
            "refdomains": rd["value"] if rd else None,
            "refdomains_delta": None if not rd or rd["new"] else rd["delta"],
            "refdomains_new": bool(rd and rd["new"]),
            "backlinks": bl["value"] if bl else None,
            "backlinks_delta": None if not bl or bl["new"] else bl["delta"],
            "series": None,  # transient chart data, never stored (§10.7)
            "manual": [dict(r) for r in c.execute(
                "SELECT id, note, value, ts FROM manual_log WHERE kind='backlink'"
                " ORDER BY ts DESC, id DESC LIMIT 100")],
        }


@router.post("/backlinks/summary")
async def backlinks_summary():
    r = await dataforseo.backlinks_summary()
    if "error" in r:
        raise HTTPException(502, r["error"])
    return r


class BacklinksTsBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    date_from: str | None = Field(None, alias="from")
    date_to: str | None = Field(None, alias="to")


@router.post("/backlinks/timeseries")
async def backlinks_timeseries(body: BacklinksTsBody):
    if not body.date_from or not body.date_to:
        raise HTTPException(400, "from and to are required (ISO dates)")
    r = await dataforseo.backlinks_timeseries(body.date_from, body.date_to)
    if "error" in r:
        raise HTTPException(502, r["error"])
    return r


@router.get("/runs")
async def runs(kind: str | None = None):
    q = "SELECT id, source, kind, cost, status, error, ts FROM runs"
    args: list = []
    if kind:
        q += " WHERE kind=?"
        args.append(kind)
    q += " ORDER BY ts DESC, id DESC LIMIT 100"
    with db.db() as c:
        return [dict(r) for r in c.execute(q, args)]


# Settings + maintenance (§10.10). Secret values never leave the server —
# the credentials array is {var, set} only.

CREDENTIAL_VARS = [
    "PLAUSIBLE_BASE_URL", "PLAUSIBLE_KEY", "GIPHY_API_KEY",
    "GOOGLE_SA_JSON_B64", "GSC_SITE_URL", "GMC_MERCHANT_ID",
    "DATAFORSEO_LOGIN", "DATAFORSEO_PASSWORD",
    "OAUTH_CLIENT_ID", "OAUTH_CLIENT_SECRET", "OAUTH_ALLOWED_EMAILS",
    "SESSION_SECRET", "PUBLIC_BASE_URL", "DASH_MCP_TOKEN",
]

EXPORT_TABLES = {"snapshots", "alerts", "runs", "settings", "manual_log",
                 "watermarks"}


def _validate_setting(k: str, v) -> None:
    if k not in engine.SEEDS:
        raise HTTPException(400, f"unknown setting '{k}'")
    if k in ("giphy_tags", "tracked_keywords", "store_urls"):
        if not (isinstance(v, list) and all(isinstance(x, str) for x in v)):
            raise HTTPException(400, f"{k} must be a list of strings")
    elif k == "retention_days":
        if not (isinstance(v, int) and v > 0):
            raise HTTPException(400, "retention_days must be a positive int")
    elif k in ("intervals", "enabled"):
        if not isinstance(v, dict):
            raise HTTPException(400, f"{k} must be an object")
        for sk, sv in v.items():
            if k == "intervals" and not (isinstance(sv, int) and sv > 0):
                raise HTTPException(400, f"intervals.{sk} must be a positive int")
            if k == "enabled" and not isinstance(sv, bool):
                raise HTTPException(400, f"enabled.{sk} must be a bool")


@router.get("/settings")
async def get_settings():
    with db.db() as c:
        return {
            "settings": {
                k: engine.get_setting(c, k) for k in engine.SEEDS
            },
            "credentials": [
                {"var": v, "set": bool(config.env(v))} for v in CREDENTIAL_VARS
            ],
        }


class SettingBody(BaseModel):
    k: str
    v: object


@router.put("/settings")
async def put_setting(body: SettingBody):
    _validate_setting(body.k, body.v)
    with db.db() as c:
        engine.set_setting(c, body.k, body.v)
    return {"ok": True, "value": body.v}


@router.post("/settings/purge")
async def purge_snapshots():
    with db.db() as c:
        retention = engine.get_setting(c, "retention_days", 180)
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=retention)
        ).isoformat(timespec="microseconds")
        cur = c.execute("DELETE FROM snapshots WHERE fetched_at < ?", (cutoff,))
        return {"ok": True, "deleted": cur.rowcount}


class WipeBody(BaseModel):
    confirm: str


@router.post("/settings/wipe")
async def wipe_data(body: WipeBody):
    if body.confirm != "wipe":
        raise HTTPException(400, 'type "wipe" to confirm')
    with db.db() as c:
        for table in ("snapshots", "alerts", "runs", "manual_log", "watermarks"):
            c.execute(f"DELETE FROM {table}")  # table names are a fixed literal set
        return {"ok": True}


@router.get("/export")
async def export(table: str, format: str = "csv"):
    if table not in EXPORT_TABLES:
        raise HTTPException(400, f"table must be one of {sorted(EXPORT_TABLES)}")
    with db.db() as c:
        cur = c.execute(f"SELECT * FROM {table}")  # whitelisted above
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
        if format == "json":
            return [dict(r) for r in rows]
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(cols)
        w.writerows([tuple(r) for r in rows])
        return Response(
            content=buf.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{table}.csv"'},
        )


# Finance (§ finance module): literal paths stay ahead of the /{source}/... catchers.
# Stripe/PayPal are TBA — movements.source/external_id are the daemon hook.

class FinanceAccountBody(BaseModel):
    name: str
    description: str | None = None
    dare_increases: bool = True


class FinanceAccountPatchBody(BaseModel):
    name: str | None = None
    description: str | None = None
    dare_increases: bool | None = None


class FinanceLegBody(BaseModel):
    account_id: int
    dare: float = 0
    avere: float = 0


class FinanceMovementBody(BaseModel):
    date: str
    description: str
    currency: str = "EUR"
    legs: list[FinanceLegBody]
    manual_rate: float | None = None


class FinanceProductBody(BaseModel):
    name: str
    unit_price: float
    currency: str = "EUR"


class FinanceProductPatchBody(BaseModel):
    name: str | None = None
    unit_price: float | None = None
    currency: str | None = None
    active: bool | None = None


@router.get("/finance/accounts")
async def finance_accounts():
    with db.db() as c:
        return finance.account_balances(c)


@router.post("/finance/accounts")
async def finance_account_add(body: FinanceAccountBody):
    if not body.name.strip():
        raise HTTPException(400, "name required")
    with db.db() as c:
        cur = c.execute(
            "INSERT INTO accounts(name, description, dare_increases, created_at)"
            " VALUES(?,?,?,?)",
            (body.name, body.description, int(body.dare_increases), engine.now_iso()),
        )
        return {"ok": True, "id": cur.lastrowid}


@router.put("/finance/accounts/{aid}")
async def finance_account_edit(aid: int, body: FinanceAccountPatchBody):
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(400, "nothing to update")
    with db.db() as c:
        if not c.execute("SELECT 1 FROM accounts WHERE id=?", (aid,)).fetchone():
            raise HTTPException(404, f"account {aid} does not exist")
        if "dare_increases" in fields:
            fields["dare_increases"] = int(fields["dare_increases"])
        sets = ", ".join(f"{k}=?" for k in fields)  # keys from a fixed pydantic model
        c.execute(f"UPDATE accounts SET {sets} WHERE id=?", (*fields.values(), aid))
        return {"ok": True}


@router.delete("/finance/accounts/{aid}")
async def finance_account_delete(aid: int):
    with db.db() as c:
        if c.execute("SELECT 1 FROM movement_legs WHERE account_id=?", (aid,)).fetchone():
            raise HTTPException(400, "account has movements — delete those first")
        cur = c.execute("DELETE FROM accounts WHERE id=?", (aid,))
        if cur.rowcount == 0:
            raise HTTPException(404, f"account {aid} does not exist")
        return {"ok": True}


@router.get("/finance/movements")
async def finance_movements(page: int = Query(1), per_page: int = Query(25)):
    if page < 1 or not 1 <= per_page <= 100:
        raise HTTPException(400, "page must be >= 1 and per_page in 1..100")
    with db.db() as c:
        return finance.movements_page(c, page, per_page)


@router.post("/finance/movements")
async def finance_movement_add(body: FinanceMovementBody):
    if body.manual_rate is not None and not body.manual_rate > 0:
        raise HTTPException(400, "manual_rate must be > 0")
    with db.db() as c:
        rate = None
        if body.currency == "USD":
            rate = (body.manual_rate if body.manual_rate is not None
                    else finance.usd_rate(c, body.date))
        return finance.create_movement(
            c, body.date, body.description, body.currency,
            [leg.model_dump() for leg in body.legs], rate=rate)


@router.delete("/finance/movements/{mid}")
async def finance_movement_delete(mid: int):
    with db.db() as c:
        cur = c.execute("DELETE FROM movements WHERE id=?", (mid,))
        if cur.rowcount == 0:
            raise HTTPException(404, f"movement {mid} does not exist")
        return {"ok": True}


@router.get("/finance/products")
async def finance_products():
    with db.db() as c:
        return finance.products_list(c)


@router.post("/finance/products")
async def finance_product_add(body: FinanceProductBody):
    if not body.name.strip():
        raise HTTPException(400, "name required")
    with db.db() as c:
        return {"ok": True, "id": finance.product_create(
            c, body.name, body.unit_price, body.currency)}


@router.put("/finance/products/{pid}")
async def finance_product_edit(pid: int, body: FinanceProductPatchBody):
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(400, "nothing to update")
    if "active" in fields:
        fields["active"] = int(fields["active"])
    with db.db() as c:
        finance.product_update(c, pid, fields)
        return {"ok": True}


@router.delete("/finance/products/{pid}")
async def finance_product_delete(pid: int):
    with db.db() as c:
        finance.product_delete(c, pid)
        return {"ok": True}


@router.get("/finance/overview")
async def finance_overview():
    with db.db() as c:
        return {"accounts": finance.account_balances(c), "totals": finance.totals(c)}


@router.get("/{source}/latest")
async def latest(source: str, From: str | None = Query(None, alias="from"),
                 to: str | None = None):
    src = SOURCES.get(source)
    if src is None:
        raise HTTPException(404, f"unknown source {source}")
    if not src.free:
        raise HTTPException(409, "paid source — use its refresh button")
    if not src.has_creds():
        raise HTTPException(503, f"{source} not configured")
    if src.view is None:
        raise HTTPException(501, f"{source} view not implemented yet")
    if not From:
        From = engine.rome_date_str(6)  # fixed 7d default — no watermark anchoring
    to = to or engine.rome_today()
    with db.db() as c:
        return await src.view(c, From, to)


@router.post("/{source}/refresh")
async def refresh_one(source: str):
    src = SOURCES.get(source)
    if src is None:
        raise HTTPException(404, f"unknown source {source}")
    if not src.free:
        raise HTTPException(409, "paid source — use its refresh button")
    if not src.has_creds():
        raise HTTPException(503, f"{source} not configured")
    return await engine.refresh_source(source)


class InspectBody(BaseModel):
    path: str


@router.post("/{source}/inspect")
async def inspect(source: str, body: InspectBody):
    src = SOURCES.get(source)
    if src is None or src.inspect is None:
        raise HTTPException(404, f"{source} has no inspect")
    with db.db() as c:
        allowed = engine.get_setting(c, "store_urls", [])
    if body.path not in allowed:
        raise HTTPException(400, f"only known store URLs can be inspected: {allowed}")
    return await src.inspect(body.path)
