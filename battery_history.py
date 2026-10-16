#!/usr/bin/env python3
"""
battery_history.py - Long-range battery history report from the macOS Powerlog
database.

Why Powerlog and not `log show`?
  The unified log (`log show --predicate 'subsystem == "com.apple.powerd"'`)
  is a ring buffer that usually keeps only 1-3 days. The Powerlog SQLite
  database keeps far more history (weeks to months) and rotates older data
  into the Archives/ folder.

Data source (world-readable, no sudo needed to read):
  /var/db/powerlog/Library/BatteryLife/CurrentPowerlog.PLSQL

Tables used:
  PLBatteryAgent_EventBackward_BatteryUI    -> timestamp, Level, IsCharging
  PLSleepWakeAgent_EventForward_PowerState  -> State, SleepTriggers
        State=1 with a SleepTriggers text ("Idle Sleep"/"Clamshell Sleep")
        marks the onset of sleep; State=0 marks a full wake.

For a longer, human-readable log dump (24h window), see analyze_battery.py.

Usage:
    python3 battery_history.py                 # all available data
    python3 battery_history.py --days 30
    python3 battery_history.py --db /path/to/CurrentPowerlog.PLSQL
"""
import argparse
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime

DEFAULT_DB = "/var/db/powerlog/Library/BatteryLife/CurrentPowerlog.PLSQL"

REQUIRED_TABLES = (
    "PLBatteryAgent_EventBackward_BatteryUI",
    "PLSleepWakeAgent_EventForward_PowerState",
)


class PowerlogError(RuntimeError):
    """Raised with a human-friendly message when the DB cannot be read."""


def _tables(con):
    return {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def open_db(path):
    """Open the Powerlog DB, working around permissions and WAL locking.

    Tries a direct read-only open first; if that fails (e.g. the file or its
    -wal/-shm sidecars are not readable), it copies the DB to a temp dir and
    opens the copy. Raises PowerlogError with guidance if neither works.
    """
    if not os.path.exists(path):
        raise PowerlogError(f"file not found: {path}")

    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        _tables(con)                       # force an actual open
        return con
    except sqlite3.Error:
        pass

    tmpdir = tempfile.mkdtemp(prefix="powerlog-")
    base = os.path.basename(path)
    try:
        for suffix in ("", "-wal", "-shm"):
            src = path + suffix
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(tmpdir, base + suffix))
        con = sqlite3.connect(os.path.join(tmpdir, base))
        _tables(con)
        return con
    except (OSError, sqlite3.Error) as err:
        raise PowerlogError(
            f"could not read {path} ({err}).\n"
            f"Try running with sudo, or grant your terminal Full Disk Access "
            f"in System Settings > Privacy & Security.")


def ts(epoch):
    return datetime.fromtimestamp(epoch)


def hm(seconds):
    """Format a number of seconds as 'Xh Ym'."""
    s = int(seconds)
    return f"{s // 3600}h {(s % 3600) // 60}m"


def load(db, days):
    """Read battery samples and sleep intervals from the Powerlog database."""
    con = open_db(db)
    cur = con.cursor()

    missing = [t for t in REQUIRED_TABLES if t not in _tables(con)]
    if missing:
        avail = sorted(t for t in _tables(con)
                       if "Battery" in t or "Sleep" in t)
        raise PowerlogError(
            "expected table(s) missing: " + ", ".join(missing)
            + "\nAvailable battery/sleep tables: "
            + (", ".join(avail) if avail else "(none)")
            + "\nThis macOS version may store battery data differently.")
    # --- battery percentage and charging state ---
    samples = [(r[0], r[1], int(r[2] or 0)) for r in cur.execute(
        "SELECT timestamp, Level, IsCharging "
        "FROM PLBatteryAgent_EventBackward_BatteryUI "
        "WHERE timestamp > 1000000000 ORDER BY timestamp")]

    # --- sleep / wake intervals ---
    sleeps = []                       # list of (start, end, reason)
    onset = onset_reason = None
    for t, state, trig in cur.execute(
            "SELECT timestamp, State, SleepTriggers "
            "FROM PLSleepWakeAgent_EventForward_PowerState "
            "WHERE timestamp > 1000000000 ORDER BY timestamp"):
        if state == 1 and trig:       # sleep onset
            if onset is None:
                onset, onset_reason = t, trig
        elif state == 0:              # full wake
            if onset is not None:
                sleeps.append((onset, t, onset_reason))
                onset = onset_reason = None
    if onset is not None:             # still asleep at end of data
        sleeps.append((onset, samples[-1][0] if samples else onset, onset_reason))
    con.close()

    if days and samples:
        cutoff = samples[-1][0] - days * 86400
        samples = [s for s in samples if s[0] >= cutoff]
        sleeps = [s for s in sleeps if s[1] >= cutoff]

    return samples, sleeps


