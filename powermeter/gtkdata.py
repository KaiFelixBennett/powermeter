"""Datenbeschaffung fuer die Linux-Anzeige (GTK-frei, deshalb testbar).

Die Anzeige ist bewusst ein **lesender** Zweitprozess: der Sammler
(`powermeter run --headless`) schreibt Monats-CSVs und `state.json`, die Anzeige
liest beides und fasst es zusammen. Sie misst selbst nichts und schreibt nichts
in die Messreihe – sonst gaebe es zwei Logger auf einer Datenbank.

Was sie zeigt, ist gemittelt, wo keine Momentaufnahme existiert: das CSV traegt
eine Zeile pro Geraet und Quelle pro Minute. „W aktuell“ heisst dort „Watt der
letzten Minute“, und das Alter der letzten Zeile wird mitgegeben statt versteckt.
"""
from __future__ import annotations

import csv
import json
import time
from pathlib import Path

from .store import HEAD

MONAT = ".csv"
VERLAUF_MINUTEN = 90  # Kurve: gut eine Stunde, damit man Spitzen und Tal sieht
VERALTET_S = 900  # ab 15 min ohne Zeile ist die Anzeige eine Luege, wenn sie das nicht sagt


def _json(dic: Path, standard: dict) -> dict:
    try:
        wert = json.loads(dic.read_text(encoding="utf-8"))
        return wert if isinstance(wert, dict) else standard
    except (OSError, ValueError):
        return standard


def gerate_namen(cfgdir: Path) -> dict[str, str]:
    """Id -> Anzeigename aus devices.json; fehlende Namen bleiben die Id."""
    daten = _json(cfgdir / "devices.json", {})
    out: dict[str, str] = {}
    for ein in daten.get("geraete", []):
        if isinstance(ein, dict) and ein.get("id"):
            out[str(ein["id"])] = str(ein.get("name") or ein["id"])
    return out


def quellennamen(cfgdir: Path) -> dict[str, dict[str, str]]:
    """Geraet -> Quelle -> Anzeigename. Ohne das steht `http` fuer die GPU.

    Das CSV traegt nur den Quelltyp; die devices.json hat die Bezeichnung. Ein
    Geraet mit genau einer Quelle dieses Typs bekommt ihren Namen, sonst bleibt
    der Typ stehen – raten ist schlechter als beschriften.
    """
    daten = _json(cfgdir / "devices.json", {})
    out: dict[str, dict[str, str]] = {}
    for ein in daten.get("geraete", []):
        if not isinstance(ein, dict) or not ein.get("id"):
            continue
        namen: dict[str, str] = {}
        for quelle in ein.get("quellen", []):
            if not isinstance(quelle, dict) or not quelle.get("typ"):
                continue
            typ = str(quelle["typ"])
            bezeichnet = str(quelle.get("bezeichnung") or "")
            if typ in namen:
                namen[typ] = ""           # mehrdeutig: Typ reicht als Label
            elif bezeichnet:
                namen[typ] = bezeichnet
        out[str(ein["id"])] = {t: n for t, n in namen.items() if n}
    return out


def _preis_ct(cfgdir: Path) -> float:
    cfg = _json(cfgdir / "config.json", {})
    try:
        return float(cfg.get("strompreis_kwh_ct", 0.0))
    except (TypeError, ValueError):
        return 0.0


def _fehler(datadir: Path, jetzt: float) -> list[str]:
    """Fehler des Sammlers zeigen statt verschlucken (error.log, frisch)."""
    pfad = datadir / "error.log"
    try:
        if jetzt - pfad.stat().st_mtime > 86400:
            return []
        zeilen = [z.strip() for z in pfad.read_text(encoding="utf-8", errors="replace").splitlines()]
        letzte = [z for z in zeilen if z][-1:]
        return letzte
    except (OSError, IndexError):
        return []


