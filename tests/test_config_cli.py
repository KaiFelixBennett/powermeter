"""Konfiguration (inkl. Migration aus dem alten Einzeldatei-Tool) und Kommandozeile."""
import json

import pytest

from powermeter.cli import main
from powermeter.config import load_config, load_devices, resolve_paths
from powermeter.store import HEAD, merge_data


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setenv("POWERMETER_DATA", str(tmp_path / "daten"))
    p = resolve_paths(str(tmp_path / "daten"))
    p.config.parent.mkdir(parents=True, exist_ok=True)
    return p


def test_config_wird_beim_ersten_start_angelegt_und_klemmt_werte(paths):
    cfg = load_config(paths)
    assert paths.config.exists() and cfg.intervall_sekunden == 2.0
    paths.config.write_text(json.dumps({"intervall_sekunden": 999, "strompreis_kwh_ct": -5}),
                            encoding="utf-8")
    cfg = load_config(paths)
    assert cfg.intervall_sekunden == 60      # nach oben geklemmt
    assert cfg.strompreis_kwh_ct == 0.0      # nach unten geklemmt


def test_migration_alte_konfiguration_wird_ein_geraet(paths):
    paths.config.write_text(json.dumps({"rest_watt": 55, "netzteil_wirkungsgrad": 0.85,
                                        "strompreis_kwh_ct": 27.48, "tarif_name": "Sondertarif"}),
                            encoding="utf-8")
    df = load_devices(paths)
    assert df.ids() == ["pc"]
    dev = df.by_id("pc")
    assert dev.wirkungsgrad == 0.85
    fest = [q for q in dev.quellen if q.typ == "fest"]
    assert len(fest) == 1 and fest[0].value["wert_watt"] == 55
    assert paths.devices.exists()                      # devices.json neu geschrieben
    cfg = load_config(paths)
    assert cfg.strompreis_kwh_ct == 27.48 and cfg.tarif_name == "Sondertarif"


def test_geraete_id_bereinigt_und_werte_geklemmt(paths):
    (paths.config.parent / "devices.json").write_text(json.dumps({"geraete": [
        {"id": "PC", "name": "Gross", "wirkungsgrad": 3, "quellen": [{"typ": "fest", "wert_watt": 1}]},
        {"id": "PC", "name": "Duplikat", "quellen": [{"typ": "fest", "wert_watt": 2}]},
        {"id": "kaputt*", "name": "Sonderzeichen", "quellen": [{"typ": "fest", "wert_watt": 3}]},
        {"id": "aus", "aktiv": False, "host": "anderer", "quellen": [{"typ": "fest", "wert_watt": 4}]},
    ]}), encoding="utf-8")
    df = load_devices(paths)
    assert df.ids() == ["pc", "kaputt", "aus"]                # Duplikat verworfen, id bereinigt
    assert df.by_id("pc").wirkungsgrad == 1.0                 # Obergrenze
    assert df.by_id("aus").owns_on("anderer") is False        # deaktiviert
    assert df.by_id("kaputt").owns_on("host1") is True        # ohne host: auf jedem Rechner


def test_cli_run_report_und_merge(tmp_path, capsys):
    daten = tmp_path / "daten"
    daten.mkdir()
    (daten / "devices.json").write_text(json.dumps({"geraete": [
        {"id": "pc", "name": "Dieser PC", "wirkungsgrad": 0.9,
         "quellen": [{"typ": "fest", "wert_watt": 100}]},
        {"id": "andre", "name": "Fremder Rechner", "host": "andrekiste",
         "quellen": [{"typ": "fest", "wert_watt": 60}]},
    ]}), encoding="utf-8")
    args = ["--data", str(daten)]

    assert main(args + ["run", "--headless", "--duration", "3", "--devices", "pc"]) == 0
    csvs = list(daten.glob("????-??.csv"))
    assert len(csvs) == 1
    zeilen = csvs[0].read_text(encoding="utf-8").strip().splitlines()
    assert zeilen[0] == ",".join(HEAD)
    assert any(",pc,rest," in z for z in zeilen)
    assert not any(",andre," in z for z in zeilen)   # host-gebundenes Geraet misst nicht hier

    main(args + ["report"])
    assert "energy" in capsys.readouterr().out

    main(args + ["report", "--json"])
    text = capsys.readouterr().out
    zahl = json.loads(text[text.index("{"):text.rindex("}") + 1])
    assert zahl["wh_gesamt"] > 0 and list(zahl["je_geraet"]) == ["pc"]

    assert main(args + ["devices"]) == 0
    assert "Fremder Rechner" in capsys.readouterr().out

    zweit = tmp_path / "messplatz2"
    zweit.mkdir()
    (zweit / csvs[0].name).write_text(
        csvs[0].read_text(encoding="utf-8")
        + "2026-11-20 10:00,andre,dose,60.0,60.0\n2026-11-20 10:01,andre,dose,60.0,61.0\n",
        encoding="utf-8")
    ziel = resolve_paths(str(daten))
    neu, dupe = merge_data(ziel, [zweit], "pc")
    assert neu == 2 and dupe >= 1                      # neu uebernommen, Altes erkannt
    neu2, _ = merge_data(ziel, [zweit], "pc")          # zweiter Aufruf ist folgenlos
    assert neu2 == 0
    text = (daten / csvs[0].name).read_text(encoding="utf-8")
    assert text.count(",andre,") == 2
    main(args + ["report", "--device", "andre"])
    assert "Fremder Rechner" in capsys.readouterr().out


