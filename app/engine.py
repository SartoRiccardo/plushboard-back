"""Core mechanics (§6): snapshots, watermarks, deltas, alerts, runs, settings."""
import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import db

log = logging.getLogger("plushboard")
ROME = ZoneInfo("Europe/Rome")


def now_iso() -> str:
    # microseconds: second resolution lets a post-mark_seen snapshot tie on
    # fetched_at <= seen_at and zero out its own delta
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def rome_today() -> str:
    return datetime.now(ROME).date().isoformat()


def rome_date_str(days_back: int) -> str:
    return (datetime.now(ROME).date() - timedelta(days=days_back)).isoformat()


def rome_date_of(iso_ts: str) -> str:
    return datetime.fromisoformat(iso_ts).astimezone(ROME).date().isoformat()


def window_start(days: int) -> str:
    """Fixed-window anchor for deltas: now UTC − days, same format as now_iso()."""
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(
        timespec="microseconds")


def window_phrase(days: int) -> str:
    return {1: "today", 7: "this week", 30: "this month"}[days]


# --- settings ---

def get_setting(c, k: str, default=None):
    row = c.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
    return json.loads(row["v"]) if row else default


def set_setting(c, k: str, v) -> None:
    c.execute(
        "INSERT INTO settings(k, v) VALUES(?, ?)"
        " ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, json.dumps(v)),
    )


def since_seen(c, source: str, metric: str, f: str | None = None,
               t: str | None = None, anchor: str | None = None) -> dict:
    """Σ of a daily metric's deltas since the watermark, or since `anchor`
    (window summaries pass one); never-baselined days count their full value.
    {value, delta, new}"""
    total, delta_sum, new = 0.0, 0.0, False
    for r in c.execute(
        "SELECT DISTINCT key FROM snapshots WHERE source=? AND metric=?"
        " AND (? IS NULL OR key>=?) AND (? IS NULL OR key<=?)",
        (source, metric, f, f, t, t),
    ):
        d = delta(c, source, metric, r["key"], anchor)
        if d is None:
            continue
        if d["new"]:
            new = True
            total += d["value"]
        else:
            delta_sum += d["delta"]
            total += max(0, d["delta"])
    return {"value": total, "delta": delta_sum, "new": new}


SEEDS: dict[str, object] = {
    # seconds; metricool axed from the plan; "inspect" = gsc URL inspection as
    # its own 12h background job
    "intervals": {"plausible": 900, "gsc": 21600, "mc": 21600, "giphy": 21600,
                  "inspect": 43200, "finance_sync": 7200},
    "enabled": {"plausible": True, "gsc": True, "mc": True, "giphy": True,
                "inspect": True, "finance_sync": True},
    "giphy_tags": [
        "nareko", "nareko michigami", "houraisan kaguya", "touhou",
        "dissolve", "ammonite", "anvil", "cant sleep",
    ],
    "tracked_keywords": [
        "touhou plush", "touhou plushies", "touhou project plush", "touhou merch",
        "nareko plush", "nareko michigami plush", "nareko fumo",
        "kaguya touhou plush", "kaguya houraisan plush", "cirno plush",
        "touhou characters",
    ],
    "store_urls": ["/", "/nareko-michigami-plush", "/kaguya-houraisan-plush"],
    "retention_days": 180,
    # §11 catalog, each toggleable in Settings
    "alert_rules": {
        "mc_review_cleared": True, "mc_disapproved": True, "mc_first_serving": True,
        "gsc_first_click": True, "gsc_new_query": True, "gsc_index_flip": True,
        "plausible_new_source": True, "plausible_spike": True,
        "giphy_indexed": True, "giphy_tag_moved": True,
        "df_rank_change": True, "backlinks_change": True,
    },
    # finance daemon: dry-run (proposals land in the alert feed) until these
    # account ids are set; then charges auto-log as movements
    "finance_accounts": {"stripe_dare": None, "paypal_dare": None,
                         "sales_avere": None, "kofi_avere": None},
    "finance_sync_dry_run": True,
}


