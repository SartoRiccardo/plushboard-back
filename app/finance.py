"""Double-entry finance: accounts (conti), movements (dare/avere legs),
USD/EUR with FX. Stripe/PayPal are TBA — a future daemon will auto-log
external charges with source='stripe'|'paypal' and dedupe on external_id;
manual rows here stay untouched."""
from datetime import datetime

import httpx
from fastapi import HTTPException

from . import engine

CURRENCIES = ("USD", "EUR")


def usd_rate(c, date: str) -> float:
    """USD→EUR for `date`: fx_rates cache first, else frankfurter.dev (free
    ECB feed, no key; weekends/holidays return the previous business day's
    rate — accepted, but stored under the requested date)."""
    row = c.execute("SELECT rate FROM fx_rates WHERE date=?", (date,)).fetchone()
    if row:
        return row["rate"]
    try:
        r = httpx.get(f"https://api.frankfurter.dev/v1/{date}",
                      params={"base": "USD", "symbols": "EUR"}, timeout=15)
        r.raise_for_status()
        rate = float(r.json()["rates"]["EUR"])
    except Exception as e:
        raise RuntimeError("FX rate unavailable — set a manual rate or retry") from e
    c.execute("INSERT INTO fx_rates(date, rate, fetched_at) VALUES(?,?,?)",
              (date, rate, engine.now_iso()))
    return rate


def _account_exists(c, aid: int) -> bool:
    return bool(c.execute("SELECT 1 FROM accounts WHERE id=?", (aid,)).fetchone())


def create_movement(c, date: str, description: str, currency: str, legs: list[dict],
                    source: str = "manual", external_id: str | None = None,
                    rate: float | None = None) -> dict:
    """Insert one movement header + its legs. `legs` = [{account_id, dare,
    avere}]; every violation is a 400. `rate` is the native→EUR rate when
    currency=USD (the caller resolves it: manual_rate or usd_rate); EUR legs
    convert 1:1. Each leg's eur_amount = (dare or avere) * rate."""
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except (TypeError, ValueError):
        raise HTTPException(400, "date must be YYYY-MM-DD")
    if currency not in CURRENCIES:
        raise HTTPException(400, f"currency must be one of {list(CURRENCIES)}")
    if currency == "USD" and not (rate and rate > 0):
        raise HTTPException(400, "rate must be > 0 for USD")
    if len(legs) < 2:
        raise HTTPException(400, "a movement needs at least 2 legs")
    rows, dare_sum, avere_sum = [], 0.0, 0.0
    for leg in legs:
        dare, avere, aid = leg.get("dare") or 0, leg.get("avere") or 0, leg.get("account_id")
        if dare < 0 or avere < 0:
            raise HTTPException(400, "dare/avere must be >= 0")
        if dare == 0 and avere == 0:
            raise HTTPException(400, "each leg needs a non-zero dare or avere")
        if not _account_exists(c, aid):
            raise HTTPException(400, f"account {aid} does not exist")
        native = dare or avere
        eur = round(native * rate, 2) if rate else native
        rows.append((aid, dare, avere, eur))
        dare_sum, avere_sum = dare_sum + dare, avere_sum + avere
    if abs(dare_sum - avere_sum) > 0.01:
        raise HTTPException(400, f"unbalanced movement: dare {dare_sum} != avere {avere_sum}")
    cur = c.execute(
        "INSERT INTO movements(date, description, currency, rate, source,"
        " external_id, created_at) VALUES(?,?,?,?,?,?,?)",
        (date, description, currency, rate, source, external_id, engine.now_iso()),
    )
    mid = cur.lastrowid
    c.executemany(
        "INSERT INTO movement_legs(movement_id, account_id, dare, avere, eur_amount)"
        " VALUES(?,?,?,?,?)",
        [(mid, *r) for r in rows],
    )
    return {"ok": True, "id": mid, "rate": rate,
            "eur_amount": sum(r[3] for r in rows)}


