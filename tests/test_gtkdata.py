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


def test_Momentaufnahme_ueber_die_letzten_fuenf_Minuten(tmp_path):
    """Wer in den letzten fuenf Minuten gemessen hat, zaehlt – der Rest nicht.

    Der Windows-Rechner liefert in Schwueben (Plan + Import). Ihn bei jeder
    Importlücke auf 0 W fallen zu lassen, waere erfundene Sparsamkeit.
    """
    daten, cfg = _umgebung(tmp_path)
    a = lese_anzeige(daten, cfg, jetzt=JETZT)
    pc = [g for g in a["geraete"] if g["id"] == "pc"][0]
    cach = [g for g in a["geraete"] if g["id"] == "cachyos-ai"][0]
    assert pc["minuten_alt"] == 1.0 and pc["aktuell"] is True
    assert pc["w_aktuell"] == 60.0 and cach["w_aktuell"] == 200.0
    assert a["gesamt"]["w_aktuell"] == 260.0         # 200 + 60, pc von 07:01
    assert dict(a["anteile"])["cpu"] == 140.0


def test_Geraet_mit_Zwölf_Minuten_Stille_zaehlt_nicht_mehr(tmp_path):
    daten, cfg = _umgebung(tmp_path)
    (daten / "2026-11.csv").write_text(
        MONAT.replace("2026-11-23 07:01,pc,cpu,60,60\n", "").replace(
            "2026-11-23 07:00,pc,cpu,60,50\n", "2026-11-23 06:49,pc,cpu,60,50\n"),
        encoding="utf-8")
    a = lese_anzeige(daten, cfg, jetzt=JETZT)
    pc = [g for g in a["geraete"] if g["id"] == "pc"][0]
    assert pc["minuten_alt"] == 13.0 and pc["aktuell"] is False
    assert a["gesamt"]["w_aktuell"] == 200.0
    assert dict(a["anteile"]).get("cpu", 0.0) == 80.0     # nur cachyos' cpu


def test_Kosten_Tageswerte_und_Kurve_der_Summe(tmp_path):
    daten, cfg = _umgebung(tmp_path)
    a = lese_anzeige(daten, cfg, jetzt=JETZT)
    # 07:02: cachyos 80+120, pc von 07:01 wird bis 07:02 weitergezählt (+60)
    assert a["gesamt"]["w_aktuell"] == 260.0
    assert a["verlauf"] == [450.0, 460.0, 260.0]   # pc wird ueber die Lücke weitergezählt
    # tag rechnet auf der ueberbrueckten Summe: 07:02 ist 200 + 60 (pc weitergezaehlt)
    assert a["tag"] == {"spitze": 460.0, "tief": 260.0, "mittel": 390.0, "minuten": 3,
                        "tage": [("2026-11-23", 18.5)], "wh_tag": 18.5}
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
    # pc wurde 07:01 gemessen und zaehlt bis 07:02 weiter: cpu = 80 + 60
    assert watt["cpu"] == 140.0 and watt["GPU Radeon (tokdeck)"] == 120.0


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


def test_bereiche_und_Geraetekurven(tmp_path):
    daten, cfg = _umgebung(tmp_path)
    n = lese_anzeige(daten, cfg, jetzt=JETZT, bereich="h")
    t = lese_anzeige(daten, cfg, jetzt=JETZT, bereich="woche")
    assert n["bereich_text"] == "letzte Stunde" and t["bereich_text"] == "letzte 7 Tage"
    pc = [g for g in n["geraete"] if g["id"] == "pc"][0]
    assert pc["verlauf"] == [50.0, 60.0]                     # eigene Kurve bleibt roh
    # 60-Minuten-Kisten verschmelzen beide Minuten zu einem Mittelwert
    assert [g for g in t["geraete"] if g["id"] == "pc"][0]["verlauf"] == [55.0]
    assert pc["tage"] == [("2026-11-23", 1.83)]              # (50+60)*60/3600 Wh
    # unbekannte Bereichsangabe faellt zur Standardstunde zurueck
    assert lese_anzeige(daten, cfg, jetzt=JETZT, bereich="quatsch")["bereich"] == "h"


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


def test_Bereichsschalter_keine_Rueckkopplung():
    """Der Schalter, der gerade läuft, ändert nichts – sonst schleift er sich selbst.

    Früher hingen die Bereichsknöpfe an "toggled": jedes programmatische set_active
    löste den nächsten Handler aus, die Knöpfe setzten sich gegenseitig zurück, bis der
    Stack überlief (GNOME meldete "powermeter antwortet nicht").
    """
    from powermeter.gtkdata import bereich_schalte

    namen = ("h", "tag", "woche")
    assert bereich_schalte(namen, "woche", gesperrt=False) == {"h": False, "tag": False,
                                                              "woche": True}
    assert bereich_schalte(namen, "woche", gesperrt=True) is None   # Rückkopplung: nix tun
    assert bereich_schalte(namen, "unbekannt", gesperrt=False) is None


def test_Bereichsumschaltung_im_Fenster_liest_genau_einmal():
    """Gleiches noch einmal am Fenster-Handler – braucht GTK4 (nur lokal lauffaehig)."""
    try:
        from powermeter.gtkwidget import Fenster
    except (ImportError, Exception) as e:          # ohne PyGObject/cairo: sichtbar skippen
        import pytest
        pytest.skip(f"GTK-Bindung nicht verfuegbar: {e}")

    class Schalter:
        def __init__(self):
            self.aktiv = False

        def set_active(self, wert):
            self.aktiv = bool(wert)

    class Fake:
        bereich = "h"
        _bereich_sperre = False
        lesen = 0
        sturm = 0

        def __init__(self):
            self.bereich_knoepfe = {w: Schalter() for w in ("h", "tag", "woche")}

        def aktualisiere(self, erzwingen=False):
            self.lesen += 1
            self.sturm += 1
            if self.sturm < 40:     # Signal-Sturm: Handler fällt sofort wieder herein
                Fenster.bereich_klick(self, self.bereich_knoepfe["h"], "woche")

    fake = Fake()
    # der Fake bekommt beide echten Methoden, damit der Klickweg genau wie im
    # Fenster abläuft (Klick -> setzen -> Nachziehen)
    fake.bereich_setzen = lambda wert: Fenster.bereich_setzen(fake, wert)
    Fenster.bereich_klick(fake, fake.bereich_knoepfe["woche"], "woche")
    assert fake.bereich == "woche" and fake.lesen == 1
    assert [k for k, s in fake.bereich_knoepfe.items() if s.aktiv] == ["woche"]