def seed_settings(c) -> None:
    for k, v in SEEDS.items():
        if isinstance(v, dict):
            # an existing row predates later seed keys and INSERT OR IGNORE
            # would keep it that way (e.g. a new interval falls back to the
            # 900s scheduler default) — merge in the keys it lacks, never
            # touching what the user already has
            row = c.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
            if row:
                cur = json.loads(row["v"])
                cur.update({sk: sv for sk, sv in v.items() if sk not in cur})
                c.execute("UPDATE settings SET v=? WHERE k=?", (json.dumps(cur), k))
                continue
        c.execute(
            "INSERT OR IGNORE INTO settings(k, v) VALUES(?, ?)", (k, json.dumps(v))
        )


# --- snapshots / watermarks / deltas ---

def record(c, source: str, metric: str, key: str | None, value: float) -> None:
    c.execute(
        "INSERT INTO snapshots(source, metric, key, value, fetched_at) VALUES(?,?,?,?,?)",
        (source, metric, key, value, now_iso()),
    )


def latest(c, source: str, metric: str, key: str | None = None):
    if key is None:
        return c.execute(
            "SELECT * FROM snapshots WHERE source=? AND metric=? AND key IS NULL"
            " ORDER BY fetched_at DESC, id DESC LIMIT 1", (source, metric),
        ).fetchone()
    return c.execute(
        "SELECT * FROM snapshots WHERE source=? AND metric=? AND key=?"
        " ORDER BY fetched_at DESC, id DESC LIMIT 1", (source, metric, key),
    ).fetchone()


def at_watermark(c, source: str, metric: str, key: str | None, seen_at: str):
    if key is None:
        return c.execute(
            "SELECT * FROM snapshots WHERE source=? AND metric=? AND key IS NULL"
            " AND fetched_at<=? ORDER BY fetched_at DESC, id DESC LIMIT 1",
            (source, metric, seen_at),
        ).fetchone()
    return c.execute(
        "SELECT * FROM snapshots WHERE source=? AND metric=? AND key=?"
        " AND fetched_at<=? ORDER BY fetched_at DESC, id DESC LIMIT 1",
        (source, metric, key, seen_at),
    ).fetchone()


def watermark(c, source: str) -> str | None:
    row = c.execute("SELECT seen_at FROM watermarks WHERE source=?", (source,)).fetchone()
    return row["seen_at"] if row else None


def delta(c, source: str, metric: str, key: str | None = None,
          anchor: str | None = None) -> dict | None:
    """None = never fetched. Else {value, delta, new}: delta vs the value in
    effect at the watermark, or at `anchor` (ISO ts, e.g. engine.window_start)
    when given — fixed-window summaries re-anchor there, so deltas move
    without mark_seen. anchor=None stays watermark-anchored. new=True when no
    baseline exists yet at that time."""
    cur = latest(c, source, metric, key)
    if cur is None:
        return None
    ts = anchor or watermark(c, source)
    base = at_watermark(c, source, metric, key, ts) if ts else None
    return {
        "value": cur["value"],
        "delta": None if base is None else cur["value"] - base["value"],
        "new": base is None,
    }


def unseen_count(c, source: str) -> int:
    """Metrics whose latest snapshot differs from the watermark-time snapshot."""
    n = 0
    pairs = c.execute(
        "SELECT DISTINCT metric, key FROM snapshots WHERE source=?", (source,)
    ).fetchall()
    for row in pairs:
        d = delta(c, source, row["metric"], row["key"])
        if d and (d["new"] or (d["delta"] or 0) != 0):
            n += 1
    return n


def mark_seen(c, source: str | None = None) -> None:
    ts = now_iso()
    if source:
        c.execute(
            "INSERT INTO watermarks(source, seen_at) VALUES(?,?)"
            " ON CONFLICT(source) DO UPDATE SET seen_at=excluded.seen_at",
            (source, ts),
        )
    else:
        for sid in all_snapshot_sources(c):
            c.execute(
                "INSERT INTO watermarks(source, seen_at) VALUES(?,?)"
                " ON CONFLICT(source) DO UPDATE SET seen_at=excluded.seen_at",
                (sid, ts),
            )


def all_snapshot_sources(c) -> list[str]:
    return [r["source"] for r in c.execute("SELECT DISTINCT source FROM snapshots")]


# --- alerts ---

