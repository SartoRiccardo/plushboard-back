#!/usr/bin/env python
"""Import the owner's Excel ledger (SmallPlushies Stats.xlsx) into Plushboard.

    .venv/bin/python import_xlsx.py <xlsx-path> [--force] [--dump PATH]

Sheets: BK@Conti (accounts: Nome conto, Descrizione, Bilancio, Cresce con
Avere), BK@Transazioni (movement LEGS: Data, Conto, Dare, Avere, Valuta,
Dare (EUR), Avere (EUR), Descrizione), BK@_conversion_to_eur (FX cache).

Legs accumulate into movements in sheet order until one balances —
Σdare == Σavere (EUR, within 0.01, both > 0) closes it. The owner splits
one real transaction across rows with suffixed descriptions (Ko-Fi fee
rows), so balance, not the description, is the movement boundary. Legs
keep the sheet's Dare (EUR)/Avere (EUR) verbatim, so account balances
reproduce the Bilancio column; the run ends with a verification gate that
aborts (and rolls back) unless every movement balances and every account
matches within 0.01. Rows the balance rule can't group are printed under
MANUAL CASES and left out; SPECIAL_CASES below force-groups exact rows.

Idempotence: aborts if source='import' movements already exist, unless
--force (deletes all movements first; legs cascade). Accounts are matched
by name — updated if present, created if not.

created_at conventions: movements get their ledger date (T00:00:00+00:00);
accounts get their earliest leg date, or the xlsx file mtime for leg-less
accounts (e.g. 'Utile').
"""
import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import openpyxl

sys.path.insert(0, str(Path(__file__).parent))
from app import db, finance  # noqa: E402

TOLERANCE = 0.01

# Hand-written movement specs for rows the balance rule can't group: each
# entry forces those exact sheet rows (in row order) into ONE movement with
# the given description, through the same insert + verification path as the
# automatic groups. Their rows are removed before the automatic pass runs,
# so they can never turn up as leftovers. Example — the owner splits one
# Ko-Fi sale across rows with suffixed descriptions:
#     SPECIAL_CASES = [
#         {"rows": [62, 63, 64, 65], "description": "Ko-Fi S-K6A6220622"},
#     ]
SPECIAL_CASES: list[dict] = []


def _d(v) -> str:
    """datetime/date/str -> ISO date string."""
    return v.date().isoformat() if isinstance(v, datetime) else str(v)[:10]


def _n(v) -> float:
    return float(v) if v is not None else 0.0


def read_workbook(path: str):
    wb = openpyxl.load_workbook(path, data_only=True)
    conti = [  # (name, descr, bilancio, cresce_con_avere)
        (r[0], r[1], _n(r[2]), bool(r[3]))
        for r in wb["BK@Conti"].iter_rows(min_row=2, values_only=True) if r[0]
    ]
    legs = [  # (excel_row_number, row) — sheet order, blanks skipped
        (i, r) for i, r in
        enumerate(wb["BK@Transazioni"].iter_rows(min_row=2, values_only=True), start=2)
        if r[1] or r[7]
    ]
    fx = [  # foreign currencies only — EUR rows are identity, skip entirely
        (_d(r[1]), float(r[3])) for r in
        wb["BK@_conversion_to_eur"].iter_rows(min_row=2, values_only=True)
        if r[0] and r[0] != "EUR" and r[3] is not None
    ]
    return conti, legs, fx


def _groups(legs):
    """Split legs (sheet order) into movements by accumulating until one
    balances: Σdare == Σavere (EUR), both > 0. The owner splits one
    transaction across rows with suffixed descriptions ('Ko-Fi S-...
    (PayPal fee)'), so grouping by exact description shredded those into
    unbalanced movements — balance is the boundary. The balance is computed
    on the sheet's (EUR) columns because movements genuinely mix Valuta
    (the 'Order N' groups book inventory cost in EUR legs against USD
    revenue/fee/bank legs); a movement keeps the first leg's Valuta.
    Returns (balanced groups, leftover legs) — leftovers are reported for
    hand-processing, never inserted."""
    out, grp, dare, avere = [], [], 0.0, 0.0
    for rn, r in legs:
        grp.append((rn, r))
        dare, avere = dare + _n(r[5]), avere + _n(r[6])
        if avere > 0 and abs(dare - avere) <= TOLERANCE:
            out.append(grp)
            grp, dare, avere = [], 0.0, 0.0
    return out, grp


