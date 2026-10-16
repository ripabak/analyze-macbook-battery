#!/usr/bin/env python3
"""
analyze_battery.py - Analyze a macOS `powerd` unified-log dump for battery
charge/discharge behaviour.

It tells you, from a log file, when the machine was charged, how fast the
battery drained while in use, and how much of the drain happened while the
machine was actually awake versus asleep.

Input: a plain-text dump produced by `log show`, for example:

    log show --predicate 'subsystem == "com.apple.powerd"' --info --last 24h > powerd.log

Log markers it understands (all under subsystem com.apple.powerd):
  - [BDC] Battery percentage last X now Y
        -> battery percentage change (sampled roughly every 5 minutes)
  - Settings change for power source change to AC Power / Battery Power
        -> charger plugged in / unplugged
  - handleChargingStateUpdate State:... VBUS:... UISOC:...
        -> charging state transition (VBUS=1 means an adapter is present)
  - Entering Sleep state ... / Wake from ...
        -> sleep / wake, used to subtract idle time from "in use" time

Usage:
    python3 analyze_battery.py powerd.log
"""
import re
import sys
from datetime import datetime, timedelta

# --- markers ----------------------------------------------------------------
RE_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\.\d+(?:[+-]\d{4})?")
RE_PCT = re.compile(r"BDC\] Battery percentage last (\d+) now (\d+)")
RE_AC = re.compile(r"power source change to (AC Power|Battery Power)")
RE_CHARGE = re.compile(
    r"handleChargingStateUpdate State:(\w+) VBUS:(\d+) UISOC:(\d+)")
RE_SLEEP = re.compile(r"Entering Sleep state")
RE_WAKE = re.compile(r"(?:DarkWake|Wake) from ")

FMT = "%Y-%m-%d %H:%M:%S"


def parse(path):
    """Return four lists of events: percentage, AC source, charging, sleep."""
    pct, ac, charge, sleep = [], [], [], []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = RE_TS.match(line)
            if not m:
                continue
            t = datetime.strptime(m.group(1), FMT)

            m = RE_PCT.search(line)
            if m:
                pct.append((t, int(m.group(1)), int(m.group(2))))
                continue
            m = RE_AC.search(line)
            if m:
                ac.append((t, m.group(1)))
                continue
            m = RE_CHARGE.search(line)
            if m:
                charge.append((t, m.group(1), int(m.group(3))))
                continue
            if RE_SLEEP.search(line):
                sleep.append((t, "sleep"))
            elif RE_WAKE.search(line):
                sleep.append((t, "wake"))
    return pct, ac, charge, sleep


def hm(td):
    """Format a timedelta as 'Xh Ym Zs'."""
    s = int(td.total_seconds())
    return f"{s // 3600}h {(s % 3600) // 60}m {s % 60}s"


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    path = sys.argv[1]

    pct, ac, charge, sleep = parse(path)
    if not pct:
        print("No battery-percentage data found. Is this a powerd log?")
        return

    print(f"File       : {path}")
    print(f"Log range  : {pct[0][0]}  ->  {pct[-1][0]}")
    print(f"Percentage samples: {len(pct)}")
    print()

    # --- 1. charger plug / unplug events ------------------------------------
    print("=== Power-source changes (charger) ===")
    if ac:
        for t, src in ac:
            print(f"  {t}  -> {src}")
    else:
        print("  (none; assuming always on battery)")
    print()

    # --- 2. charging sessions ------------------------------------------------
    print("=== Charging sessions (from handleChargingStateUpdate) ===")
    for t, state, uisoc in charge:
        print(f"  {t}  {state:12s}  UISOC={uisoc}%")
    print()

    # --- 3. split the timeline by AC state -----------------------------------
    events = [(t, "AC" if src == "AC Power" else "BATT") for t, src in ac]
    if not events or events[0][1] == "AC":
        events.insert(0, (pct[0][0], "BATT"))

    def state_at(t):
        cur = "BATT"
        for et, st in events:
            if et <= t:
                cur = st
            else:
                break
        return cur

    segments, cur_state, seg_start, prev = [], state_at(pct[0][0]), pct[0], pct[0]
    for t, last, now in pct[1:]:
        st = state_at(t)
        if st != cur_state:
            segments.append((cur_state, seg_start[0], seg_start[2], prev[0], prev[2]))
            cur_state, seg_start = st, (t, last, now)
        prev = (t, last, now)
    segments.append((cur_state, seg_start[0], seg_start[2], prev[0], prev[2]))

    # --- 4. per-segment summary ---------------------------------------------
    print("=== Per-segment summary ===")
    for st, t0, p0, t1, p1 in segments:
        dur, delta = t1 - t0, p1 - p0
        hours = dur.total_seconds() / 3600
        if st == "BATT" and delta < 0 and hours > 0:
            rate = -delta / hours
            print(f"[DISCHARGE] {t0} ({p0}%) -> {t1} ({p1}%)")
            print(f"    dropped {p0 - p1} points in {hm(dur)}  "
                  f"=> {rate:.2f} %/h  (full 100% ~ {100 / rate:.1f} h)")
        else:
            print(f"[{st}] {t0} ({p0}%) -> {t1} ({p1}%)  duration {hm(dur)}")
        print()

    # --- 5. active vs asleep discharge rate ---------------------------------
    print("=== DISCHARGE rate (awake vs asleep) ===")
    for st, t0, p0, t1, p1 in segments:
        if st != "BATT" or p1 >= p0:
            continue
        sleep_sec, asleep_at = 0.0, None
        for ts, kind in sleep:
            if kind == "sleep" and t0 <= ts <= t1 and asleep_at is None:
                asleep_at = ts
            elif kind == "wake" and asleep_at is not None and ts <= t1:
                sleep_sec += (ts - asleep_at).total_seconds()
                asleep_at = None
        if asleep_at is not None:
            sleep_sec += (t1 - asleep_at).total_seconds()

        total = (t1 - t0).total_seconds()
        active = max(total - sleep_sec, 1)
        drop = p0 - p1
        print(f"  {t0} ({p0}%) -> {t1} ({p1}%)")
        print(f"      total {hm(t1 - t0)} | asleep {hm(timedelta(seconds=sleep_sec))} "
              f"| active {hm(timedelta(seconds=active))}")
        print(f"      rate while AWAKE: {drop / (active / 3600):.2f} %/h "
              f"(full 100% ~ {100 * (active / 3600) / drop:.1f} h)")
        print()


if __name__ == "__main__":
    main()
