"""Merchant Center offline checks: §5.3 state interpretation + §11 alert
transitions. Run from backend/: .venv/bin/python test_mc.py
Upstream calls are faked — hits no network, needs no credentials."""
import asyncio
import tempfile
from pathlib import Path

from app import db, engine
from app.sources import mc

db.DB_PATH = Path(tempfile.mkdtemp()) / "test.db"
db.init_db()

CUR = {"p": None, "rows": []}


def prod(state: str, created="2026-09-23T10:00:00Z"):
    """Fake Product resource; the pending-review issue carries severity
    DISAPPROVED (not ERROR) — observed live 2026-09-30."""
    issues, pending = [], []
    if state == "in_review":
        issues = [{"code": mc.PENDING_REVIEW_CODE, "detail": "wait",
                   "severity": "DISAPPROVED"}]
        pending = ["US"]
    if state == "disapproved":
        issues = [{"code": "policy_violation", "detail": "bad", "severity": "ERROR"}]
    return {
        "name": "accounts/1/products/en~PLUSH~mini-x",
        "offerId": "mini-x",
        "productAttributes": {"title": "Mini X"},
        "productStatus": {
            "creationDate": created,
            "destinationStatuses": [
                {"reportingContext": "FREE_LISTINGS", "pendingCountries": pending}
            ],
            "itemLevelIssues": issues,
        },
    }


async def fake_products():
    return [CUR["p"]]


async def fake_report(f, t):
    return CUR["rows"]


mc.products = fake_products
mc.report = fake_report

# interpretation: verbatim §5.3 rules
assert mc._interpret(prod("in_review"))["state"] == "in_review"
assert mc._interpret(prod("approved"))["state"] == "approved"
assert mc._interpret(prod("disapproved"))["state"] == "disapproved"
# ERROR wins over a pending-review issue (worst state)
p = prod("in_review")
p["productStatus"]["itemLevelIssues"].append(
    {"code": "x", "detail": "x", "severity": "ERROR"})
assert mc._interpret(p)["state"] == "disapproved"
# offerId = last ~-segment of the name when the field is absent
assert mc._offer_id({"name": "accounts/1/products/en~PLUSH~mini-x"}) == "mini-x"

# transitions: baseline -> no alerts; same state -> none; clearing review wins
async def once():
    with db.db() as c:
        ctx = engine.Ctx(c)
        ctx.source = "mc"
        await mc.refresh(ctx)
        return [r["rule"] for r in c.execute("SELECT rule FROM alerts")]

CUR["p"] = prod("in_review")
assert asyncio.run(once()) == [], "baseline must not alert"
assert asyncio.run(once()) == [], "unchanged state must not alert"

CUR["p"] = prod("approved")
rules = asyncio.run(once())
assert rules == ["mc_review_cleared"], rules

CUR["p"] = prod("disapproved")
rules = asyncio.run(once())
assert rules == ["mc_review_cleared", "mc_disapproved"], rules

CUR["rows"] = [{"date": "2026-09-28", "impressions": 3, "clicks": 1}]
rules = asyncio.run(once())
assert rules == ["mc_review_cleared", "mc_disapproved", "mc_first_serving"], rules
assert asyncio.run(once()) == rules, "first_serving must fire only once"

print("mc ok")
