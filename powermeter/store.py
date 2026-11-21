"""Ablegen und Wiederholen der Messreihen.

Format ( Schema v2 ):  data/JJJJ-MM.csv
    zeit,geraet,quelle,sekunden,watt
    2026-11-21 13:21,desktop,cpu,60.0,48.7
Eine Zeile pro Geraet und Quelle pro Minute. Das ist bewusst "lang" (tidy) statt
breit: neue Quellen aendern kein Schema, und Dateien von mehreren Rechnern lassen
sich durch simples Zusammenlegen kombinieren (siehe docs/ARCHITECTURE.md).

Alte Einzeldatei-Logs ( v1 mit Spalten cpu_w/nvidia_w/amd_w …) werden lesend
unterstuetzt und dem Geraet `pc` zugeordnet.

data/state.json haelt die laufenden Summen; fehlt oder beschaedigt, werden sie aus
den CSVs neu aufgebaut.
"""
from __future__ import annotations

import csv
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from .util import log_error

V1_HEAD = ["zeit", "sekunden", "cpu_w", "nvidia_w", "amd_w", "rest_w",
           "netzteil_wirkungsgrad", "gesamt_w"]
HEAD = ["zeit", "geraet", "quelle", "sekunden", "watt"]
GAP_S = 15  # Lueber ueber dieser Dauer (Standby, Rechner aus) sind keine Messsekunden


@dataclass
class Row:
    zeit: str          # 'YYYY-MM-DD HH:MM'
    geraet: str
    quelle: str
    sekunden: float
    watt: float

    @property
    def tag(self) -> str:
        return self.zeit[:10]

    @property
    def wh(self) -> float:
        """Gemessene (nicht wirkungsgrad-korrigierte) Stundenenergie in Wh."""
        return self.watt * self.sekunden / 3600.0


def is_v1(header: list[str]) -> bool:
    return header[:2] == V1_HEAD[:2] and any(h.endswith("_w") for h in header) and "geraet" not in header


def _v1_rows(reader: csv.DictReader, geraet: str):
    """v1-Breitzeile -> lange Zeilen pro Quelle (eff-Werte werden ignoriert)."""
    for row in reader:
        try:
            s = float(row["sekunden"])
        except (KeyError, TypeError, ValueError):
            continue
        for key, val in row.items():
            if not key or not key.endswith("_w") or key == "gesamt_w":
                continue
            try:
                w = float(val)
            except (TypeError, ValueError):
                continue
            yield Row(row.get("zeit", ""), geraet, key[:-2], s, w)


