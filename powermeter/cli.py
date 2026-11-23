"""Kommandozeile: powermeter run | widget | report | scan | devices | merge | export | autostart | service

Beispiele
  powermeter run                      Logger + Widgets (falls Anzeige vorhanden)
  powermeter run --headless           nur Logger (servertauglich, systemd)
  powermeter report                   heute / gesamt / pro Messtag, alle Geraete
  powermeter report --device pc       nur ein Geraet
  powermeter report --tag 2026-11-21  ein Kalendertag
  powermeter report --json            maschinenlesbar (fur Graphen/SSH)
  powermeter scan                     zeigen, welche Quellen dieser Rechner findet
  powermeter devices                  konfigurierte Geraete und ihre Quellen
  powermeter merge ~/downloads/pc2    Mess-CSVs eines anderen Rechners übernimmt
  powermeter export --month 2026-11   Messreihe als CSV (fur Auswertung)
  powermeter autostart --enable       Widget bei Anmeldung starten
  powermeter service install          systemd-User-Dienst (Linux) einrichten
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import signal
import sys
import time

from .config import resolve_paths
from .core import Core
from .stats import aggregate
from .store import HEAD, merge_data, read_rows
from .util import IS_WINDOWS, log_error, nf, set_data_dir, system_start


def _fmt_table(summe: dict, gerate: list, args_show_all: bool = False) -> list[str]:
    dash = "–"
    rows = [f"{'':<14}{'today':>12}{'total':>13}{'per day':>12}"]
    rows.append(f"{'avg power':<14}{nf(summe['w_tag']) + ' W':>12}"
                f"{nf(summe['w_gesamt']) + ' W':>13}{dash:>12}")
    rows.append(f"{'energy':<14}{nf(summe['wh_tag'] / 1000, 2) + ' kWh':>12}"
                f"{nf(summe['wh_gesamt'] / 1000, 2) + ' kWh':>13}"
                f"{nf(summe['kwh_pro_tag'], 2) + ' kWh':>12}")
    rows.append(f"{'cost':<14}{nf(summe['kosten_tag'], 2) + ' €':>12}"
                f"{nf(summe['kosten_gesamt'], 2) + ' €':>13}"
                f"{nf(summe['kosten_pro_tag'], 2) + ' €':>12}")
    rows.append(f"{'uptime':<14}{nf(summe['std_tag'], 1) + ' h':>12}"
                f"{nf(summe['std_gesamt'], 1) + ' h':>13}{nf(summe['std_pro_tag'], 1) + ' h':>12}")
    if len(summe["je_geraet"]) > 1 or args_show_all:
        rows.append("")
        rows.append("Devices (share):")
        for gid in summe["je_geraet"]:
            g = summe["je_geraet"][gid]
            dev = next((d for d in gerate if d.id == gid), None)
            preis = "" if not dev or dev.preis_ct_kwh is None else f" {nf(dev.preis_ct_kwh, 2)} ct/kWh"
            if dev is None:
                preis += " (no entry in devices.json)"
            rows.append(f"  {gid:<14}{nf(g['wh_tag'] / 1000, 2) + ' kWh':>12}"
                        f"{nf(g['wh_gesamt'] / 1000, 2) + ' kWh':>13}"
                        f"{'':<12} {(dev.name if dev else gid)}{preis}")
    return rows


def cmd_report(core: Core, args) -> int:
    ids = args.device or None
    if args.json:
        print(json.dumps(core.summary(0.0, ids), indent=1, ensure_ascii=False))
        return 0
    if args.tag:
        t = aggregate(core.store, core.devices.devices, ids, args.tag)
        print(f"{args.tag}: {nf(t.wh_steckdose / 1000, 2)} kWh · {nf(t.std, 1)} h measured · "
              f"{nf(t.watt_mittel())} W average")
        for gid, d in sorted(t.je_geraet.items()):
            print(f"  {gid:<12}{nf(d.wh_steckdose / 1000, 2):>10} kWh {nf(d.std, 1):>8} h")
        return 0
    summe = core.summary(0.0, ids)
    print("\n".join(_fmt_table(summe, core.devices.devices, args_show_all=bool(args.device))))
    tage = f"{summe['tage']} measured day" + ("s" if summe['tage'] != 1 else "")
    print(f"\n{tage} · {nf(summe['ct_kwh'], 2)} ct/kWh"
          + (f" · {summe['tarif']}" if summe["tarif"] else ""))
    if core.cfg.grundpreis_einrechnen:
        print(f"incl. base fee {nf(core.cfg.grundpreis_eur_tag, 2)} EUR/day")
    return 0


def cmd_scan(core: Core, args) -> int:
    from . import sources as srcmod

    print(f"host {core.host} · {platform.system()} {platform.release()} "
          f"(python {platform.python_version()})")
    for dev in core.devices.devices:
        owner = dev.host or "(jeder Rechner)"
        print(f"\ndevice {dev.id} – {dev.name} · host: {owner} · efficiency {dev.wirkungsgrad}")
        if not dev.owns_on(core.host):
            print(f"  bound to host '{dev.host}' - not measured on this machine")
            continue
        found = srcmod.make_sources(dev, core.host)
        if not found:
            print("  no source could be created on this machine "
                  "(driver/permission? see data/error.log)")
            continue
        for s in found:
            ok, info = s.probe()
            print(f"  {'ok ' if ok else '–  '} {s.key:<10} {s.kind:<8} {s.label:<28} {info}")
    rapl_hint = ""
    if not IS_WINDOWS:
        rapl_hint = ("\nLinux hint: RAPL files are root-owned. For unprivileged reading add the "
                     "udev rule documented in the README.")
    print("\nfirst reading needs two passes (counter deltas); let it run about 10 s."
          + rapl_hint)
    return 0


def cmd_devices(core: Core, args) -> int:
    for dev in core.devices.devices:
        print(f"{dev.id:<12} {dev.name:<24} host={dev.host or '-':<16} "
              f"eta={dev.wirkungsgrad} preis={dev.preis_ct_kwh if dev.preis_ct_kwh is not None else 'global'}")
        for q in dev.quellen:
            extra = " ".join(f"{k}={v}" for k, v in q.value.items())
            print(f"    {q.typ:<8} {q.bezeichnung:<18} {extra}")
    print(f"\nDateien: {core.paths.config} · {core.paths.devices} · {core.paths.data}")
    return 0


def cmd_widget(core: Core, args) -> int:
    """Linux-Anzeige: eigener Prozess, der nur liest (der Sammler misst weiter)."""
    from .autostart import widget_autostart_set, widget_service_install, widget_service_remove

    if args.autostart or args.no_autostart:
        print(widget_autostart_set(args.autostart and not args.no_autostart), flush=True)
        return 0
    if args.service or args.no_service:
        print(widget_service_remove() if args.no_service else widget_service_install(), flush=True)
        return 0
    try:
        from .gtkwidget import run_widget
    except ImportError as e:
        print(f"Anzeige nicht verfuegbar: {e}\n"
              "GTK4/PyGObject fehlt. Unter Windows nutzt powermeter das Tk-Widget "
              "(`powermeter run`).", flush=True)
        return 2
    return run_widget(core.paths, detail=args.detail or "", bereich=args.bereich)


def cmd_run(core: Core, args) -> int:
    from .util import SingleInstance

    # Sperre pro Datenverzeichnis (nicht pro Konto): zwei Logger auf verschiedenen
    # Datenbanken sind bewusst erlaubt, zwei auf derselben nicht.
    kennung = hashlib.sha1(str(core.paths.data).encode("utf-8")).hexdigest()[:12]
    lock = SingleInstance(f"powermeter-{kennung}")
    if not lock.acquire():
        print("powermeter is already running for this data folder - nothing to do.", flush=True)
        return 0
    if args.headless:
        return _run_headless(core, args)
    if _display_fehlt():
        print("No display detected - logger continues without a window.", flush=True)
        return _run_headless(core, args)
    try:
        from .gui import run_widget  # Import spaet: tkinter fehlt auf Headless-Systemen

        return run_widget(core)
    except Exception as e:  # Anzeige kaputt? Dann zumindest weiter messen
        log_error(f"Widget nicht moeglich: {e}")
        if args.strict:
            raise
        print(f"widget unavailable ({e}) - logger continues without a window.", flush=True)
        return _run_headless(core, args)


def _display_fehlt() -> bool:
    import os

    if IS_WINDOWS:
        return False
    return not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _run_headless(core: Core, args) -> int:
    print(f"powermeter logger: devices {', '.join(core.measurable()) or 'none'}, "
          f"interval {core.cfg.intervall_sekunden:.0f} s, Daten {core.paths.data}", flush=True)
    ende = time.time() + args.duration if args.duration else None
    try:
        while ende is None or time.time() < ende:
            core.step()
            time.sleep(core.cfg.intervall_sekunden)
        print(f"ran {args.duration:.0f} s, measurement stopped", flush=True)
    except KeyboardInterrupt:
        print("\nstopped", flush=True)
    finally:
        core.close()
    return 0


def cmd_export(core: Core, args) -> int:
    import csv

    out = open(args.out, "w", newline="", encoding="utf-8") if args.out else sys.stdout
    try:
        w = csv.writer(out)
        w.writerow(HEAD)
        files = sorted(core.paths.data.glob(f"{args.month or '????-??'}.csv"))
        for f in files:
            for r in read_rows(f, core.store.default_device, seit=args.seit):
                if args.bis and r.zeit > args.bis:
                    continue
                w.writerow([r.zeit, r.geraet, r.quelle, r.sekunden, r.watt])
    finally:
        if args.out:
            out.close()
            print(f"geschrieben: {args.out}")
    return 0


def cmd_merge(core: Core, args) -> int:
    neu, dupe = merge_data(core.paths, args.orte, core.store.default_device)
    print(f"{neu} rows imported, {dupe} duplicates skipped. totals rebuilt.")
    return 0


def cmd_sync(core: Core, args) -> int:
    from .sync import pull, remote_command, sync_all
    from .store import HEAD  # noqa: F401  (Export-Kopf bleibt identisch)

    eintraege = core.cfg.rechner
    if args.rechner:
        eintraege = [e for e in eintraege
                     if (e.get("name") or e.get("host")) in set(args.rechner)]
    if args.liste:
        if not core.cfg.rechner:
            print('no hosts configured - add them to config.json under "hosts", e.g.')
            print('  "hosts": [{"name": "linuxbox", "host": "you@192.168.1.30",')
            print('               "data": "/home/kai/.local/share/powermeter"}]')
            return 0
        for e in core.cfg.rechner:
            print(f"{(e.get('name') or e.get('host')):<16} {(e.get('host') or '(eigenes Kommando)'):<28}"
                  f" data={e.get('data') or e.get('daten') or '(default path of that host)'}")
            print(f"   command: {' '.join(remote_command(e))}")
        return 0
    if not eintraege and not args.befehl:
        print("no hosts configured (see: powermeter sync --list)")
        return 1
    marker = core.store.state.setdefault("sync", {})
    ergebnisse = []
    for e in (eintraege or [{"name": "vorgegeben", "host": "", "befehl": args.befehl}]):
        name = e.get("name") or e.get("host")
        bisher = marker.get(name)
        erg = pull(core.paths, e, month=args.month,
                   seit=None if args.month else bisher,
                   command=args.befehl or None, eigene_ids=set(core.measurable()))
        if erg.get("seit_zuletzt"):
            marker[name] = erg["seit_zuletzt"]
        ergebnisse.append(erg)
    core.store.save()
    for erg in ergebnisse:
        if erg.get("fehler"):
            print(f"{erg['rechner']}: ERROR {erg['fehler']} {erg.get('meldung','')}".rstrip())
        else:
            print(f"{erg['rechner']}: {erg['neu']} new rows, {erg['dupes']} duplicates "
                  f"(since {marker.get(erg['rechner'])})")
        if erg.get("konflikt"):
            print(f"  WARNING: device ids {erg['konflikt']} exist on both hosts - separate them "
                  f"in devices.json (otherwise daily totals cannot be told apart).")
    return 0 if all(not e.get("fehler") for e in ergebnisse) else 1


def cmd_mute(core: Core, args) -> int:
    if args.show or not (args.source or args.device):
        aus = core.store.stumme()
        for typ, label in core.stummquellen((core.measurable() or [None])[0]):
            print(f"  source {typ:<10} {'off' if typ in aus['typ'] else 'measuring'}  ({label})")
        for did in core.device_ids():
            print(f"  device {did:<10} {'off' if did in aus['geraet'] else 'measuring'}")
        return 0
    aus = not args.on
    for typ in args.source or []:
        core.store.stumm_setzen("typ", typ, aus)
    for did in args.device or []:
        core.store.stumm_setzen("geraet", did, aus)
    core._sync_stumm()
    core.detect()
    still = core.store.stumme()
    print("off:", ", ".join(still["typ"] + still["geraet"]) or "nothing")
    return 0


def cmd_autostart(core: Core, args) -> int:
    from .autostart import autostart_get, autostart_set

    if args.status or not (args.enable or args.disable):
        print("autostart: on" if autostart_get() else "autostart: off")
        return 0
    autostart_set(args.enable, headless=args.headless)
    print("autostart: on" if args.enable else "autostart: off")
    return 0


def cmd_service(core: Core, args) -> int:
    from .autostart import service_install, service_remove

    print(service_install() if args.aktion == "install" else service_remove())
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="powermeter", formatter_class=argparse.RawDescriptionHelpFormatter,
                                 description=__doc__)
    ap.add_argument("--data", help="Datenverzeichnis bzw. config.json (statt Standardpfad)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="messen und anzeigen (Logger + Widget)")
    p.add_argument("--headless", action="store_true", help="ohne Fenster (fur Server/systemd)")
    p.add_argument("--devices", nargs="*", help="nur diese Geraete messen")
    p.add_argument("--duration", type=float, help="nach N Sekunden geordnet aufhoeren (Test/CI)")
    p.add_argument("--strict", action="store_true", help="Widget-Fehler hart beenden")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("report", help="Kennzahlen anzeigen")
    p.add_argument("--device", action="append", help="nur dieses Geraet (mehrfach moeglich)")
    p.add_argument("--tag", help="ein Kalendertag YYYY-MM-DD")
    p.add_argument("--json", action="store_true", help="JSON statt Tabelle")
    p.set_defaults(fn=cmd_report)

    sub.add_parser("scan", help="gefundene Quellen zeigen").set_defaults(fn=cmd_scan)
    sub.add_parser("devices", help="Konfiguration anzeigen").set_defaults(fn=cmd_devices)

    p = sub.add_parser("merge", help="Mess-CSVs anderer Rechner ubernehmen")
    p.add_argument("orte", nargs="+", help="Ordner oder CSV-Dateien (auch von einem anderen Rechner)")
    p.set_defaults(fn=cmd_merge)

    p = sub.add_parser("export", help="dump the measurement series as CSV")
    p.add_argument("--month", help="nur ein Monat, z. B. 2026-11")
    p.add_argument("--since", "--seit", dest="seit",
                   help="only rows from 'YYYY-MM-DD [HH:MM]'")
    p.add_argument("--until", "--bis", dest="bis",
                   help="only rows up to 'YYYY-MM-DD [HH:MM]'")
    p.add_argument("--out", help="write to a file instead of stdout")
    p.set_defaults(fn=cmd_export)

    p = sub.add_parser("sync", help="pull measurement rows from other hosts over SSH")
    p.add_argument("--host", "--rechner", dest="rechner", action="append",
                   help="only this host (repeatable)")
    p.add_argument("--month", help="only one month, e.g. 2026-11")
    p.add_argument("--command", "--befehl", dest="befehl",
                   help="custom command instead of ssh (CSV on stdout)")
    p.add_argument("--list", "--liste", dest="liste", action="store_true",
                   help="show configured hosts")
    p.set_defaults(fn=cmd_sync)

    p = sub.add_parser("mute", help="switch sources or devices off/on without editing files")
    p.add_argument("--source", action="append", help="source key, e.g. cpu, nvidia, rest")
    p.add_argument("--device", action="append", help="device id")
    p.add_argument("--on", action="store_true", help="switch back on instead of off")
    p.add_argument("--show", action="store_true", help="show what is currently off")
    p.set_defaults(fn=cmd_mute)

    p = sub.add_parser("widget", help="Linux-Anzeige (GTK4) lesen neben dem Sammler her")
    p.add_argument("--autostart", action="store_true", help="Anzeige bei Anmeldung starten")
    p.add_argument("--no-autostart", action="store_true", help="Autostart der Anzeige entfernen")
    p.add_argument("--service", action="store_true", help="Anzeige als systemd-Userdienst ( Restart, immer sichtbar )")
    p.add_argument("--no-service", action="store_true", help="Anzeige-Dienst entfernen")
    p.add_argument("--detail", metavar="GERAET", help="direkt die Detailseite dieses Geraets oeffnen")
    p.add_argument("--bereich", choices=("h", "tag", "woche"), default="h",
                   help="Zeitfenster der Kurve beim Start (h=Stunde, tag=24 h, woche=7 Tage)")
    p.set_defaults(fn=cmd_widget)

    p = sub.add_parser("autostart", help="start the widget at login")
    p.add_argument("--enable", action="store_true")
    p.add_argument("--disable", action="store_true")
    p.add_argument("--headless", action="store_true", help="ohne Fenster starten")
    p.add_argument("--status", action="store_true")
    p.set_defaults(fn=cmd_autostart)

    p = sub.add_parser("service", help="systemd-User-Dienst (Linux)")
    p.add_argument("aktion", choices=["install", "remove"])
    p.set_defaults(fn=cmd_service)
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    if hasattr(signal, "SIGPIPE"):            # `powermeter run | head` darf nicht krachen
        try:
            signal.signal(signal.SIGPIPE, signal.SIG_DFL)
        except (ValueError, OSError):
            pass
    for stream in (sys.stdout, sys.stderr):    # deutsche Umlaute, € und ≈ auch in Windows-Konsolen
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
    paths = resolve_paths(args.data)
    set_data_dir(paths.data)
    paths.ensure()
    core = Core(paths, geraete=getattr(args, "devices", None))
    try:
        return args.fn(core, args)
    finally:
        if args.cmd == "run":
            core.close()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
