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
# Messbereiche: (Anzeigetitel, Minuten pro Kasten, Anzahl Kästen)
BEREICHE = {"h": ("letzte Stunde", 1, 75), "tag": ("letzte 24 h", 10, 144),
            "woche": ("letzte 7 Tage", 60, 168)}
AUSFUELL_MIN = 10   # Lücke bis 10 min überbrücken: Import in Schwüben ist kein Aus-Sprung
AKTUELL_MIN = 5     # wer in den letzten 5 min gemessen hat, zählt zur Momentaufnahme
SPAET_S = 150   # eigenste Minute älter als die neueste: verspätet, nicht „kein Messwert"
VERALTET_S = 900  # ab 15 min ohne Zeile ist die Anzeige eine Luege, wenn sie das nicht sagt


def _zeitmarke(minute: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(minute * 60))


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


def bereich_schalte(namen, gewaehlt: str, gesperrt: bool) -> dict[str, bool] | None:
    """Which range switch should be on — and none of the feedback.

    Range buttons were wired to "toggled", so every programmatic ``set_active``
    re-entered the handler, the buttons kept re-activating one another and the stack
    overflowed (the desktop reported it as "powermeter is not responding"). A switch
    in progress returns None: do nothing at all, change nothing.
    """
    if gesperrt or gewaehlt not in namen:
        return None
    return {name: (name == gewaehlt) for name in namen}


