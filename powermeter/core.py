"""Mess-Engine: lies alle Quellen aller Geraete, haelt lueckenlos die Zeit fest und
schiebt alles in den Store. Widget, Report und Headless-Betrieb nutzen dieselbe Engine.

Wichtig fuer mehrere Geraete:
 * pro Geraet eigene Quellen, eigener Wirkungsgrad, optional eigener Preis
 * Netzwerkquellen werden nicht im Sekundentakt abgefragt (read_interval)
 * Quellen, die mehrfach fehlschlagen, werden neu erkannt (Treiber-Update, Stecker umgesteckt)
"""
from __future__ import annotations

import socket
import time
from pathlib import Path

from . import sources as srcmod
from .config import load_config, load_devices
from .sources.base import Source
from .stats import summary
from .store import GAP_S, Store
from .util import log_error, set_data_dir


def mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def host_name() -> str:
    """Rechnername dieses Systems; POWERMETER_HOSTNAME ueberschreibt ihn.

    Noetig, weil WSL2 standardmaessig den Windows-Namen kopiert – mit der Variable
    kann man einem Geraet trotzdem eindeutig einen Messrechner zuordnen.
    """
    import os

    return (os.environ.get("POWERMETER_HOSTNAME") or socket.gethostname()).lower()


class Core:
    def __init__(self, paths, geraete: list[str] | None = None):
        set_data_dir(paths.data)
        self.paths = paths
        self.cfg, self.cfg_mtime = load_config(paths), mtime(paths.config)
        self.devices, self.dev_mtime = load_devices(paths), mtime(paths.devices)
        self.store = Store(paths, default_device=self.devices.ids()[0] if self.devices.ids() else "pc")
        self.host = host_name()
        self.nur = set(geraete) if geraete else None
        self.sources: dict[str, list[Source]] = {}
        self.fails: dict[str, int] = {}
        self.power: dict[str, dict[str, float]] = {}
        self.cache: dict[tuple[str, str], tuple[float, float]] = {}
        self.stumm: dict[str, set] = {"typ": set(), "geraet": set()}
        self.last_t = self.last_cfg_check = self.last_detect = time.time()
        self._sync_stumm()
        self._sum_cache: tuple[float, dict] | None = None
        self.detect()

    # ---------------------------------------------------------------- Geraete
    def device_ids(self) -> list[str]:
        return [d.id for d in self.devices.devices if self.nur is None or d.id in self.nur]

    def alle_geraete(self) -> list[str]:
        """Konfigurierte Geraete plus die aus uebernommenen Messreihen anderer Rechner."""
        return sorted({d.id for d in self.devices.devices if self.nur is None or d.id in self.nur}
                      | set(self.store.geraete_im_daten()))

    def kennung(self, did: str) -> str:
        """Anzeigename; fuer Geraete ohne Eintrag in devices.json einfach die id."""
        dev = self.devices.by_id(did)
        return dev.name if dev else did

    def measurable(self) -> list[str]:
        """Geraete, die auf diesem Rechner gemessen werden duerfen (host-Freigabe)."""
        return [d.id for d in self.devices.devices
                if (self.nur is None or d.id in self.nur) and d.owns_on(self.host)
                and d.id not in self.stumm.get("geraet", ())]

    def _sync_stumm(self) -> None:
        """Schalter aus state.json in die Engine holen (Widget und CLI schalten dort)."""
        aus = self.store.stumme()
        self.stumm = {"typ": set(aus["typ"]), "geraet": set(aus["geraet"])}

    def detect(self) -> None:
        srcmod.close_all([s for lst in self.sources.values() for s in lst])
        self.sources, self.fails, self.cache = {}, {}, {}
        for did in self.measurable():
            lst = srcmod.make_sources(self.devices.by_id(did), self.host)
            self.sources[did] = lst
            self.fails[did] = {s.key: 0 for s in lst}
            self.power[did] = {s.key: 0.0 for s in lst}
            if not lst:
                log_error(f"Geraet {did}: keine Quelle auf diesem Rechner nutzbar "
                          f"(host-Freigabe oder Treiber?) – siehe `powermeter scan`")

    def reload_if_changed(self) -> None:
        now = time.time()
        if now - self.last_cfg_check < 5:
            return
        self.last_cfg_check = now
        if mtime(self.paths.config) != self.cfg_mtime:
            self.cfg_mtime, self.cfg = mtime(self.paths.config), load_config(self.paths, self.cfg)
        if mtime(self.paths.devices) != self.dev_mtime:
            self.dev_mtime, self.devices = mtime(self.paths.devices), load_devices(self.paths)
            self.detect()

    def _broken(self) -> bool:
        return any(n >= 30 for per in self.fails.values() for n in per.values())

    # ---------------------------------------------------------------- Messung
    def _read_source(self, did: str, src: Source, now: float) -> float | None:
        ivl = getattr(src, "read_interval", None)
        ck = (did, src.key)
        if ivl and ck in self.cache and now - self.cache[ck][1] < ivl:
            return self.cache[ck][0]
        try:
            v = src.read()
        except Exception as e:
            log_error(f"{did}/{src.key}: {e}")
            v = None
        self.cache[ck] = (v if v is not None else self.cache.get(ck, (None, 0))[0], now)
        return self.cache[ck][0]

    def stummquellen(self, did: str) -> list[tuple[str, str]]:
        """Quellen dieses Geraets mit ihrem Ruhe-Zustand: (key, label), schaltbar."""
        return [(s.key, s.label or s.key) for s in self.sources.get(did, [])]

    def step(self) -> None:
        self.reload_if_changed()
        self._sync_stumm()
        now = time.time()
        if self._broken() and now - self.last_detect > 60:
            self.last_detect = now
            log_error("Quellen fehlschlagen dauerhaft -> Neu erkennen")
            self.detect()
        for did, lst in self.sources.items():
            werte = {}
            for s in lst:
                if s.key in self.stumm["typ"]:
                    werte[s.key] = self.power.get(did, {}).get(s.key, 0.0)
                    continue
                v = self._read_source(did, s, now)
                fail = self.fails.setdefault(did, {})
                if v is None or not 0 <= v < 5000:
                    fail[s.key] = fail.get(s.key, 0) + 1
                    v = self.power.get(did, {}).get(s.key, 0.0)
                else:
                    fail[s.key] = 0
                werte[s.key] = v
            self.power[did] = werte
        gap, self.last_t = now - self.last_t, now
        if 0 < gap <= max(GAP_S, 3 * self.cfg.intervall_sekunden):
            for did, werte in self.power.items():
                if werte and did not in self.stumm["geraet"]:
                    self.store.add(did, werte, gap, now)

    # ------------------------------------------------------------- Anzeigewerte
    def watt_geraet(self, did: str) -> float:
        if did in self.stumm["geraet"]:
            return 0.0
        dev = self.devices.by_id(did)
        eff = dev.wirkungsgrad if dev else 0.9
        return sum(self.power.get(did, {}).values()) / eff if eff else 0.0

    def watt_gesamt(self, geraete: list[str] | None = None) -> float:
        ids = geraete or self.device_ids()
        return sum(self.watt_geraet(d) for d in ids)

    def parts(self, did: str) -> list[tuple[str, float]]:
        labels = {s.key: s.label for s in self.sources.get(did, [])}
        return [(labels.get(k, k), v) for k, v in sorted(self.power.get(did, {}).items())]

    def summary(self, ttl: float = 60.0, geraete: list[str] | None = None) -> dict:
        now = time.time()
        if self._sum_cache and now - self._sum_cache[0] < ttl:
            return self._sum_cache[1]
        s = summary(self.store, self.devices.devices, self.cfg,
                    geraete or self.alle_geraete())
        self._sum_cache = (now, s)
        return s

    def close(self) -> None:
        self.store.flush()
        srcmod.close_all([s for lst in self.sources.values() for s in lst])
        self.sources = {}
