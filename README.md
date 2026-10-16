# CheckBattery

Analyze macOS battery behaviour from system logs: **when the Mac was charging,
how fast the battery drains while in use, how much of the time is spent asleep,
and how the percentage evolves over days.**

Two complementary tools:

| Script | Source | Best for |
|---|---|---|
| `analyze_battery.py` | a `log show` **powerd** text dump | Last ~1–3 days, awake-vs-asleep drain rate |
| `battery_history.py` | the **Powerlog SQLite** database | Weeks–months: charge sessions, use vs. sleep, per-day % |

Both are dependency-free (Python 3 standard library only).

---

## Why two scripts?

macOS stores power data in two places with very different retention:

| Source | Path | Typical retention |
|---|---|---|
| Unified log (`powerd`) | `log show --predicate 'subsystem == "com.apple.powerd"'` | **~1–3 days** (ring buffer) |
| Powerlog database | `/var/db/powerlog/Library/BatteryLife/CurrentPowerlog.PLSQL` | **weeks–months** (rotates into `Archives/`) |
| `pmset -g log` | `/var/log/powermanagement` | ~1 day on a busy machine |

So `log show` is great for a detailed recent picture, while the Powerlog
database is the right source for weekly/monthly history.

> ⚠️ **`log show` trap:** if you request a time range that is *older than the
> retention window*, `log show` does **not** error — it silently returns the
> most recent data instead. Always verify the first/last timestamp before
> trusting a dump:
> ```bash
> log show --last 90d --style compact --predicate 'subsystem == "com.apple.powerd"' | sed -n '2p'
> ```
> If that prints today's date, the older range no longer exists.

---

## Requirements

- macOS
- Python 3.8+ (no third-party packages)
- For `battery_history.py`: the Powerlog DB is world-readable, so **no sudo**
  is needed to read it.

---

## Quick start

### 1. Recent behaviour (unified log)

```bash
# Collect a dump (add --info; powerd messages are info/default level)
log show --predicate 'subsystem == "com.apple.powerd"' --info --last 24h > powerd.log

# Analyze it
python3 analyze_battery.py powerd.log
```

### 2. Long-range history (Powerlog)

```bash
python3 battery_history.py            # all available data
python3 battery_history.py --days 30  # last 30 days
python3 battery_history.py --db /path/to/CurrentPowerlog.PLSQL
```

---

## Sample output

`analyze_battery.py`:

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

`battery_history.py`:

```
===== 2026-10-15 =====
  Battery: min 26%  max 100%
  [BATTERY ] 02:52->11:00  100% -> 78%
      total 8h 7m | asleep 5h 46m | awake 2h 21m | rate while awake: 9.3 %/h
  [CHARGING] 11:19->13:04  84% -> 100%  (1h 45m)
  [BATTERY ] 13:04->23:36  100% -> 30%
      total 10h 32m | asleep 3h 59m | awake 6h 32m | rate while awake: 10.7 %/h
  Sleep periods (15x):
     03:53->09:27    5h 33m  Clamshell Sleep
     16:55->19:02    2h 7m   Clamshell Sleep
     ...
```

---

## How it works

### `analyze_battery.py` — key log markers

| Marker in the log | Meaning |
|---|---|
| `[BDC] Battery percentage last X now Y` | battery % change (~every 5 min) |
| `Settings change for power source change to AC Power / Battery Power` | charger plugged / unplugged |
| `handleChargingStateUpdate State:... VBUS:... UISOC:...` | charging state transition |
| `Entering Sleep state ...` / `Wake from ...` | sleep / wake |

It splits the timeline into segments by AC state, then computes:

```
drain rate (%/h)   = points dropped / hours awake
full runtime (h)   ≈ 100 / drain rate
```

Sleep time is subtracted so the "rate while awake" reflects real usage, not
idle/sleep.

### `battery_history.py` — Powerlog tables

| Table | Columns used |
|---|---|
| `PLBatteryAgent_EventBackward_BatteryUI` | `timestamp, Level, IsCharging` |
| `PLSleepWakeAgent_EventForward_PowerState` | `State, SleepTriggers` |

Sleep decoding (verified empirically against `log show`):

- `State = 1` **and** a `SleepTriggers` text (`Idle Sleep`, `Clamshell Sleep`, …)
  → sleep onset
- `State = 0` → full wake (matches display-on events)

For richer data, `PLBatteryAgent_EventBackward_Battery` also has
`ExternalConnected`, `CycleCount`, `MaxCapacity`, `DesignCapacity`,
`Temperature`, `Amperage`, etc.

---

## Collecting long-term history

Because retention is limited, snapshot the data yourself if you want a
permanent record. Example daily job:

```bash
# Read-only backup of the Powerlog DB
sqlite3 /var/db/powerlog/Library/BatteryLife/CurrentPowerlog.PLSQL \
  ".backup '/path/to/snapshots/powerlog-$(date +%F).sqlite'"

# Or append the percentage table to a CSV
sqlite3 -csv /var/db/powerlog/Library/BatteryLife/CurrentPowerlog.PLSQL \
  "SELECT datetime(timestamp,'unixepoch','localtime'), Level, IsCharging \
   FROM PLBatteryAgent_EventBackward_BatteryUI WHERE timestamp>1000000000" \
  >> battery_history.csv
```

Schedule it with `launchd` or `cron`, and within a month you'll have a full
history.

---

## Caveats

- `powerd` logs do **not** measure energy (Joules) per app. These tools rank
  *who keeps the system awake* (power assertions) and compute drain rates.
  For per-app energy, use `sudo powermetrics --samplers tasks` or
  `top -o power` / Activity Monitor's Energy tab.
- Battery health (cycle count, max capacity) is **not** in `log show`. Read it
  from the Powerlog DB or with:
  ```bash
  system_profiler SPPowerDataType | grep -E "Cycle Count|Condition|Maximum Capacity"
  ```
- Some powerd fields are logged as `<private>` (redacted) unless a logging
  profile is installed.
- Percentage samples are periodic (~5 min), not per-second.

---

## License

MIT — see [LICENSE](LICENSE).
