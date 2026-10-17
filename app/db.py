import sqlite3
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / "data" / "plushboard.db"

# §7 of DASHBOARD-PLAN.md, verbatim (+ IF NOT EXISTS for idempotent startup).
# Sources: plausible|gsc|mc|giphy|dataforseo|manual
DDL = """
CREATE TABLE IF NOT EXISTS snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL,
  metric TEXT NOT NULL,
  key TEXT,
  value REAL,
  fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snap ON snapshots(source, metric, key, fetched_at);

CREATE TABLE IF NOT EXISTS watermarks (source TEXT PRIMARY KEY, seen_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  rule TEXT NOT NULL,
  severity TEXT NOT NULL DEFAULT 'info',
  payload TEXT NOT NULL,
  created_at TEXT NOT NULL,
  seen INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL,
  kind TEXT NOT NULL,
  cost REAL NOT NULL DEFAULT 0,
  status TEXT NOT NULL,
  error TEXT,
  ts TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS manual_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  value REAL, note TEXT, ts TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  description TEXT,
  dare_increases INTEGER NOT NULL DEFAULT 1,  -- 1: grows with Dare (asset/expense), 0: grows with Avere (liability/revenue)
  created_at TEXT NOT NULL
);

-- multi-leg double-entry: one header per real-world transaction, one leg per
-- touched account (the owner's ledger runs 2-5 legs per movement)
CREATE TABLE IF NOT EXISTS movements (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  date TEXT NOT NULL,
  description TEXT NOT NULL,
  currency TEXT NOT NULL,
  rate REAL,                     -- native→EUR rate when currency=USD
  source TEXT NOT NULL DEFAULT 'manual',
  external_id TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS movement_legs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  movement_id INTEGER NOT NULL REFERENCES movements(id) ON DELETE CASCADE,
  account_id INTEGER NOT NULL REFERENCES accounts(id),
  dare REAL NOT NULL DEFAULT 0,
  avere REAL NOT NULL DEFAULT 0,
  eur_amount REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS products (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  unit_price REAL NOT NULL,
  currency TEXT NOT NULL DEFAULT 'EUR',
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fx_rates (
  date TEXT PRIMARY KEY,
  rate REAL NOT NULL,
  fetched_at TEXT NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")  # movement_legs ON DELETE CASCADE
    return conn


@contextmanager
def db():
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db() as c:
        c.executescript(DDL)
