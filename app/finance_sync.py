"""Stripe/PayPal → ledger daemon.

Every `intervals.finance_sync` (default 2h) pulls new charges/transactions,
classifies them, and — unless `finance_sync_dry_run` is true or the finance
account mapping is unset — logs movements:

- Stripe charge with Ko-fi metadata  → tip   → avere = finance_accounts.kofi_avere
- Stripe charge without it, succeeded → sale → avere = finance_accounts.sales_avere
  (product attribution: fetch the public receipt page, match line-item names
   against the Products table — the charge total includes shipping, so amount
   matching is wrong; receipt matching is exact. Verified 2026-10-07:
   $59.97 receipt contains "Mini Nareko × 1 $48.00".)
- PayPal transaction (type T0006 payment, status S) → sale via subject/note
  matching (Transaction Search API; 403s until the app's "Transaction
  Search" feature is enabled).

Dedup: every movement carries source + external_id (provider tx id), so a
charge is never logged twice. Dry-run emits finance_sync_dry alerts with the
charge id instead of writing movements — flip `finance_sync_dry_run` to
false once the proposals look right.
"""
import json
import re
from datetime import datetime, timedelta, timezone

import httpx

from . import config, db, engine, finance

STRIPE_BASE = "https://api.stripe.com/v1"
PAYPAL_BASE = "https://api-m.paypal.com"


def _stripe_get(path: str, **params) -> dict:
    key = config.env("STRIPE_API_KEY")
    if not key:
        raise RuntimeError("STRIPE_API_KEY not set")
    r = httpx.get(f"{STRIPE_BASE}{path}", params=params,
                  headers={"Authorization": f"Bearer {key}"}, timeout=30)
    r.raise_for_status()
    return r.json()


def _paypal_token() -> str:
    cid, sec = config.env("PAYPAL_CLIENT_ID"), config.env("PAYPAL_SECRET") or \
        config.env("PAYPAL_CLIENT_SECRET")
    if not (cid and sec):
        raise RuntimeError("PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET not set")
    r = httpx.post(f"{PAYPAL_BASE}/v1/oauth2/token", auth=(cid, sec),
                   data={"grant_type": "client_credentials"}, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]


def _receipt_products(receipt_url: str, product_names: list[str]) -> str | None:
    """Fetch the public receipt page and return the first product name that
    matches a known product (case-insensitive substring both ways)."""
    try:
        html = httpx.get(receipt_url, timeout=30).text
    except Exception:
        return None
    text = re.sub(r"<[^>]+>", " ", html)
    for name in product_names:
        head = name.split()[0] if name.split() else name  # "Mini Nareko…" → "Nareko"
        for token in {name.lower(), head.lower()}:
            if len(token) >= 4 and re.search(re.escape(token), text, re.I):
                return name
    return None


def _stripe_since(c) -> float:
    ts = engine.get_setting(c, "finance_sync_since")
    if ts:
        return datetime.fromisoformat(ts).timestamp() - 3600  # 1h overlap, dedup handles it
    return (datetime.now(timezone.utc) - timedelta(days=30)).timestamp()


# --- classification ---

def propose_stripe(c, since_ts: float) -> list[dict]:
    """New succeeded Stripe charges → proposed ledger entries."""
    data = _stripe_get("/charges", limit=100,
                       **{"created[gte]": int(since_ts)})
    product_names = [p["name"] for p in finance.products_list(c)]
    out = []
    for ch in data.get("data", []):
        kofi = "Ko-fi" in json.dumps(ch.get("metadata") or {})
        if ch["status"] != "succeeded":
            continue
        date = datetime.fromtimestamp(ch["created"], tz=engine.ROME).date().isoformat()
        out.append({
            "source": "stripe",
            "external_id": ch["id"],
            "date": date,
            "amount": ch["amount"] / 100,
            "currency": ch["currency"].upper(),
            "kind": "kofi" if kofi else "sale",
            "product": None,
        })
        if not kofi:
            entry = out[-1]
            name = _receipt_products(ch.get("receipt_url"), product_names)
            entry["product"] = name
    return out