class Store:
    """Puffert die aktuelle Minute und schreibt sie als Zeilen; haelt laufende Summen."""

    def __init__(self, paths, default_device: str = "pc"):
        self.paths, self.default_device = paths, default_device
        paths.data.mkdir(parents=True, exist_ok=True)
        self.state = self._load()
        self.minute_key: str | None = None
        self.buffers: dict[str, dict[str, tuple[float, float]]] = {}  # geraet -> quelle -> (W*s, s)

    # ------------------------------------------------------------ state.json
    def _blank_state(self) -> dict:
        return {"version": 2, "seit": "", "fenster": {}, "geraete": {}}

    def _load(self) -> dict:
        alt: dict = {}
        try:
            st = json.loads(self.paths.state.read_text(encoding="utf-8"))
            if isinstance(st, dict):
                alt = st
                if st.get("version") == 2 and isinstance(st.get("geraete"), dict):
                    st.setdefault("seit", "")
                    return st
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            log_error(f"state.json unlesbar: {e}")
        return self._rebuild_state(alt)

    def _rebuild_state(self, vorher: dict | None = None) -> dict:
        """Summen aus den CSVs neu bilden; Anzeigeschluessel bleiben erhalten.

        `vorher` ist der inhaltlich alte state-Inhalt - noetig, weil diese Methode
        auch aus `_load` heraus aufgerufen wird, bevor `self.state` existiert.
        """
        st = self._blank_state()
        n = 0
        for row in self.rows():
            dev = st["geraete"].setdefault(row.geraet, {"sekunden": 0.0, "quellen": {}})
            dev["sekunden"] += row.sekunden
            dev["quellen"][row.quelle] = dev["quellen"].get(row.quelle, 0.0) + row.wh
            st["seit"] = min(st["seit"], row.zeit[:10]) if st["seit"] else row.zeit[:10]
            n += 1
        if vorher is None:
            vorher = getattr(self, "state", None) or {}
        for schluessel in ("fenster", "aus", "sync"):   # Anzeige/Schalter sind keine Summen
            if schluessel in vorher:
                st[schluessel] = vorher[schluessel]
        if n:
            log_error(f"state.json aus {n} CSV-Zeilen neu aufgebaut")
        return st

    def save(self) -> None:
        tmp = self.paths.state.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1), encoding="utf-8")
        os.replace(tmp, self.paths.state)

    # ------------------------------------------------------- Aufnehmen/Schreiben
    def device_state(self, device_id: str) -> dict:
        return self.state["geraete"].setdefault(device_id, {"sekunden": 0.0, "quellen": {}})

    def add(self, device_id: str, watts: dict[str, float], seconds: float, now: float | None = None) -> None:
        key = time.strftime("%Y-%m-%d %H:%M", time.localtime(now if now is not None else time.time()))
        if self.minute_key is None:
            self.minute_key = key
        if key != self.minute_key:
            self.flush()
            self.minute_key = key
        buf = self.buffers.setdefault(device_id, {})
        dev = self.device_state(device_id)
        dev["sekunden"] += seconds
        for q, w in watts.items():
            acc_w, acc_s = buf.get(q, (0.0, 0.0))
            buf[q] = (acc_w + w * seconds, acc_s + seconds)
            qd = dev["quellen"]
            qd[q] = qd.get(q, 0.0) + w * seconds / 3600.0
        if not self.state.get("seit"):
            self.state["seit"] = self.minute_key[:10]

    def flush(self) -> None:
        """Gepufferte Minute(n) als Zeilen ins Monats-CSV schreiben."""
        if not self.buffers or not self.minute_key:
            return
        key, gepuffert, self.buffers = self.minute_key, self.buffers, {}
        path = self.paths.data / f"{key[:7]}.csv"
        new = not path.exists()
        try:
            with path.open("a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(HEAD)
                for geraet in sorted(gepuffert):
                    for quelle, (ws, s) in sorted(gepuffert[geraet].items()):
                        if s <= 0:
                            continue
                        w.writerow([key, geraet, quelle, round(s, 1), round(ws / s, 1)])
        except OSError as e:
            log_error(f"CSV {path.name} nicht schreibbar: {e}")
        self.save()

    # ----------------------------------------------------------------- Lesen
    def rows(self, months: list[str] | None = None):
        """Alle Messzeilen aus allen Monats-CSVs (v1 und v2), zeitlich aufsteigend."""
        files = sorted(self.paths.data.glob("????-??.csv"))
        if months:
            files = [p for p in files if p.stem in months]
        for path in files:
            yield from read_rows(path, self.default_device)

    def geraete_im_daten(self) -> list[str]:
        """Geraete-Ids, die in den CSVs vorkommen (auch ohne Eintrag in devices.json)."""
        ids = set(self.state.get("geraete", {}))
        for row in self.rows():
            ids.add(row.geraet)
        return sorted(ids)

    def stumme(self) -> dict[str, list]:
        """ruhiggestellte Quellen und Geraete (state.json -> "aus")."""
        aus = self.state.get("aus") or {}
        return {"typ": list(aus.get("typ") or []), "geraet": list(aus.get("geraet") or [])}

    def stumm_setzen(self, bereich: str, schluessel: str, aus: bool) -> None:
        liste = self.state.setdefault("aus", {"typ": [], "geraet": []}).setdefault(bereich, [])
        if aus and schluessel not in liste:
            liste.append(schluessel)
        if not aus and schluessel in liste:
            liste.remove(schluessel)
        self.save()

    def unflushed(self) -> dict[str, dict[str, tuple[float, float]]]:
        """Puffer der noch laufenden Minute (device -> quelle -> (W*s, s))."""
        return self.buffers

    def minute(self) -> str | None:
        return self.minute_key


# ------------------------------------------------------------------ Multi-Geräte
def read_rows(path: Path, default_device: str = "pc", seit: str | None = None):
    """Ein Monats-CSV (v1 oder v2) -> Row-Objekte.

    seit = 'YYYY-MM-DD' oder 'YYYY-MM-DD HH:MM': nur Zeilen ab diesem Zeitpunkt.
    Der Vergleich ist lexikografisch, weil beide Formate so sortierbar sind.
    """
    try:
        with path.open(newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            header = next(reader, [])
            if header[:len(HEAD)] == HEAD:
                for rec in reader:
                    if len(rec) < len(HEAD):
                        continue
                    try:
                        row = Row(rec[0], rec[1], rec[2], float(rec[3]), float(rec[4]))
                    except ValueError:
                        continue
                    if seit and row.zeit < seit:
                        continue
                    yield row
            elif is_v1(header):
                for row in _v1_rows(csv.DictReader(f, fieldnames=header), default_device):
                    if seit and row.zeit < seit:
                        continue
                    yield row
            else:
                log_error(f"{path.name}: unbekannter CSV-Kopf {header[:3]}")
    except OSError as e:
        log_error(f"CSV {path.name} nicht lesbar: {e}")


def parse_rows(text: str, default_device: str = "pc"):
    """CSV-Text (Kopf plus Zeilen) -> Row-Objekte; fuer Fernuebernahme per SSH."""
    reader = csv.reader([z for z in text.splitlines() if z.strip()])
    header = next(reader, [])
    if header[:len(HEAD)] == HEAD:
        for rec in reader:
            if len(rec) < len(HEAD):
                continue
            try:
                yield Row(rec[0], rec[1], rec[2], float(rec[3]), float(rec[4]))
            except ValueError:
                continue
    elif is_v1(header):
        for rec in reader:
            zeilen = dict(zip(header, rec))
            try:
                s = float(zeilen["sekunden"])
            except (KeyError, TypeError, ValueError):
                continue
            for key, wert in zeilen.items():
                if not key or not key.endswith("_w") or key == "gesamt_w":
                    continue
                try:
                    yield Row(zeilen.get("zeit", ""), default_device, key[:-2], s, float(wert))
                except (TypeError, ValueError):
                    continue
    else:
        log_error(f"fern gelieferte Zeilen: unbekannter Kopf {header[:3]}")


def append_rows(paths, rows, default_device: str = "pc") -> tuple[int, int]:
    """Row-Objekte duplettenfrei in die eigenen Monats-CSVs einreihen.

    Dupletten erkennen ueber (zeit, geraet, quelle): dieselbe Quelle mehrfach zu
    uebernehmen ist folgenlos. Rueckgabe: (uebernommen, dupeliert).
    """
    paths.data.mkdir(parents=True, exist_ok=True)
    seen: set[tuple[str, str, str]] = set()
    for path in sorted(paths.data.glob("????-??.csv")):
        for row in read_rows(path, default_device):
            seen.add((row.zeit, row.geraet, row.quelle))
    neu = dupe = 0
    for row in rows:
        key = (row.zeit, row.geraet, row.quelle)
        if key in seen:
            dupe += 1
            continue
        seen.add(key)
        target = paths.data / f"{row.zeit[:7]}.csv"
        fresh = not target.exists()
        with target.open("a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if fresh:
                w.writerow(HEAD)
            w.writerow([row.zeit, row.geraet, row.quelle, round(row.sekunden, 1),
                        round(row.watt, 1)])
        neu += 1
    return neu, dupe


def rebuild_state(paths, default_device: str = "pc", fenster: dict | None = None) -> dict:
    """Summen in state.json aus den CSVs neu bilden; Fensterposition bleibt erhalten."""
    st = Store(paths, default_device)
    neu = st._rebuild_state()
    neu["fenster"] = fenster if fenster is not None else st.state.get("fenster", {})
    st.state = neu
    st.save()
    return neu


def merge_data(paths, other_dirs, default_device: str = "pc") -> tuple[int, int]:
    """Ordner/Dateien anderer Messplaetze duplettenfrei uebernehmen und Summen neu bauen.

    Der niedrige Weg, Rechner zu kombinieren: jeder schreibt eigene Dateien,
    `merge` legt sie zusammen. Kein Dienst, kein offener Port.
    """
    rows = []
    for other in other_dirs:
        o = Path(other).expanduser()
        files = ([o] if o.is_file()
                 else sorted(list(o.glob("????-??.csv")) + list((o / "data").glob("????-??.csv"))))
        if not files:
            log_error(f"{o}: keine Monats-CSVs gefunden")
            continue
        for src in files:
            if src.parent == paths.data:
                continue
            rows.extend(read_rows(src, default_device))
    neu, dupe = append_rows(paths, rows, default_device)
    rebuild_state(paths, default_device)
    return neu, dupe
