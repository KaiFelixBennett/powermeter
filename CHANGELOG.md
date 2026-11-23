# Changelog

All notable changes to this project are documented here; the format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), versioning follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Linux display without Tk: `powermeter widget` (GTK4/Libadwaita) shows the last hour
  as a curve, today's peak / mean / low, per-source shares as coloured bars and one row
  per device. It reads the collector's CSVs only, so the collector can stay a headless
  systemd service. `--autostart` writes the `.desktop` entries, `--service` installs a
  user unit with `Restart=always` (the window comes back after a crash or a close).
- Trend against five minutes ago, and an explicit `—` for a device that has no row in
  the newest measured minute (plus its "last seen" time) instead of a silent 0 W.
- Linux display: device detail page on click (own curve, per-source bars, daily energy
  bars, back button / `Esc`), time ranges hour / 24 h / 7 days (`--bereich`), a mini
  curve per device row, and `Ø kWh/day` in the week range.
- Import trigger: a systemd `path` unit merges pushed CSVs as soon as they arrive, with
  a five-minute timer as backstop (was fifteen) — rows from the Windows box are ~5
  minutes old instead of up to ~30.
- `powermeter sync`: pull measurement rows from other hosts over SSH
  (`ssh <host> python3 -m powermeter export --since …`), deduplicated append,
  per-host "since" marker in `state.json`, `--list` and `--command` escape hatch.
- `export --since` / `export --until` for incremental dumps.
- `powermeter mute --source … --device … [--on] [--show]` plus the same switches in the
  widget menu: silence one source or one device without editing files.
- Compact two-line widget mode when exactly one device is measured;
  "detailed table" in the widget menu switches back.
- `opacity` setting for the widget window (formerly hard-coded 0.92).
- Devices that only appear in imported rows from other hosts are counted with default
  efficiency and price instead of being silently dropped.
- English configuration keys (`devices`, `sources`, `efficiency`, `price_ct_kwh`,
  `interval_seconds`, `price_ct_kwh`, `hosts`, `file`, `fixed`, `battery`, `display`,
  `other`); the German keys of the first release keep working.
- `docs/images/overview.svg`: at-a-glance topology at the very top of the README —
  three hosts with their local CSVs, one collector pulling over ssh, the combined
  report and a per-device bar chart.
- `docs/images/architecture.svg`: rendered architecture diagram (hosts -> engine -> CSV
  -> widget/report/sync) used in the README "How it works" section.
- Tests for store/stats, sources, config/CLI and the sync path; CI matrix on
  `windows-latest` (3.9, 3.12) and `ubuntu-latest` (3.12).

### Changed
- CLI, widget and documentation are English; numbers are formatted `1,234.56`.
- Diagnostics go to `data/error.log` (was `data/fehler.log`).
- `powermeter-widget.pyw` passes its arguments through, so
  `powermeter-widget.pyw --data DIR run` works.
- CSV rows are tidy-long (`zeit,geraet,quelle,sekunden,watt`); the v1 layout
  (`cpu_w,nvidia_w,amd_w,rest_w,…`) is still read and migrated.

### Fixed
- Linux display: the range buttons were wired to `toggled` and re-activated each other,
  so one click recursed until the stack overflowed (GNOME reported it as
  "powermeter is not responding"). Now `clicked` plus a re-entrancy guard, with a
  regression test that fires the handler from inside the refresh.
- Linux display: a device reporting in bursts (imported rows from another host) counted
  as "no measurement" — missing from the total, the shares and the curve, which saw
  toothed on the import rhythm. The snapshot now covers the last five minutes, gaps up
  to ten minutes are carried forward in the total (device curves stay raw), and a late
  device shows its last value with the timestamp instead of an em dash.
- Test suite runs without PyGObject: the range-switch logic lives in `gtkdata`
  (`bereich_schalte`) and is covered there; the window-level test skips visibly when
  the GTK bindings are missing (CI has no cairo).
- Linux display: the unit "W" now shares the digits' baseline instead of being pushed
  down by hand, so `450 W` reads as one figure rather than number plus footnote.
- Linux display: source colours follow the source name, not its list position; all
  numbers use the German decimal comma; costs moved into the hero card so the device
  list is visible without scrolling.
- `sync`: a host entry's own `command` now runs through the shell, so paths with spaces
  work (was `WinError 2`).
- `sync`: the duplicate-device-id warning only fires for devices **this** machine
  measures itself; a collector that lists the other machines' devices in its
  `devices.json` (the normal setup) no longer gets a false warning.
- Device names duplicated across hosts no longer merge into one anonymous series
  (`sync` warns).
- `state.json` rebuild keeps window position and the mute switches.
- Windows Python 3.9 in CI (image no longer ships 3.9 on `ubuntu-latest`).
