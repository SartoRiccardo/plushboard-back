"""Finance module + routes check (multi-leg double-entry).
Run from backend/: .venv/bin/python test_finance.py"""
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from app import db
from app.auth import serializer

db.DB_PATH = Path(tempfile.mkdtemp()) / "t.db"
db.init_db()

from app.main import app  # noqa: E402

COOKIE = {"plush_session": serializer.dumps({"email": "rikki.sarto@gmail.com"})}

with TestClient(app) as c:
    # accounts: Bank (asset, grows with Dare), Sales + Fees (revenue/expense)
    ids = {}
    for name, dare in (("Bank", True), ("Sales", False), ("Fees", True)):
        r = c.post("/api/finance/accounts", json={"name": name, "dare_increases": dare},
                   cookies=COOKIE)
        assert r.status_code == 200, r.text
        ids[name] = r.json()["id"]
    bal = {a["name"]: a["balance"] for a in
           c.get("/api/finance/accounts", cookies=COOKIE).json()}
    assert bal == {"Bank": 0, "Sales": 0, "Fees": 0}

    # balanced 2-leg EUR movement
    r = c.post("/api/finance/movements", json={
        "date": "2026-10-01", "description": "sale", "currency": "EUR",
        "legs": [{"account_id": ids["Bank"], "dare": 100},
                 {"account_id": ids["Sales"], "avere": 100}]}, cookies=COOKIE)
    assert r.status_code == 200, r.text
    assert r.json()["rate"] is None and r.json()["eur_amount"] == 200

    # 3-leg USD movement with manual rate: 59.97 dare vs 41.97+18 avere
    r = c.post("/api/finance/movements", json={
        "date": "2026-10-02", "description": "usd sale", "currency": "USD",
        "manual_rate": 0.9,
        "legs": [{"account_id": ids["Bank"], "dare": 59.97},
                 {"account_id": ids["Sales"], "avere": 41.97},
                 {"account_id": ids["Fees"], "avere": 18.0}]}, cookies=COOKIE)
    assert r.status_code == 200, r.text
    assert r.json()["rate"] == 0.9

    # unbalanced 3-leg → 400
    r = c.post("/api/finance/movements", json={
        "date": "2026-10-01", "description": "bad", "currency": "EUR",
        "legs": [{"account_id": ids["Bank"], "dare": 100},
                 {"account_id": ids["Sales"], "avere": 40},
                 {"account_id": ids["Fees"], "avere": 40}]}, cookies=COOKIE)
    assert r.status_code == 400 and "unbalanced" in r.text
    # bad currency / negative leg / unknown account → 400
    # (USD without manual_rate is valid: it fetches the live ECB rate)
    base = {"date": "2026-10-01", "description": "x"}
    for body in [
        {**base, "currency": "GBP",
         "legs": [{"account_id": ids["Bank"], "dare": 1}, {"account_id": ids["Fees"], "avere": 1}]},
        {**base, "currency": "EUR",
         "legs": [{"account_id": ids["Bank"], "dare": -1}, {"account_id": ids["Fees"], "avere": 1}]},
        {**base, "currency": "EUR",
         "legs": [{"account_id": 9999, "dare": 1}, {"account_id": ids["Fees"], "avere": 1}]},
        {**base, "currency": "EUR", "legs": [{"account_id": ids["Bank"], "dare": 1}]},
    ]:
        r = c.post("/api/finance/movements", json=body, cookies=COOKIE)
        assert r.status_code == 400, (body, r.text)

    # balances: Bank dare = 100 + 53.97; Sales avere = 100 + 37.77;
    # Fees has only avere legs → negative (expense account credited)
    bal = {a["name"]: round(a["balance"], 2) for a in
           c.get("/api/finance/accounts", cookies=COOKIE).json()}
    assert bal["Bank"] == 153.97 and bal["Sales"] == 137.77 and bal["Fees"] == -16.2

    # movements page: aggregation + totals
    r = c.get("/api/finance/movements", cookies=COOKIE)
    items = r.json()["items"]
    assert r.json()["total"] == 2 and r.json()["pages"] == 1
    usd = next(i for i in items if i["description"] == "usd sale")
    assert usd["dare_accounts"] == ["Bank"]
    assert sorted(usd["avere_accounts"]) == ["Fees", "Sales"]
    assert round(usd["eur_amount"], 2) == 107.94
    ov = c.get("/api/finance/overview", cookies=COOKIE).json()
    assert round(ov["totals"]["dare_eur"], 2) == round(ov["totals"]["avere_eur"], 2)
    assert ov["totals"]["movements"] == 2

    # delete movement; account with legs refuses deletion
    assert c.delete(f"/api/finance/movements/{items[0]['id']}",
                    cookies=COOKIE).status_code == 200
    r = c.delete(f"/api/finance/accounts/{ids['Bank']}", cookies=COOKIE)
    assert r.status_code == 400 and "movements" in r.text

    # products CRUD
    r = c.post("/api/finance/products",
               json={"name": "Mini Nareko Plush", "unit_price": 48.0}, cookies=COOKIE)
    pid = r.json()["id"]
    r = c.post("/api/finance/products", json={"name": "", "unit_price": 1}, cookies=COOKIE)
    assert r.status_code == 400
    assert c.put(f"/api/finance/products/{pid}", json={"unit_price": 41.17},
                 cookies=COOKIE).status_code == 200
    assert c.get("/api/finance/products", cookies=COOKIE).json()[0]["unit_price"] == 41.17
    assert c.delete(f"/api/finance/products/{pid}", cookies=COOKIE).status_code == 200
    assert c.delete(f"/api/finance/products/{pid}", cookies=COOKIE).status_code == 404

print("finance ok")
