"""Finance sync daemon check (mocked Stripe/PayPal/receipt HTTP).
Run from backend/: .venv/bin/python test_finance_sync.py"""
import json
import tempfile
from pathlib import Path

from app import db, engine, finance, finance_sync
from app.sources import dataforseo  # noqa: F401  (registry import side effect)

db.DB_PATH = Path(tempfile.mkdtemp()) / "t.db"
db.init_db()

RECEIPT = "<html>Mini Nareko × 1 $48.00 — Receipt from Sartouhou</html>"
CHARGE = {
    "id": "ch_test1",
    "status": "succeeded",
    "amount": 5997,
    "currency": "usd",
    "created": 1791209298,
    "metadata": {},
    "receipt_url": "https://pay.stripe.com/receipts/x",
}
KOFI = {
    "id": "ch_kofi1",
    "status": "succeeded",
    "amount": 3000,
    "currency": "usd",
    "created": 1791000000,
    "metadata": {"Ko-fi Transaction Id": "k1"},
    "receipt_url": "",
}

# --- mocks ---
finance_sync._stripe_get = lambda path, **params: {"data": [CHARGE, KOFI]}
finance_sync.httpx.get = lambda url, **kw: type(
    "R", (), {"status_code": 200, "text": RECEIPT, "json": lambda self: {}}
)()
finance_sync._paypal_token = lambda: "tok"
finance_sync.httpx.post = lambda *a, **kw: type(
    "R", (), {"status_code": 403, "json": lambda self: {}})()
finance.usd_rate = lambda c, date: 0.9


async def none():
    return None


# scheduler loop isn't running here; run_sync is sync
with db.db() as c:
    engine.set_setting(c, "store_urls", [])
    # products: "Nareko Plush" should match the receipt's "Mini Nareko"
    c.execute(
        "INSERT INTO products(name, unit_price, currency, created_at)"
        " VALUES('Nareko Plush', 48.0, 'USD', ?)", (engine.now_iso(),))
    engine.set_setting(c, "finance_accounts", {
        "stripe_dare": None, "sales_avere": None, "kofi_avere": None})

# dry-run, unconfigured: both charges propose, nothing inserts
r = finance_sync.run_sync()
actions = r["actions"]
assert any("ch_test1" in a and "dry-run" in a for a in actions), actions
assert any("ch_kofi1" in a and "Ko-fi tip" in a for a in actions), actions
assert "Nareko Plush" in next(a for a in actions if "ch_test1" in a), actions
with db.db() as q:
    n = q.execute("SELECT COUNT(*) AS n FROM movements").fetchone()["n"]
assert n == 0, n

# configure accounts + turn dry-run off: run inserts real movements
with db.db() as c:
    c.execute("INSERT INTO accounts(id, name, dare_increases, created_at)"
              " VALUES(1,'Stripe',1,?)", (engine.now_iso(),))
    c.execute("INSERT INTO accounts(id, name, dare_increases, created_at)"
              " VALUES(2,'Sales',0,?)", (engine.now_iso(),))
    c.execute("INSERT INTO accounts(id, name, dare_increases, created_at)"
              " VALUES(3,'Ko-fi tips',0,?)", (engine.now_iso(),))
    engine.set_setting(c, "finance_accounts", {
        "stripe_dare": 1, "sales_avere": 2, "kofi_avere": 3})
    engine.set_setting(c, "finance_sync_dry_run", False)

r = finance_sync.run_sync()
with db.db() as q:
    n = q.execute("SELECT COUNT(*) AS n FROM movements").fetchone()["n"]
    heads = q.execute("SELECT id, description, currency, rate, source,"
                      " external_id FROM movements ORDER BY id").fetchall()
    legs = q.execute("SELECT m.external_id, l.account_id, l.dare, l.avere,"
                     " l.eur_amount FROM movement_legs l"
                     " JOIN movements m ON m.id=l.movement_id"
                     " ORDER BY m.id, l.id").fetchall()
assert n == 2, n
sale = heads[0]
assert sale["description"] == "Stripe sale — Nareko Plush", sale["description"]
assert sale["currency"] == "USD" and sale["rate"] == 0.9
assert sale["source"] == "stripe" and sale["external_id"] == "ch_test1"
sale_legs = [l for l in legs if l["external_id"] == "ch_test1"]
assert len(sale_legs) == 2
assert sale_legs[0]["account_id"] == 1 and sale_legs[0]["dare"] == 59.97
assert sale_legs[0]["eur_amount"] == round(59.97 * 0.9, 2)
tip = heads[1]
assert tip["description"] == "Ko-fi tip" and tip["external_id"] == "ch_kofi1"
tip_legs = [l for l in legs if l["external_id"] == "ch_kofi1"]
assert {l["account_id"] for l in tip_legs} == {1, 3}

# dedupe: second run logs nothing new
r = finance_sync.run_sync()
with db.db() as q:
    n = q.execute("SELECT COUNT(*) AS n FROM movements").fetchone()["n"]
assert n == 2, n

print("finance sync ok")
