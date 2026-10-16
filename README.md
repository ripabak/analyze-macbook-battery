# CheckBattery

A simple tool to check your **MacBook battery** from macOS system logs: when it
was charged, how fast the battery drains while in use, and how much of the time
the Mac is asleep instead of being used.

Needs macOS and Python 3 (no extra packages).

---

## Legend

The reports use three states:

| Label | Meaning |
|---|---|
| `[AC]` + `[charging]` | Plugged in and charging |
| `[AC]` + `[plugged, not charging]` | Plugged in but not charging (battery full or charge limit) |
| `[BATTERY]` | Running on battery (unplugged) |

Both `[AC]` states mean the Mac is on wall power — this **includes any use while
plugged in**. Only `[BATTERY]` time is used to calculate how fast the battery
drains.

Other terms in the output:

- `asleep` / `awake` — how much of a `[BATTERY]` period the Mac was asleep vs awake
- `rate while awake` — battery drain per hour while actually awake (e.g. `14.5 %/h`)

---

## `analyze_battery.py` — recent days (from `log show`)

Best for the last ~1–3 days. Shows every charger plug/unplug and the drain rate.

```bash
# 1. make a dump of the powerd log
log show --predicate 'subsystem == "com.apple.powerd"' --info --last 24h > powerd.log

# 2. analyze it
python3 analyze_battery.py powerd.log
```

Example output:

```
=== Power-source changes (charger) ===
  2026-10-15 23:40:29  -> AC Power
  2026-10-16 01:21:09  -> Battery Power

=== Per-segment summary ===
[DISCHARGE] 2026-10-15 17:38:47 (87%) -> 2026-10-15 23:36:36 (31%)
    dropped 56 points in 5h 57m 49s  => 9.39 %/h  (full 100% ~ 10.6 h)
[AC] 2026-10-15 23:41:37 (27%) -> 2026-10-16 01:16:36 (86%)  duration 1h 34m 59s

=== DISCHARGE rate (awake vs asleep) ===
  2026-10-16 01:31:01 (89%) -> 2026-10-16 17:26:36 (15%)
      total 15h 55m 35s | asleep 10h 25m 57s | active 5h 29m 38s
      rate while AWAKE: 13.47 %/h (full 100% ~ 7.4 h)
```

---

## `battery_history.py` — long-range history (Powerlog database)

Best for weeks/months. Reads the database directly, no dump needed.

```bash
python3 battery_history.py            # all available data
python3 battery_history.py --days 30  # last 30 days
```

Example output:

```
===== 2026-10-15 =====
  Battery: min 26%  max 100%
  [BATTERY ] 02:52->11:00  100% -> 78%
      total 8h 7m | asleep 5h 46m | awake 2h 21m | rate while awake: 9.3 %/h
  [AC     ] 11:19->13:04  84% -> 100%  (1h 45m)  [charging]
  [BATTERY ] 13:04->23:36  100% -> 30%
      total 10h 32m | asleep 3h 59m | awake 6h 32m | rate while awake: 10.7 %/h
  [AC     ] 23:40->01:17  26% -> 86%  (1h 36m)  [charging]
  Sleep periods (15x):
     03:53->09:27    5h 33m  Clamshell Sleep
     16:55->19:02    2h 7m   Clamshell Sleep
     ...
```

> Reads `/var/db/powerlog/Library/BatteryLife/CurrentPowerlog.PLSQL` directly.
> Normally no `sudo` is needed; if you get a permission error, run it with
> `sudo`. Use `--db <path>` to point at a copy of the database.