def emit_alert(c, rule: str, severity: str, one_liner: str, page: str,
               facts: dict) -> None:
    """Insert unless the rule is toggled off (settings `alert_rules`) or an
    alert with the same rule+one_liner already exists (any seen state —
    otherwise seen alerts re-fire on every refresh)."""
    if not get_setting(c, "alert_rules", {}).get(rule, True):
        return
    for row in c.execute("SELECT payload FROM alerts WHERE rule=?", (rule,)):
        if json.loads(row["payload"])["one_liner"] == one_liner:
            return
    c.execute(
        "INSERT INTO alerts(rule, severity, payload, created_at) VALUES(?,?,?,?)",
        (rule, severity, json.dumps(
            {"one_liner": one_liner, "page": page, "facts": facts}), now_iso()),
    )


# --- runs / source status ---

def log_run(c, source: str, kind: str, cost: float, status: str,
            error: str | None) -> None:
    c.execute(
        "INSERT INTO runs(source, kind, cost, status, error, ts) VALUES(?,?,?,?,?,?)",
        (source, kind, cost, status, error, now_iso()),
    )


def source_status(c, source: str, interval: int) -> dict:
    """fresh | stale (>2x interval) | error | never + last-ok stamp."""
    last = c.execute(
        "SELECT * FROM runs WHERE source=? ORDER BY ts DESC, id DESC LIMIT 1",
        (source,),
    ).fetchone()
    if last and last["status"] == "error":
        return {"status": "error", "error": last["error"], "last_ok": _last_ok(c, source)}
    if last is None:
        return {"status": "never", "error": None, "last_ok": None}
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(last["ts"])).total_seconds()
    return {
        "status": "stale" if age > 2 * interval else "fresh",
        "error": None,
        "last_ok": _last_ok(c, source),
    }


def _last_ok(c, source: str) -> str | None:
    row = c.execute(
        "SELECT ts FROM runs WHERE source=? AND status='ok'"
        " ORDER BY ts DESC, id DESC LIMIT 1", (source,),
    ).fetchone()
    return row["ts"] if row else None


# --- refresh machinery ---

class Ctx:
    """Per-refresh handle passed to source modules."""

    def __init__(self, c):
        self.c = c

    def record(self, metric: str, key: str | None, value: float) -> None:
        record(self.c, self.source, metric, key, value)

    def alert(self, rule: str, severity: str, one_liner: str, page: str,
              facts: dict) -> None:
        emit_alert(self.c, rule, severity, one_liner, page, facts)


async def refresh_source(sid: str) -> dict:
    from .sources import SOURCES

    src = SOURCES[sid]
    with db.db() as c:
        try:
            ctx = Ctx(c)
            ctx.source = sid
            summary = await src.refresh(ctx)
            log_run(c, sid, "free", 0, "ok", None)
            return {"ok": True, **summary}
        except Exception as e:
            log.exception("refresh %s failed", sid)
            log_run(c, sid, "free", 0, "error", str(e))
            return {"ok": False, "error": str(e)}


def due(c, sid: str, interval: int) -> bool:
    row = c.execute(
        "SELECT ts FROM runs WHERE source=? AND status='ok'"
        " ORDER BY ts DESC, id DESC LIMIT 1", (sid,),
    ).fetchone()
    if row is None:
        return True
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(row["ts"])).total_seconds()
    return age >= interval


async def scheduler_loop() -> None:
    from .sources import SOURCES

    log.info("scheduler started")
    while True:
        try:
            with db.db() as c:
                intervals = get_setting(c, "intervals", {})
                enabled = get_setting(c, "enabled", {})
                for sid, src in SOURCES.items():
                    if not src.free or not src.has_creds():
                        continue
                    if not enabled.get(sid, True):
                        continue
                    if due(c, sid, intervals.get(sid, 900)):
                        await refresh_source(sid)
                if enabled.get("finance_sync", True) and due(
                    c, "finance_sync", intervals.get("finance_sync", 7200)
                ):
                    from .finance_sync import run_sync

                    summary = run_sync()
                    # log the run so the 2h cadence actually engages (due()
                    # reads this table; without a row it would fire every tick)
                    engine.log_run(c, "finance_sync", "free", 0, "ok", None)
                    log.info("finance sync: %s", json.dumps(summary))
        except Exception:
            log.exception("scheduler tick failed")
        await asyncio.sleep(30)