def _print_legs(banner: str, grp) -> None:
    print(banner)
    for rn, r in grp:
        print(f"  row {rn}: {_d(r[0])} | {r[1]} | dare {_n(r[2]):.2f} |"
              f" avere {_n(r[3]):.2f} | {r[4]} | {r[7]}")


def _insert(c, names, grp, descr) -> None:
    """One movement header + its legs. First leg wins for date, Valuta
    (mixed USD/EUR groups, the orders) and hence the USD rate; eur_amount
    is verbatim from the sheet's (EUR) column."""
    first = grp[0][1]
    date, currency = _d(first[0]), first[4]
    rate = None
    if currency == "USD":
        native = _n(first[2]) + _n(first[3])
        rate = round((_n(first[5]) + _n(first[6])) / native, 6) if native else None
    cur = c.execute(
        "INSERT INTO movements(date, description, currency, rate, source,"
        " external_id, created_at) VALUES(?,?,?,?, 'import', ?, ?)",
        (date, descr, currency, rate, f"xlsx:{grp[0][0]}", f"{date}T00:00:00+00:00"))
    for _, r in grp:
        dare, avere = _n(r[2]), _n(r[3])
        c.execute(
            "INSERT INTO movement_legs(movement_id, account_id, dare, avere,"
            " eur_amount) VALUES(?,?,?,?,?)",
            (cur.lastrowid, names[r[1]], dare, avere,
             _n(r[5]) if dare else _n(r[6])))


def _unbalanced(c):
    """Import movements whose legs don't balance: Σdare != Σavere (EUR)."""
    return c.execute(
        "SELECT m.id, m.description,"
        " SUM(CASE WHEN l.dare>0 THEN l.eur_amount ELSE 0 END) AS dare_sum,"
        " SUM(CASE WHEN l.avere>0 THEN l.eur_amount ELSE 0 END) AS avere_sum"
        " FROM movements m JOIN movement_legs l ON l.movement_id = m.id"
        " WHERE m.source='import' GROUP BY m.id"
        " HAVING ABS(dare_sum - avere_sum) > ?", (TOLERANCE,)).fetchall()


def import_file(path: str, force: bool = False, dump: str | None = None) -> list[dict]:
    """Import everything and verify balances against Bilancio. Returns the
    verification rows; raises SystemExit on any mismatch (rolls back)."""
    conti, legs, fx = read_workbook(path)
    mtime = datetime.fromtimestamp(Path(path).stat().st_mtime, tz=timezone.utc)
    mtime_iso = mtime.strftime("%Y-%m-%dT00:00:00+00:00")
    first_leg = {}
    for _, r in legs:
        first_leg.setdefault(r[1], _d(r[0]) + "T00:00:00+00:00")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    with db.db() as c:
        if c.execute("SELECT 1 FROM movements WHERE source='import' LIMIT 1").fetchone():
            if not force:
                raise SystemExit("already imported — use --force to reimport")
            c.execute("DELETE FROM movements")  # legs cascade (foreign_keys=ON)
        for name, descr, _, crescere in conti:
            if c.execute("SELECT 1 FROM accounts WHERE name=?", (name,)).fetchone():
                c.execute(
                    "UPDATE accounts SET description=?, dare_increases=? WHERE name=?",
                    (descr, int(not crescere), name))
            else:
                c.execute(
                    "INSERT INTO accounts(name, description, dare_increases, created_at)"
                    " VALUES(?,?,?,?)",
                    (name, descr, int(not crescere), first_leg.get(name, mtime_iso)))
        names = {r["name"]: r["id"] for r in c.execute("SELECT id, name FROM accounts")}
        unknown = {r[1] for _, r in legs} - set(names)
        if unknown:
            raise SystemExit(f"legs reference accounts missing from BK@Conti: {unknown}")

        # hand-grouped rows first — same insert path, same verification gate
        by_row = dict(legs)
        for spec in SPECIAL_CASES:
            missing = [rn for rn in spec["rows"] if rn not in by_row]
            if missing:
                raise SystemExit(f"SPECIAL_CASES: rows not in the sheet: {missing}")
            grp = [(rn, by_row[rn]) for rn in spec["rows"]]
            dare = sum(_n(r[5]) for _, r in grp)
            avere = sum(_n(r[6]) for _, r in grp)
            if abs(dare - avere) > TOLERANCE:
                _print_legs(f"SPECIAL_CASES {spec['rows']} unbalanced:", grp)
                raise SystemExit(f"SPECIAL_CASES {spec['rows']}: dare {dare:.2f}"
                                 f" != avere {avere:.2f} — fix the row list")
            _insert(c, names, grp, spec["description"])

        used = {rn for s in SPECIAL_CASES for rn in s["rows"]}
        groups, leftover = _groups([(rn, r) for rn, r in legs if rn not in used])
        for grp in groups:
            _insert(c, names, grp, grp[0][1][7])  # first leg's description, verbatim
        if leftover:
            _print_legs("MANUAL CASES — legs that never closed into a balanced"
                        " movement (NOT imported; hand-decide these, then add"
                        " a SPECIAL_CASES entry):", leftover)
        for date, rate in fx:  # owner's rates win over the live cache
            c.execute("INSERT OR REPLACE INTO fx_rates(date, rate, fetched_at)"
                      " VALUES(?,?,?)", (date, rate, now))

        # gate 1: every imported movement must balance — an unbalanced one
        # means the grouping split a real transaction in half
        bad_moves = _unbalanced(c)
        if bad_moves:
            for m in bad_moves:
                print(f"  #{m['id']} {m['description'][:40]:<40}"
                      f" dare {m['dare_sum']:>12.4f} avere {m['avere_sum']:>12.4f}")
            raise SystemExit(f"VERIFICATION FAILED: {len(bad_moves)} movement(s) "
                             f"unbalanced by more than {TOLERANCE} — investigate "
                             "the grouping, do not fudge it")

        # verification gate: our computed balances vs the owner's Bilancio
        computed = {a["name"]: a["balance"] for a in finance.account_balances(c)}
        report = [{"account": name, "bilancio": bilancio,
                   "computed": computed.get(name, 0.0)}
                  for name, _, bilancio, _ in conti]
        bad = [r for r in report if abs(r["computed"] - r["bilancio"]) > TOLERANCE]
        if bad:
            for r in report:
                print(f"  {r['account']:<20} {r['bilancio']:>14.4f} "
                      f"{r['computed']:>14.4f} {r['computed'] - r['bilancio']:>+12.4f}")
            raise SystemExit(f"VERIFICATION FAILED: {len(bad)} account(s) off by "
                             f"more than {TOLERANCE} — investigate the pairing, "
                             "do not fudge it")
        if dump:
            dump_seed(c, dump)
    return report