def account_balances(c) -> list[dict]:
    """Per account, EUR-equivalent sums over its legs; balance sign follows
    dare_increases."""
    rows = c.execute(
        "SELECT a.id, a.name, a.description, a.dare_increases,"
        " COALESCE(SUM(CASE WHEN l.dare>0 THEN l.eur_amount END),0) AS dare_sum,"
        " COALESCE(SUM(CASE WHEN l.avere>0 THEN l.eur_amount END),0) AS avere_sum"
        " FROM accounts a LEFT JOIN movement_legs l ON l.account_id=a.id"
        " GROUP BY a.id ORDER BY a.id"
    ).fetchall()
    return [
        {**dict(r),
         "balance": (r["dare_sum"] - r["avere_sum"]) if r["dare_increases"]
                    else (r["avere_sum"] - r["dare_sum"])}
        for r in rows
    ]


def movements_page(c, page: int, per_page: int) -> dict:
    total = c.execute("SELECT COUNT(*) AS n FROM movements").fetchone()["n"]
    heads = c.execute(
        "SELECT id, date, description, currency, source FROM movements"
        " ORDER BY date DESC, id DESC LIMIT ? OFFSET ?",
        (per_page, (page - 1) * per_page),
    ).fetchall()
    legs: dict[int, list] = {}
    if heads:
        q = ",".join("?" * len(heads))
        for r in c.execute(
            f"SELECT l.movement_id, a.name, l.dare, l.avere, l.eur_amount"
            f" FROM movement_legs l JOIN accounts a ON a.id=l.account_id"
            f" WHERE l.movement_id IN ({q})", [h["id"] for h in heads],
        ):
            legs.setdefault(r["movement_id"], []).append(r)
    items = [
        {**dict(h),
         "dare_accounts": [r["name"] for r in legs.get(h["id"], []) if r["dare"] > 0],
         "avere_accounts": [r["name"] for r in legs.get(h["id"], []) if r["avere"] > 0],
         "eur_amount": sum(r["eur_amount"] for r in legs.get(h["id"], []))}
        for h in heads
    ]
    return {"items": items, "total": total, "page": page,
            "pages": max(1, -(-total // per_page))}


def totals(c) -> dict:
    dare = c.execute(
        "SELECT COALESCE(SUM(eur_amount),0) AS s FROM movement_legs WHERE dare>0"
    ).fetchone()["s"]
    avere = c.execute(
        "SELECT COALESCE(SUM(eur_amount),0) AS s FROM movement_legs WHERE avere>0"
    ).fetchone()["s"]
    n = c.execute("SELECT COUNT(*) AS n FROM movements").fetchone()["n"]
    return {"dare_eur": dare, "avere_eur": avere, "movements": n}


# --- products ---

def products_list(c) -> list[dict]:
    return [dict(r) for r in c.execute("SELECT * FROM products ORDER BY id")]


def product_create(c, name: str, unit_price: float, currency: str = "EUR") -> int:
    if currency not in CURRENCIES:
        raise HTTPException(400, f"currency must be one of {list(CURRENCIES)}")
    if not unit_price > 0:
        raise HTTPException(400, "unit_price must be > 0")
    cur = c.execute(
        "INSERT INTO products(name, unit_price, currency, created_at) VALUES(?,?,?,?)",
        (name, unit_price, currency, engine.now_iso()),
    )
    return cur.lastrowid


def product_update(c, pid: int, fields: dict) -> None:
    if "currency" in fields and fields["currency"] not in CURRENCIES:
        raise HTTPException(400, f"currency must be one of {list(CURRENCIES)}")
    if not c.execute("SELECT 1 FROM products WHERE id=?", (pid,)).fetchone():
        raise HTTPException(404, f"product {pid} does not exist")
    sets = ", ".join(f"{k}=?" for k in fields)  # keys from a fixed pydantic model
    c.execute(f"UPDATE products SET {sets} WHERE id=?", (*fields.values(), pid))


def product_delete(c, pid: int) -> None:
    if not c.execute("SELECT 1 FROM products WHERE id=?", (pid,)).fetchone():
        raise HTTPException(404, f"product {pid} does not exist")
    c.execute("DELETE FROM products WHERE id=?", (pid,))
