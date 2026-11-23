"""Anzeige-Daten (gtkdata): Kurve, Tageswerte, Anteile, Alter, mehrdeutige Quellen.

Die Anzeige ist ein lesender Zweitprozess – was sie zeigt, muss aus den CSVs der
Sammler kommen, ohne zu raten. Diese Tests laufen ohne Display.
"""
import json
import time

from powermeter.gtkdata import lese_anzeige, quellennamen

MONAT = ("zeit,geraet,quelle,sekunden,watt\n"
         "2026-11-23 07:00,cachyos-ai,cpu,60,100\n"
         "2026-11-23 07:00,cachyos-ai,http,60,300\n"
         "2026-11-23 07:00,pc,cpu,60,50\n"
         "2026-11-23 07:01,cachyos-ai,cpu,60,120\n"
         "2026-11-23 07:01,cachyos-ai,http,60,280\n"
         "2026-11-23 07:01,pc,cpu,60,60\n"
         "2026-11-23 07:02,cachyos-ai,cpu,60,80\n"
         "2026-11-23 07:02,cachyos-ai,http,60,120\n")

DEVICES = {"geraete": [
    {"id": "cachyos-ai", "name": "CachyOS-Hub", "quellen": [
        {"typ": "cpu"}, {"typ": "http", "bezeichnung": "GPU Radeon (tokdeck)"}]},
    {"id": "pc", "name": "Kais GamingDesk", "quellen": [{"typ": "cpu"}, {"typ": "cpu"}]},
]}

# 07:02 ist die letzte Zeile; jetzt = 07:04 -> 120 s alt
JETZT = time.mktime(time.strptime("2026-11-23 07:04", "%Y-%m-%d %H:%M"))


def _umgebung(tmp_path, monat=MONAT, devices=DEVICES):
    daten = tmp_path / "daten"
    daten.mkdir()
    (daten / "2026-11.csv").write_text(monat, encoding="utf-8")
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "devices.json").write_text(json.dumps(devices), encoding="utf-8")
    (cfg / "config.json").write_text(json.dumps({"strompreis_kwh_ct": 30.0}), encoding="utf-8")
    return daten, cfg


def test_geraet_ohne_zeile_in_der_neuesten_minute_zahlt_nicht_zu_jetzt(tmp_path):
    daten, cfg = _umgebung(tmp_path)
    a = lese_anzeige(daten, cfg, jetzt=JETZT)
    pc = [g for g in a["geraete"] if g["id"] == "pc"][0]
    cach = [g for g in a["geraete"] if g["id"] == "cachyos-ai"][0]
    assert cach["aktuell"] is True and pc["aktuell"] is False
    assert pc["w_aktuell"] == 0.0 and pc["minuten_alt"] == 1.0
    assert a["gesamt"]["w_aktuell"] == 200.0         # nicht 200 + 60 aus einer aelteren Minute
    assert dict(a["anteile"])["cpu"] == 80.0         # pc's cpu von 07:01 nicht mitgezählt


def test_anzeige_summiert_letzte_minute_und_kurve(tmp_path):
    daten, cfg = _umgebung(tmp_path)
    a = lese_anzeige(daten, cfg, jetzt=JETZT)
    # letzte Minute 07:02: 80 + 120 (pc hat dort keine Zeile)
    assert a["gesamt"]["w_aktuell"] == 200.0
    assert a["verlauf"] == [450.0, 460.0, 200.0]
    assert a["tag"] == {"spitze": 460.0, "tief": 200.0, "mittel": 370.0, "minuten": 3}
    assert a["zeit_letzte"] == "2026-11-23 07:02"
    assert a["alt_s"] == 120.0
    assert a["veraltet"] is False


def test_anteile_kennen_die_Bezeichnung_aus_devices_json(tmp_path):
    daten, cfg = _umgebung(tmp_path)
    a = lese_anzeige(daten, cfg, jetzt=JETZT)
    namen = [name for name, _ in a["anteile"]]
    assert "GPU Radeon (tokdeck)" in namen          # http ist nicht „http“
    assert "cpu" in namen
    watt = dict(a["anteile"])
    assert watt["cpu"] == 80.0 and watt["GPU Radeon (tokdeck)"] == 120.0


def test_mehrdeutige_Quelle_bleibt_bei_ihrem_Typ(tmp_path):
    daten, cfg = _umgebung(tmp_path)
    namen = quellennamen(cfg)
    # pc hat zwei Quellen desselben Typs ohne Bezeichnung -> nichts geraten
    assert namen["cachyos-ai"] == {"http": "GPU Radeon (tokdeck)"}
    assert "cpu" not in namen["pc"]


def test_kosten_und_Summen_ueber_alle_Tage(tmp_path):
    daten, cfg = _umgebung(tmp_path)
    a = lese_anzeige(daten, cfg, jetzt=JETZT)
    ges = a["gesamt"]
    # 60-Sekunden-Zeilen: Wh = W * s / 3600. Summe der Watt ist 1110, das sind
    # 1110 W-Minuten = 18,5 Wh, bei 30 ct/kWh also 0,555 ct.
    assert abs(ges["wh_gesamt"] - 18.5) < 0.01
    assert abs(ges["wh_heute"] - 18.5) < 0.01
    assert abs(ges["ct_heute"] - 0.555) < 0.01


def test_veraltete_Messreihe_sagt_es_klar(tmp_path):
    daten, cfg = _umgebung(tmp_path)
    spaeter = JETZT + 7200  # zwei Stunden spaeter befragt
    a = lese_anzeige(daten, cfg, jetzt=spaeter)
    assert a["veraltet"] is True
    assert any("Sammler" in f for f in a["fehler"])
    assert a["alt_s"] == 7320.0


def test_leerer_Ordner_ist_kein_Crash(tmp_path):
    daten, cfg = _umgebung(tmp_path, monat="zeit,geraet,quelle,sekunden,watt\n")
    a = lese_anzeige(daten, cfg, jetzt=JETZT)
    assert a["geraete"] == [] and a["verlauf"] == []
    assert a["gesamt"]["w_aktuell"] == 0.0
    assert any("keine Messzeilen" in f for f in a["fehler"])


def test_fremdes_CSV_Format_wird_sichtbar(tmp_path):
    daten, cfg = _umgebung(tmp_path, monat="zeit,sekunden,cpu_w\n2026-11-23 07:00,60,80\n")
    a = lese_anzeige(daten, cfg, jetzt=JETZT)
    assert any("unbekanntes Format" in f for f in a["fehler"])