def dump_seed(c, path: str) -> None:
    """Portable artifact: explicit-id INSERTs for the whole finance state."""
    lines = [
        "-- Plushboard finance seed (imported from the owner's Excel ledger",
        "-- via backend/import_xlsx.py). Apply to a database where the finance",
        "-- tables already exist (app.db DDL); ids are explicit.",
        "BEGIN TRANSACTION;",
    ]
    for table, order in (("accounts", "id"), ("movements", "id"),
                         ("movement_legs", "id"), ("fx_rates", "date")):
        cur = c.execute(f"SELECT * FROM {table} ORDER BY {order}")  # fixed literals
        cols = ", ".join(d[0] for d in cur.description)
        lines.append(f"-- {table}")
        for row in cur:
            vals = ", ".join(
                "NULL" if v is None
                else repr(v) if isinstance(v, (int, float))
                else "'" + str(v).replace("'", "''") + "'"
                for v in tuple(row))
            lines.append(f"INSERT INTO {table} ({cols}) VALUES ({vals});")
    lines.append("COMMIT;")
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("xlsx")
    ap.add_argument("--force", action="store_true",
                    help="delete existing imported movements first")
    ap.add_argument("--dump", help="also write a seed SQL dump to this path")
    args = ap.parse_args()
    rows = import_file(args.xlsx, force=args.force, dump=args.dump)
    print(f"{'account':<20} {'xlsx Bilancio':>14} {'computed':>14} {'delta':>10}")
    for r in rows:
        flag = "" if abs(r["computed"] - r["bilancio"]) <= TOLERANCE else "  <-- MISMATCH"
        print(f"{r['account']:<20} {r['bilancio']:>14.4f} {r['computed']:>14.4f} "
              f"{r['computed'] - r['bilancio']:>+12.4f}{flag}")
    with db.db() as c:
        moves = c.execute(
            "SELECT COUNT(*) AS n FROM movements WHERE source='import'").fetchone()["n"]
        unbal = _unbalanced(c)
    print(f"OK: {moves} movements ({len(unbal)} unbalanced), "
          f"{len(rows)} accounts verified within {TOLERANCE}")
