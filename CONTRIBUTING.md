# Contributing

Thanks for taking the time. Short version: small pull requests, English in code and
docs, and a test for anything measurable.

## Development setup

```bash
git clone https://github.com/KaiFelixBennett/powermeter.git
cd powermeter
python3 -m pytest -q          # no network, no admin rights, ~6 s
```

Supported Python: 3.9+ (standard library only — please keep it that way; optional
integrations such as `pynvml` are probed at runtime and must stay optional).

## Where to put things

- A new measurement technology: add a module in `powermeter/sources/`, register it in
  `sources/__init__.py`, document its `type` in the README table, add one unit test
  against a fake sysfs/HTTP fixture. Do not touch `store.py` — the tidy-long CSV
  already fits.
- A new report or aggregation: `stats.py`, and print it through `cli.py` so it is
  scriptable.
- Anything that changes the CSV layout needs a migration path plus a test that reads
  the old layout.

## Style

- English identifiers, comments and user-facing strings; numbers via `util.nf`.
- No silent failure: a source that cannot read logs once per state change
  (`util.log_error`) and keeps the last good value, it must not spam the log.
- Keep the widget keyboard-free and borderless; interactions go through the menu.

## Checks before a pull request

```bash
python3 -m pytest -q
python3 -m powermeter run --headless --duration 20
python3 -m powermeter report
```

Screenshots in a pull request that change the widget or CLI output are very welcome —
put them in the pull request discussion, not in `docs/images/` (that folder holds the
documented state of the main branch).
