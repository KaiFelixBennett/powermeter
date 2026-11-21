# Architecture

## Modules

```
powermeter/
  cli.py        argument parsing, subcommands, one-shot report/export paths
  core.py       measurement engine: owns sources, ticks, feeds the store
  sources/      one module per measurement technology, factory in __init__.py
  store.py      minute buffering, monthly CSV files, running totals in state.json
  stats.py      aggregation: totals, per-day slices, cost, uptime, device share
  gui.py        borderless tkinter widget, drag, topmost, per-source/device switches
  config.py     config.json + devices.json, key aliases, migration from v1
  sync.py       pull rows from other hosts over SSH, deduplicated append
  autostart.py  Windows Run key, Linux .desktop, systemd user unit helpers
  util.py       number formatting, logging, single-instance lock, boot time
```

The engine (`core`) is the only place that talks to sources; the widget, the CLI and
`--headless` all drive the same `core.step()`. That is why a headless server and a
desktop widget cannot disagree about the numbers.

## Why tidy long CSV rows

`zeit,geraet,quelle,sekunden,watt` — one row per interval *and source*:

- adding a source (a smart plug, an AMD GPU) never changes the schema or needs a
  migration,
- a merge of two machines is a set union keyed by `(zeit, geraet, quelle)`,
  so imports are idempotent,
- per-device and per-source queries are a filter, not a column rename,
- the file stays greppable and diffable, which is the whole point of keeping history
  in plain text.

The cost is more rows: about 40k rows per device per year at a 60 s interval, roughly
1 MB. Monthly files keep that bounded.

## What is stored, what is computed

Stored: measured watts and the seconds they applied. Computed at query time: PSU
efficiency (`watt / efficiency`), device-specific tariffs, base fee, and the "rest"
residual for display. Consequence: changing an efficiency or a price also changes
*historical* cost figures — intentionally, because the raw measurements stay untouched
and re-calibration should not require rewriting files.

## Sources and their lifecycle

Each source is `key`, `label`, `read() -> watts`. The factory builds the list per
device from `devices.json` (`type: auto` discovers what the machine actually offers).
`core` keeps a failure counter per source; after 30 consecutive failures it re-runs
discovery, so a driver update or a re-plugged cable recovers without a restart.
Sources with a `read_interval` (HTTP plugs) are polled slower than the logging tick,
and the last good reading is held while a read fails.

## Devices and hosts

A device is `{id, name, host, efficiency, price_ct_kwh, sources}`. `host` empty means
"measure this device on whatever machine runs powermeter"; `host: "linuxbox"` binds it,
so the same `devices.json` can live on three machines and each measures only its own
share. `POWERMETER_HOSTNAME` overrides the detected name (WSL2 copies the Windows
hostname). Devices that appear only in imported rows (another machine's ids) still show
up in reports with default efficiency and price.

## Multi-host: sync and merge

`powermeter sync` runs `ssh <host> <python> -m powermeter [--data DIR] export
[--since TS]` on the peer, parses the CSV on stdout and appends it through the same
deduplicating path `merge` uses. Nothing listens on a port; SSH keys are the only
credential. The last pulled timestamp per host lives in `state.json` under `sync`, so
runs are incremental. `merge <folder>` covers the offline case (USB stick, Syncthing).

## Locking

One logger per data folder is enforced: a named kernel mutex on Windows, `flock` on a
lockfile on Linux. Two loggers with different `--data` folders are allowed — that is
how separate devices on one machine (or test/demo instances) run side by side.

## Known limits

- Sources depend on what vendor firmware exposes; where the platform does not publish
  package power, only the "rest" residual approach works.
- `state.json` is a cache, not a source of truth; anything else is reconstructible
  from the CSVs.
- The widget is tkinter: it renders identically on Windows and X11/Wayland but has no
  HiDPI scaling story beyond font sizes.
