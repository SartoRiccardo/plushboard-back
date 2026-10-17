"""Engine core check: deltas, watermarks, unseen counts, alerts, status.
Run from backend/: .venv/bin/python test_engine.py"""
import tempfile
from pathlib import Path

from app import db, engine

db.DB_PATH = Path(tempfile.mkdtemp()) / "test.db"
db.init_db()

with db.db() as c:
    engine.seed_settings(c)
    assert engine.get_setting(c, "intervals")["plausible"] == 900

    # seed merge: dict-valued rows predate later seed keys — merging adds the
    # missing keys without touching user-set values
    engine.set_setting(c, "intervals", {"plausible": 60})
    engine.seed_settings(c)
    assert engine.get_setting(c, "intervals")["inspect"] == \
        engine.SEEDS["intervals"]["inspect"]
    assert engine.get_setting(c, "intervals")["plausible"] == 60
    assert engine.get_setting(c, "enabled")["inspect"] is True
    engine.set_setting(c, "intervals", {"plausible": 900})

    # baseline: first snapshot is new, counts as unseen
    engine.record(c, "plausible", "visitors_daily", "2026-09-29", 10)
    d = engine.delta(c, "plausible", "visitors_daily", "2026-09-29")
    assert d == {"value": 10, "delta": None, "new": True}, d
    assert engine.unseen_count(c, "plausible") == 1
    assert engine.source_status(c, "plausible", 900)["status"] == "never"

    # mark seen: re-anchors, everything zero
    engine.mark_seen(c, "plausible")
    d = engine.delta(c, "plausible", "visitors_daily", "2026-09-29")
    assert d == {"value": 10, "delta": 0, "new": False}, d
    assert engine.unseen_count(c, "plausible") == 0
    engine.log_run(c, "plausible", "free", 0, "ok", None)
    assert engine.source_status(c, "plausible", 900)["status"] == "fresh"

    # change after watermark: delta = latest - watermark-time value
    engine.record(c, "plausible", "visitors_daily", "2026-09-29", 14)
    d = engine.delta(c, "plausible", "visitors_daily", "2026-09-29")
    assert d == {"value": 14, "delta": 4, "new": False}, d
    assert engine.unseen_count(c, "plausible") == 1

    # delta anchors to the watermark snapshot, not the previous row
    engine.mark_seen(c, "plausible")
    engine.record(c, "plausible", "visitors_daily", "2026-09-29", 20)
    engine.record(c, "plausible", "visitors_daily", "2026-09-29", 25)
    d = engine.delta(c, "plausible", "visitors_daily", "2026-09-29")
    assert d["value"] == 25 and d["delta"] == 11, d

    # anchor param: fixed-window base instead of the watermark (the UI drops
    # mark_seen, so cards/views re-anchor deltas to a window start)
    engine.record(c, "plausible", "visitors_daily", "2026-09-30", 5)
    t0 = engine.now_iso()
    engine.record(c, "plausible", "visitors_daily", "2026-09-30", 8)
    d = engine.delta(c, "plausible", "visitors_daily", "2026-09-30", anchor=t0)
    assert d == {"value": 8, "delta": 3, "new": False}, d
    d = engine.delta(c, "plausible", "visitors_daily", "2026-09-30",
                     anchor="2000-01-01T00:00:00.000000+00:00")
    assert d == {"value": 8, "delta": None, "new": True}, d
    d = engine.delta(c, "plausible", "visitors_daily", "2026-09-30")
    assert d == {"value": 8, "delta": None, "new": True}, d  # nothing at watermark yet
    engine.mark_seen(c, "plausible")
    engine.record(c, "plausible", "visitors_daily", "2026-09-30", 11)
    d = engine.delta(c, "plausible", "visitors_daily", "2026-09-30")
    assert d == {"value": 11, "delta": 3, "new": False}, d
    # since_seen threads the anchor; days with no baseline at it count in full
    s = engine.since_seen(c, "plausible", "visitors_daily", f="2026-09-30", anchor=t0)
    assert s == {"value": 6, "delta": 6, "new": False}, s
    s = engine.since_seen(c, "plausible", "visitors_daily", f="2026-09-30",
                          anchor="2000-01-01T00:00:00.000000+00:00")
    assert s == {"value": 11, "delta": 0, "new": True}, s

    # alerts: dedupe on rule+one_liner regardless of seen state
    engine.emit_alert(c, "r1", "info", "same line", "/traffic", {})
    engine.emit_alert(c, "r1", "info", "same line", "/traffic", {})
    engine.emit_alert(c, "r1", "info", "other line", "/traffic", {})
    n = c.execute("SELECT COUNT(*) AS n FROM alerts").fetchone()["n"]
    assert n == 2, n
    c.execute("UPDATE alerts SET seen=1")
    engine.emit_alert(c, "r1", "info", "same line", "/traffic", {})
    n = c.execute("SELECT COUNT(*) AS n FROM alerts").fetchone()["n"]
    assert n == 2, n  # seen alerts don't re-fire

    # error status comes from the last run
    engine.log_run(c, "plausible", "free", 0, "error", "boom")
    st = engine.source_status(c, "plausible", 900)
    assert st["status"] == "error" and st["error"] == "boom", st

print("engine ok")
