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
import bisect
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
    # --- battery percentage and charging state (the UI level timeline) ---
    ui = [(r[0], r[1], int(r[2] or 0)) for r in cur.execute(
        "SELECT timestamp, Level, IsCharging "
        "FROM PLBatteryAgent_EventBackward_BatteryUI "
        "WHERE timestamp > 1000000000 ORDER BY timestamp")]

    # --- AC presence from the richer table ---
    # BatteryUI only has IsCharging, which is 0 both when unplugged AND when
    # plugged-but-full. The Battery table's ExternalConnected distinguishes
    # "on AC power" from "on battery", so we classify with it when available.
    ext = []
    if "PLBatteryAgent_EventBackward_Battery" in _tables(con):
        ext = [(r[0], int(r[1] or 0)) for r in cur.execute(
            "SELECT timestamp, ExternalConnected "
            "FROM PLBatteryAgent_EventBackward_Battery "
            "WHERE timestamp > 1000000000 ORDER BY timestamp")]
    ext_times = [e[0] for e in ext]

    def on_ac(t, charging):
        """On AC power at t? Use the most recent ExternalConnected sample
        (within 15 min); fall back to the charging flag otherwise."""
        if ext_times:
            i = bisect.bisect_right(ext_times, t) - 1
            if i >= 0 and (t - ext_times[i]) <= 900:
                return ext[i][1]
        return 1 if charging else 0        # fallback: charging implies AC

    samples = [(t, lvl, chg, on_ac(t, chg)) for t, lvl, chg in ui]

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

    # --- build segments from AC-power changes (plugged vs on battery) ---
    # Loading while charging is therefore kept SEPARATE from battery use.
    segs = []            # [on_ac, t_start, lvl_start, t_end, lvl_end, {chg}]
    for t, lvl, chg, ac in samples:
        if segs and segs[-1][0] == ac:
            s = segs[-1]
            s[3], s[4] = t, lvl
            s[5].add(chg)
        else:
            segs.append([ac, t, lvl, t, lvl, {chg}])

    # Merge AC/battery blips shorter than MIN_SEG (noise at plug/unplug edges)
    MIN_SEG = 120
    merged = []
    for s in segs:
        if merged and (s[3] - s[1]) < MIN_SEG:
            prev = merged[-1]
            prev[3], prev[4] = s[3], s[4]
            prev[5] |= s[5]
        else:
            merged.append(s)
    segs = merged

    # --- group by day (by segment start) ---
    by_day = {}
    for s in segs:
        by_day.setdefault(ts(s[1]).date(), []).append(s)

    for day in sorted(by_day):
        print(f"===== {day} =====")
        levels = [s[1] for s in samples if ts(s[0]).date() == day]
        if levels:
            print(f"  Battery: min {min(levels):.0f}%  max {max(levels):.0f}%")

        for ac, a, la, b, lb, chgs in by_day[day]:
            dur = b - a
            if ac == 1:
                note = ("charging" if chgs == {1}
                        else "plugged, not charging" if chgs == {0}
                        else "charging/plugged")
                print(f"  [AC     ] {ts(a):%H:%M}->{ts(b):%H:%M}  "
                      f"{la:.0f}% -> {lb:.0f}%  ({hm(dur)})  [{note}]")
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