def overlap(a0, a1, b0, b1):
    return max(0.0, min(a1, b1) - max(a0, b0))


def asleep_seconds(t0, t1, sleeps):
    return sum(overlap(t0, t1, s0, s1) for s0, s1, _ in sleeps)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=DEFAULT_DB, help="path to CurrentPowerlog.PLSQL")
    ap.add_argument("--days", type=int, default=0, help="0 = all available data")
    args = ap.parse_args()

    try:
        samples, sleeps = load(args.db, args.days)
    except PowerlogError as err:
        print(f"Could not read the Powerlog database:\n  {err}")
        print(f"Path tried: {args.db}")
        return

    if not samples:
        print("No battery data found in this database.")
        return

    print(f"DB   : {args.db}")
    print(f"Data : {ts(samples[0][0])}  ->  {ts(samples[-1][0])} "
          f"({len(samples)} samples, {len(sleeps)} sleep periods)")
    print()

    # --- build segments from charging-state changes ---
    segs = []
    t0, l0, c0 = samples[0]
    prev = samples[0]
    for t, lvl, chg in samples[1:]:
        if chg != c0:
            segs.append((c0, t0, l0, prev[0], prev[1]))
            t0, l0, c0 = t, lvl, chg
        prev = (t, lvl, chg)
    segs.append((c0, t0, l0, prev[0], prev[1]))

    # --- group by day (by segment start) ---
    by_day = {}
    for st, a, la, b, lb in segs:
        by_day.setdefault(ts(a).date(), []).append((st, a, la, b, lb))

    for day in sorted(by_day):
        print(f"===== {day} =====")
        levels = [s[1] for s in samples if ts(s[0]).date() == day]
        if levels:
            print(f"  Battery: min {min(levels):.0f}%  max {max(levels):.0f}%")

        for st, a, la, b, lb in by_day[day]:
            dur = b - a
            if st == 1:
                print(f"  [CHARGING] {ts(a):%H:%M}->{ts(b):%H:%M}  "
                      f"{la:.0f}% -> {lb:.0f}%  ({hm(dur)})")
            else:
                slp = asleep_seconds(a, b, sleeps)
                awake = max(dur - slp, 1)
                drop = la - lb
                rate = f"{drop / (awake / 3600):.1f} %/h" if drop > 0 else "-"
                print(f"  [BATTERY ] {ts(a):%H:%M}->{ts(b):%H:%M}  "
                      f"{la:.0f}% -> {lb:.0f}%")
                print(f"      total {hm(dur)} | asleep {hm(slp)} | awake {hm(awake)} "
                      f"| rate while awake: {rate}")

        # --- sleep detail for the day ---
        day_sleeps = [(s0, s1, r) for s0, s1, r in sleeps
                      if ts(s0).date() == day or ts(s1).date() == day]
        if day_sleeps:
            print(f"  Sleep periods ({len(day_sleeps)}x):")
            for s0, s1, r in day_sleeps:
                print(f"     {ts(s0):%H:%M}->{ts(s1):%H:%M}  {hm(s1 - s0):>8}  {r}")
        print()


if __name__ == "__main__":
    main()