def lese_anzeige(datadir: Path, cfgdir: Path | None = None, jetzt: float | None = None) -> dict:
    """Zusammenfassung der Messreihe fuer die Anzeige.

    Nie wirfend: fehlt alles, kommen Nullen plus Hinweis in `fehler`.
    """
    jetzt = jetzt if jetzt is not None else time.time()
    datadir = Path(datadir)
    cfgdir = Path(cfgdir) if cfgdir else datadir
    namen = gerate_namen(cfgdir)
    quellnamen = quellennamen(cfgdir)
    ct_kwh = _preis_ct(cfgdir)
    heute = time.strftime("%Y-%m-%d", time.localtime(jetzt))

    state = _json(datadir / "state.json", {})
    stumm = {str(g) for g in (state.get("aus", {}) or {}).get("geraet", [])}

    gerate: dict[str, dict] = {}
    minutensumme: dict[str, float] = {}   # zeit -> Watt gesamt (Kurve)
    letzte_zeit = ""
    wh_gesamt = 0.0
    fehler: list[str] = []
    for pfad in sorted(datadir.glob(f"20*-*{MONAT}")):
        try:
            with pfad.open(newline="", encoding="utf-8") as f:
                leser = csv.reader(f)
                kopf = next(leser, [])
                if kopf[:len(HEAD)] != HEAD:
                    fehler.append(f"{pfad.name}: unbekanntes Format")
                    continue
                for rec in leser:
                    if len(rec) < len(HEAD):
                        continue
                    zeit, geraet, quelle, sek, watt = rec[0], rec[1], rec[2], rec[3], rec[4]
                    try:
                        wh = float(watt) * float(sek) / 3600.0
                    except ValueError:
                        continue
                    try:
                        wert = float(watt)
                    except ValueError:
                        continue
                    minutensumme[zeit] = minutensumme.get(zeit, 0.0) + wert
                    if zeit > letzte_zeit:
                        letzte_zeit = zeit
                    g = gerate.setdefault(geraet, {
                        "id": geraet, "name": namen.get(geraet, geraet), "wh_heute": 0.0,
                        "wh_gesamt": 0.0, "quellen": {}, "minute": "", "w_aktuell": 0.0,
                        "minuten": [], "gemutet": geraet in stumm})
                    g["wh_gesamt"] += wh
                    wh_gesamt += wh
                    if zeit[:10] == heute:
                        g["wh_heute"] += wh
                    # Watt der neuesten Minute dieses Geraets (Quellen aufaddieren)
                    if zeit >= g["minute"]:
                        if zeit > g["minute"]:
                            g["minute"], g["quellen"] = zeit, {}
                        _label = quellnamen.get(geraet, {}).get(quelle, quelle)
                        g["quellen"][_label] = g["quellen"].get(_label, 0.0) + float(watt)
                        g["minuten"] = (g["minuten"] + [zeit])[-10:]
                    g["zuletzt"] = zeit
        except OSError as e:
            fehler.append(f"{pfad.name}: {e}")

    # „Jetzt“ gilt pro Minute, nicht pro Geraet: wer in der neuesten Minute keine
    # Zeile hat, tragt nichts bei – sein letzter Wert ist keine Momentaufnahme.
    aktuell_dabei = [g for g in gerate.values() if g.get("minute") == letzte_zeit]
    for g in gerate.values():
        g["aktuell"] = g in aktuell_dabei
        if not g["aktuell"]:
            g["quellen"] = {}

    # Kurve der letzten Minuten + Tagesrandwerte: Zahlen ohne Verlauf erzaehlen nichts
    punkte = sorted(minutensumme.items())
    # Trend gegen vor fuenf Minuten: eine Zahl ohne Richtung ist die halbe Antwort
    rueck = punkte[-6:-5] or punkte[:1]
    trend = round(punkte[-1][1] - rueck[0][1], 1) if punkte else 0.0
    heutige = [(z, w) for z, w in punkte if z[:10] == heute]
    verlauf = [round(w, 1) for _, w in punkte[-VERLAUF_MINUTEN:]]
    tageswerte = {
        "spitze": round(max((w for _, w in heutige), default=0.0), 1),
        "tief": round(min((w for _, w in heutige), default=0.0), 1),
        "mittel": round(sum(w for _, w in heutige) / len(heutige), 1) if heutige else 0.0,
        "minuten": len(heutige),
    }
    anteile: dict[str, float] = {}
    for g in aktuell_dabei:
        for quell, watt in g["quellen"].items():
            anteile[quell] = anteile.get(quell, 0.0) + watt

    # Schnitt der letzten Minuten: Einzelminute ist bei Lastspitzen kein Trend
    for g in gerate.values():
        g["w_aktuell"] = round(sum(g["quellen"].values()), 1)
        for quell, watt in sorted(g["quellen"].items(), key=lambda kv: -kv[1]):
            if watt >= 1.0:
                g.setdefault("quellen_liste", []).append((quell, round(watt, 1)))
        g["minuten_alt"] = None
        if g.get("zuletzt") and letzte_zeit:
            try:
                g["minuten_alt"] = round((time.mktime(time.strptime(letzte_zeit, "%Y-%m-%d %H:%M"))
                                          - time.mktime(time.strptime(g["zuletzt"], "%Y-%m-%d %H:%M"))) / 60.0)
            except ValueError:
                pass
        g["wh_heute"] = round(g["wh_heute"], 3)
        g["wh_gesamt"] = round(g["wh_gesamt"], 3)
        g["quellen"] = {k: round(v, 1) for k, v in g["quellen"].items()}

    liste = sorted(gerate.values(), key=lambda g: (-g["aktuell"], -g["wh_gesamt"]))
    w_aktuell = round(sum(g["w_aktuell"] for g in aktuell_dabei), 1)
    wh_heute = round(sum(g["wh_heute"] for g in liste), 3)

    alt_s = None
    if letzte_zeit:
        try:
            struct = time.strptime(letzte_zeit, "%Y-%m-%d %H:%M")
            alt_s = round(jetzt - time.mktime(struct), 1)
        except ValueError:
            pass

    if not liste:
        fehler.append("keine Messzeilen in diesem Ordner")
    if alt_s is not None and alt_s > VERALTET_S:
        fehler.append(f"letzte Messung vor {alt_s / 60:.0f} min - Sammler laeuft nicht?")

    return {
        "geraete": liste,
        "gesamt": {
            "w_aktuell": w_aktuell,
            "wh_heute": wh_heute,
            "wh_gesamt": round(wh_gesamt, 3),
            "ct_heute": round(wh_heute / 1000.0 * ct_kwh, 2),
            "ct_gesamt": round(wh_gesamt / 1000.0 * ct_kwh, 2),
        },
        "trend": trend,
        "verlauf": verlauf,
        "tag": tageswerte,
        "anteile": sorted(((k, round(v, 1)) for k, v in anteile.items() if v >= 1.0),
                          key=lambda kv: -kv[1]),
        "seit": state.get("seit", "") or (liste[0]["id"] if liste else ""),
        "zeit_letzte": letzte_zeit,
        "alt_s": alt_s,
        "veraltet": bool(alt_s is not None and alt_s > VERALTET_S),
        "ct_kwh": ct_kwh,
        "fehler": fehler,
    }