def test_sync_holt_zeilen_ueber_eigenes_kommando(tmp_path, capsys, monkeypatch):
    """Der ssh-Anteil ist Standardwerkzeug; hier läuft derselbe Weg mit lokalem Export."""
    import sys

    quelle = tmp_path / "quelle"
    quelle.mkdir()
    (quelle / "2026-11.csv").write_text(
        "zeit,geraet,quelle,sekunden,watt\n"
        "2026-11-20 10:00,linuxbox,rest,60.0,40.0\n"
        "2026-11-20 10:01,linuxbox,rest,60.0,41.0\n", encoding="utf-8")
    daten = tmp_path / "daten"
    daten.mkdir()
    befehl = " ".join(f'"{z}"' if " " in z else z
                      for z in [sys.executable, "-m", "powermeter", "--data", str(quelle), "export"])

    assert main(["--data", str(daten), "sync", "--befehl", befehl]) == 0
    aus = capsys.readouterr().out
    assert "2 new rows" in aus
    assert (daten / "2026-11.csv").exists()
    assert (daten / "2026-11.csv").read_text(encoding="utf-8").count("linuxbox") == 2

    # zweiter Anruf: nichts Neues, der Merker steht im state.json
    assert main(["--data", str(daten), "sync", "--befehl", befehl]) == 0
    assert "0 new rows" in capsys.readouterr().out
    assert "2026-11-20 10:01" in (daten / "state.json").read_text(encoding="utf-8")


def test_export_seit_filtert(tmp_path):
    daten = tmp_path / "daten"
    daten.mkdir()
    (daten / "2026-11.csv").write_text(
        "zeit,geraet,quelle,sekunden,watt\n"
        "2026-11-20 09:59,pc,rest,60.0,30.0\n"
        "2026-11-20 10:00,pc,rest,60.0,31.0\n", encoding="utf-8")
    main(["--data", str(daten), "export", "--seit", "2026-11-20 10:00"])
    aus = capsys_readout = None


def test_sync_warnt_vor_gleichen_geraete_ids(tmp_path, capsys):
    import sys

    quelle = tmp_path / "quelle"
    quelle.mkdir()
    (quelle / "2026-11.csv").write_text(
        "zeit,geraet,quelle,sekunden,watt\n"
        "2026-11-20 11:00,pc,rest,60.0,55.0\n", encoding="utf-8")
    daten = tmp_path / "daten"
    daten.mkdir()
    teile = [sys.executable, "-m", "powermeter", "--data", str(quelle), "export"]
    befehl = " ".join(f'"{z}"' if " " in z else z for z in teile)
    main(["--data", str(daten), "sync", "--befehl", befehl])
    assert "WARNING" in capsys.readouterr().out


def test_englische_geraete_und_quellen_in_devices_json(tmp_path):
    """Englische Schluessel sind die Referenz: devices/sources statt geraete/quellen."""
    paths = resolve_paths(str(tmp_path / "daten"))
    paths.data.mkdir(parents=True, exist_ok=True)
    paths.config.parent.mkdir(parents=True, exist_ok=True)
    paths.config.write_text(json.dumps(
        {"interval_seconds": 20, "price_ct_kwh": 33.5, "tariff_name": "Example Utility"}),
        encoding="utf-8")
    paths.devices.write_text(json.dumps({"devices": [
        {"id": "desktop", "name": "Desktop PC", "efficiency": 0.88,
         "sources": [{"type": "fixed", "watts": 40.0, "label": "Smart plug"}]},
        {"id": "console", "name": "Console", "price_ct_kwh": 41.0, "enabled": False,
         "sources": [{"type": "fixed", "watts": 99.0}]}]}), encoding="utf-8")
    cfg = load_config(paths)
    assert cfg.strompreis_kwh_ct == 33.5 and cfg.intervall_sekunden == 20.0
    assert cfg.tarif_name == "Example Utility"
    dv = load_devices(paths)
    assert [d.id for d in dv.devices] == ["desktop", "console"]
    assert dv.by_id("desktop").wirkungsgrad == 0.88
    assert dv.by_id("console").aktiv is False and dv.by_id("console").preis_ct_kwh == 41.0
    assert dv.by_id("desktop").quellen[0].bezeichnung == "Smart plug"
    assert dv.by_id("desktop").quellen[0].value["wert_watt"] == 40.0