def lese_anzeige(datadir: Path, cfgdir: Path | None = None, jetzt: float | None = None,
                 bereich: str = "h") -> dict:
    """Zusammenfassung der Messreihe fuer die Anzeige.

    Nie wirfend: fehlt alles, kommen Nullen plus Hinweis in `fehler`.
    """
    jetzt = jetzt if jetzt is not None else time.time()
    datadir = Path(datadir)
    cfgdir = Path(cfgdir) if cfgdir else datadir
    namen = gerate_namen(cfgdir)
    quellnamen = quellennamen(cfgdir)
    ct_kwh = _preis_ct(cfgdir)
    bereich = bereich if bereich in BEREICHE else "h"
    heute = time.strftime("%Y-%m-%d", time.localtime(jetzt))

    state = _json(datadir / "state.json", {})
    stumm = {str(g) for g in (state.get("aus", {}) or {}).get("geraet", [])}

    gerate: dict[str, dict] = {}
    minutensumme: dict[str, float] = {}   # zeit -> Watt gesamt (Kurve)
    minuten_pro_geraet: dict[str, dict[str, float]] = {}  # geraet -> zeit -> Watt
    wh_pro_tag: dict[str, dict[str, float]] = {}          # geraet -> JJJJ-MM-TT -> Wh
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
                    pro_geraet = minuten_pro_geraet.setdefault(geraet, {})
                    pro_geraet[zeit] = pro_geraet.get(zeit, 0.0) + wert
                    pro_tag = wh_pro_tag.setdefault(geraet, {})
                    pro_tag[zeit[:10]] = pro_tag.get(zeit[:10], 0.0) + wh
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
    # Zeile hat, tragt nichts zur Momentaufnahme bei. Aber „verspaetet“ (Import alle
    # paar Minuten) ist etwas anderes als „kein Messwert“ – das ist zu zeigen.
    def minuten_alt(g: dict) -> float:
        if not g.get("zuletzt") or not letzte_zeit:
            return 1e9
        try:
            return (time.mktime(time.strptime(letzte_zeit, "%Y-%m-%d %H:%M"))
                    - time.mktime(time.strptime(g["zuletzt"], "%Y-%m-%d %H:%M"))) / 60.0
        except ValueError:
            return 1e9

    # Zur Momentaufnahme zählen Geraete, die in den letzten Minuten gemessen haben –
    # ein Rechner, der nur alle paar Minuten liefert, ist damit nicht "aus".
    aktuell_dabei = [g for g in gerate.values() if minuten_alt(g) <= AKTUELL_MIN]
    spaetestens = time.mktime(time.strptime(letzte_zeit, "%Y-%m-%d %H:%M")) if letzte_zeit else 0.0
    for g in gerate.values():
        # Reihenfolge zaehlt: w_aktuell wird unten erst aus quellen berechnet, also
        # den letzten eigenen Wert hier gleich aus den Quellen holen.
        g["w_zuletzt"] = round(sum(g["quellen"].values()), 1)
        g["aktuell"] = g in aktuell_dabei
        if not g["aktuell"]:
            alt_s = (spaetestens - time.mktime(time.strptime(g["zuletzt"], "%Y-%m-%d %H:%M"))
                     if g.get("zuletzt") else 1e12)
            g["verspaetet"] = alt_s <= 3600
            if not g["verspaetet"]:
                g["quellen"] = {}
                g["w_zuletzt"] = 0.0
        else:
            g["verspaetet"] = False

    def überbrücken(zeiten: dict[str, float], bis_minute: int | None = None) -> dict[str, float]:
        """Letzten Wert bis `AUSFUELL_MIN` Minuten weitertragen.

        Ein Rechner, der nur alle paar Minuten liefert (Import vom Windows-Plan),
        verschwindet sonst in jeder Lücke aus der Gesamtsumme: Kurve und Trend
        waeren Saegezahn-Artefakte der Uebertragung, nicht des Verbrauchs. Ein
        echtes Aus bleibt sichtbar – danach endet die Uebertragung.
        """
        if not zeiten:
            return {}
        gefüllt = {}
        vorher_min = vorher_w = None
        for zeit in sorted(zeiten):
            try:
                minute = int(time.mktime(time.strptime(zeit, "%Y-%m-%d %H:%M")) // 60)
            except ValueError:
                continue
            if vorher_min is not None and 0 < minute - vorher_min <= AUSFUELL_MIN:
                for lücke in range(vorher_min + 1, minute):
                    gefüllt[_zeitmarke(lücke)] = vorher_w
            gefüllt[zeit] = zeiten[zeit]
            vorher_min, vorher_w = minute, zeiten[zeit]
        # Auch nach der letzten eigenen Zeile weiterzaehlen, bis zur neuesten Messung
        # insgesamt – sonst faellt der Import-Rechner am Kurvenende aus der Summe.
        if (bis_minute is not None and vorher_min is not None
                and 0 < bis_minute - vorher_min <= AUSFUELL_MIN):
            for lücke in range(vorher_min + 1, bis_minute + 1):
                gefüllt[_zeitmarke(lücke)] = vorher_w
        return gefüllt

    def kaesten(zeiten: dict[str, float]) -> list[float]:
        """Zeitreihe in Kisten: 1 / 10 / 60 Minuten pro Punkt, gemittelt.

        Gelochte Minuten (Gerat aus) fehlen in der Teilmenge und werden nicht zu
        Null-Werten erfunden – die Kurve endet dann frueher.
        """
        _, minuten, anzahl = BEREICHE[bereich]
        kasten: dict[int, list[float]] = {}
        for zeit, wert in zeiten.items():
            try:
                minute = int(time.mktime(time.strptime(zeit, "%Y-%m-%d %H:%M")) // 60)
            except ValueError:
                continue
            kasten.setdefault(minute // minuten, []).append(wert)
        wenn = sorted(kasten)[-anzahl:]
        return [round(sum(kasten[k]) / len(kasten[k]), 1) for k in wenn]

    # Kurve der letzten Minuten + Tagesrandwerte: Zahlen ohne Verlauf erzaehlen nichts
    # ueberbrueckte Reihen: pro Geraet die eigene, gesamt die Summe der ueberbrueckten
    letzte_minute = (int(time.mktime(time.strptime(letzte_zeit, "%Y-%m-%d %H:%M")) // 60)
                     if letzte_zeit else None)
    gefüllt_pro_geraet = {gid: überbrücken(zeiten, letzte_minute)
                          for gid, zeiten in minuten_pro_geraet.items()}
    gesamt_gefüllt: dict[str, float] = {}
    for zeiten in gefüllt_pro_geraet.values():
        for zeit, wert in zeiten.items():
            gesamt_gefüllt[zeit] = gesamt_gefüllt.get(zeit, 0.0) + wert
    punkte = sorted(gesamt_gefüllt.items())
    for g in gerate.values():
        # Die eigene Kurve bleibt roh: eine überbrückte Minute wäre eine Annahme.
        # Nur die Summe wird überbrückt, sonst sägt der Import-Rhythmus durch sie durch.
        g["verlauf"] = kaesten(minuten_pro_geraet.get(g["id"], {}))
    # Trend gegen vor fuenf Minuten: eine Zahl ohne Richtung ist die halbe Antwort
    rueck = punkte[-6:-5] or punkte[:1]
    trend = round(punkte[-1][1] - rueck[0][1], 1) if punkte else 0.0
    heutige = [(z, w) for z, w in punkte if z[:10] == heute]
    verlauf = kaesten(gesamt_gefüllt)
    # Ø pro Tag über alle Geräte: "7 Tage" ohne Energiemaß ist eine halbe Zahl
    tage_gesamt: dict[str, float] = {}
    for wh_pro_geraet in wh_pro_tag.values():
        for datum, wh in wh_pro_geraet.items():
            tage_gesamt[datum] = tage_gesamt.get(datum, 0.0) + wh

    tageswerte = {
        "spitze": round(max((w for _, w in heutige), default=0.0), 1),
        "tief": round(min((w for _, w in heutige), default=0.0), 1),
        "mittel": round(sum(w for _, w in heutige) / len(heutige), 1) if heutige else 0.0,
        "minuten": len(heutige),
        "tage": sorted((datum, round(wh, 2)) for datum, wh in tage_gesamt.items())[-31:],
        "wh_tag": round(sum(tage_gesamt.values()) / len(tage_gesamt), 1) if tage_gesamt else 0.0,
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
        g["minuten_alt"] = minuten_alt(g) if g.get("zuletzt") and letzte_zeit else None
        if g.get("zuletzt") and letzte_zeit:
            try:
                g["minuten_alt"] = round((time.mktime(time.strptime(letzte_zeit, "%Y-%m-%d %H:%M"))
                                          - time.mktime(time.strptime(g["zuletzt"], "%Y-%m-%d %H:%M"))) / 60.0)
            except ValueError:
                pass
        g["tage"] = sorted(((tag, round(wh, 2)) for tag, wh in wh_pro_tag.get(g["id"], {}).items()),
                           key=lambda tv: tv[0])[-7:]
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
        "bereich": bereich,
        "bereich_text": BEREICHE[bereich][0],
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
