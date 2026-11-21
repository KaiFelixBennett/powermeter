"""Geschaetzte und vorgegebene Quellen.

`fest`   konstanter Zuschlag (Mainboard, RAM, SSDs, Luefter, Netzteilverlust …)
`datei`  Leistungswert, den ein anderes Programm in eine Datei schreibt
         (praktisch, wenn ein Rechner/Geraet nicht direkt erreichbar ist:
         ein Cronjob oder rsync legt dort alle paar Minuten Watt ab)
`monitor` wie fest, nur mit eigenem Namen – damit der Monitor separat steht
"""
from __future__ import annotations

from pathlib import Path

from ..util import log_error
from .base import Source, SourceError


class FixedSource(Source):
    kind, key, label = "fest", "rest", "Rest"

    def __init__(self, spec=None):
        v = getattr(spec, "value", {}) if spec else {}
        try:
            self.watt = float(v.get("wert_watt", 0))
        except (TypeError, ValueError):
            raise SourceError(f"wert_watt keine Zahl: {v.get('wert_watt')!r}")
        if not 0 <= self.watt < 5000:
            raise SourceError(f"wert_watt ausser Bereich: {self.watt}")
        if spec and spec.bezeichnung:
            self.label = spec.bezeichnung
        self.key = str(v.get("key") or self.key)

    def read(self):
        return self.watt


class FilePower(Source):
    kind, key = "datei", "datei"
    read_interval = 30.0

    def __init__(self, spec=None):
        v = getattr(spec, "value", {}) if spec else {}
        p = v.get("pfad")
        if not p:
            raise SourceError("datei-Quelle braucht 'pfad'")
        self.path = Path(p).expanduser()
        self.key = str(v.get("key") or self.key)
        self.label = (spec.bezeichnung if spec and spec.bezeichnung else self.key)
        self.einheit = str(v.get("einheit") or "w").lower()
        self.altersgrenze = float(v.get("max_alter_s", 900))
        self.read_interval = float(v.get("intervall_s", self.read_interval))

    def read(self):
        try:
            text = self.path.read_text(encoding="utf-8").strip()
            mtime = self.path.stat().st_mtime
        except (OSError, ValueError) as e:
            log_error(f"datei-Quelle {self.path}: {e}")
            return None
        import time

        if self.altersgrenze and time.time() - mtime > self.altersgrenze:
            log_error(f"datei-Quelle {self.path}: aelter als {self.altersgrenze:.0f} s")
            return None
        first = text.splitlines()[0] if text else ""
        try:
            w = float(first.split()[0])
        except (ValueError, IndexError):
            log_error(f"datei-Quelle {self.path}: erste Zeile keine Zahl: {first!r}")
            return None
        return w * {"w": 1.0, "kw": 1000.0, "mw": 0.001}[self.einheit]