def propose_paypal(c, since_date: str) -> list[dict]:
    # window is clamped to PayPal's hard 31-day reporting limit
    since_date = max(since_date, (datetime.now(timezone.utc)
                                  - timedelta(days=30)).date().isoformat())
    token = _paypal_token()
    r = httpx.get(f"{PAYPAL_BASE}/v1/reporting/transactions",
                  params={"start_date": f"{since_date}T00:00:00Z",
                          "end_date": datetime.now(timezone.utc)
                          .strftime("%Y-%m-%dT%H:%M:%SZ"),
                          "fields": "all", "per_page": 100},
                  headers={"Authorization": f"Bearer {token}"}, timeout=30)
    if r.status_code == 403:
        raise RuntimeError("PayPal Transaction Search API not enabled for this app")
    r.raise_for_status()
    product_names = [p["name"] for p in finance.products_list(c)]
    out = []
    for tx in r.json().get("transaction_details", []):
        ti = tx.get("transaction_info", {})
        if ti.get("transaction_status") != "S":
            continue
        amt = ti.get("transaction_amount") or {}
        value = float(amt.get("value") or 0)
        # income only (mono-account: negatives are fees/payouts); noise floor
        # for the ±1 currency-conversion micro-tx pairs
        if value < 2.0:
            continue
        subject = ti.get("transaction_subject") or ""
        note = ti.get("transaction_note") or ""
        product = next((n for n in product_names
                        if n.lower()[:6] in (subject + note).lower()), None)
        out.append({
            "source": "paypal",
            "external_id": ti.get("transaction_id"),
            "date": (ti.get("transaction_initiation_date") or "")[:10],
            "amount": value,
            "currency": amt.get("currency_code") or "USD",
            "kind": "sale" if product else "sale",
            "product": product,
            "subject": subject[:80],
        })

    # mono-account: transfers to the bank show up as +X (in) with a matching
    # -X (out) within days — drop pairs so they don't misfile as income
    for p in out[:]:
        if p["amount"] < 0:
            continue
        pair = any(
            q is not p and abs(q["amount"] + p["amount"]) < 0.01
            and abs((datetime.fromisoformat(q["date"] + "T12:00:00Z")
                     - datetime.fromisoformat(p["date"] + "T12:00:00Z")).days) <= 2
            for q in out if q["amount"] < 0
        )
        if pair:
            out.remove(p)
    return out


# --- sync entry (called by the scheduler) ---

def _already_logged(c, source: str, external_id: str) -> bool:
    return c.execute(
        "SELECT 1 FROM movements WHERE source=? AND external_id=? LIMIT 1",
        (source, external_id),
    ).fetchone() is not None


def _log_proposed(c, p: dict, accounts: dict, dry_run: bool) -> str:
    if p["kind"] == "kofi":
        dare = accounts.get("stripe_dare")
        avere = accounts.get("kofi_avere")
        desc = "Ko-fi tip"
    else:
        dare = accounts.get("paypal_dare" if p["source"] == "paypal" else "stripe_dare")
        avere = accounts.get("sales_avere")
        desc = ("Stripe sale" if p["source"] == "stripe" else "PayPal payment") + \
            (f" — {p['product']}" if p.get("product") else "")
        if p.get("subject"):
            desc += f" ({p['subject']})"
    rate = finance.usd_rate(c, p["date"]) if p["currency"] == "USD" else None
    eur = round(p["amount"] * rate, 2) if rate else p["amount"]
    if dry_run or not (dare and avere):
        return (f"dry-run: would log {p['amount']:.2f} {p['currency']} "
                f"(€{eur:.2f}) — {desc} [{p['external_id']}]")
    finance.create_movement(
        c, p["date"], desc, p["currency"],
        [{"account_id": dare, "dare": p["amount"], "avere": 0},
         {"account_id": avere, "dare": 0, "avere": p["amount"]}],
        source=p["source"], external_id=p["external_id"], rate=rate,
    )
    return f"logged {p['amount']:.2f} {p['currency']} — {desc}"


def run_sync(dry_run: bool | None = None) -> dict:
    """Scheduler entry. Returns a summary; every action also lands in the
    alert feed when dry-run so proposals are reviewable."""
    results: dict = {"actions": [], "errors": [], "dry_run": None}
    with db.db() as c:
        accounts = engine.get_setting(c, "finance_accounts", {}) or {}
        if dry_run is None:
            dry_run = engine.get_setting(c, "finance_sync_dry_run", True)
        configured = all(accounts.get(k) for k in ("stripe_dare", "sales_avere"))
        results["dry_run"] = dry_run or not configured
        since_ts = _stripe_since(c)
        since_date = datetime.fromtimestamp(since_ts, timezone.utc).date().isoformat()
        engine.set_setting(c, "finance_sync_since", engine.now_iso())

        def surface(msg: str) -> None:
            results["actions"].append(msg)
            if results["dry_run"]:
                engine.emit_alert(c, "finance_sync_dry", "info", msg, "/finance", {})

        def propose_and_log(propose, already, source):
            for p in propose():
                try:
                    if already(p["source"], p["external_id"]):
                        continue
                    surface(_log_proposed(c, p, accounts, results["dry_run"]))
                except Exception as e:
                    results["errors"].append(f"{source} {p.get('external_id')}: {e}")

        try:
            propose_and_log(lambda: propose_stripe(c, since_ts),
                            lambda s, x: _already_logged(c, s, x), "stripe")
        except Exception as e:
            results["errors"].append(f"stripe: {e}")
        try:
            propose_and_log(lambda: propose_paypal(c, since_date),
                            lambda s, x: _already_logged(c, s, x), "paypal")
        except Exception as e:
            results["errors"].append(f"paypal: {e}")
    return results
