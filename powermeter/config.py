"""Konfiguration: globale Mess-/Preiswerte plus Geraete-Liste, und die Pfadfrage.

Zwei Dateien (beide menschenlesbar, UTF-8, keine Abhaengigkeiten):

  config.json   Messintervall, Strompreis, Grundpreis, Anzeigeeinstellungen
  devices.json  die Geraete und ihre Quellen – der Teil, der mehrere Geraete
                zusammenfuehrt (siehe docs/ARCHITECTURE.md)

Pfaderkennung (erste passende Regel gewinnt):
  1. --data <pfad>  bzw.  Umgebungsvariable POWERMETER_DATA
  2. Portable: <checkout>/config.json oder <checkout>/.portable vorhanden
     -> Konfiguration und Daten liegen direkt im Projektordner
  3. sonst benutzerueblich:
     Linux  ~/.config/powermeter  (config)  ~/.local/share/powermeter  (daten)
     Windows %APPDATA%\\powermeter            %LOCALAPPDATA%\\powermeter
     macOS  ~/Library/Application Support/powermeter (beides)
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import util
from .util import log_error

SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

DEFAULT_CONFIG = {
    "intervall_sekunden": 2,
    "strompreis_kwh_ct": 29.87,
    "grundpreis_eur_tag": 0.43,
    "grundpreis_einrechnen": False,
    "deckkraft": 0.92,
    "tarif_name": "",
    "_hinweis": "strompreis_kwh_ct = Arbeitspreis brutto in ct/kWh (Steckdosenenergie). "
                "intervall_sekunden = wie oft gemessen wird; ins CSV geschrieben wird pro Minute "
                "als Mittelwert. grundpreis_einrechnen=true rechnet den Grundpreis auf die "
                "gemessenen Tage hoch.",
}

DEFAULT_DEVICE = {
    "id": "pc",
    "name": "Dieser Rechner",
    "host": "",
    "wirkungsgrad": 0.90,
    "quellen": [
        {"typ": "auto", "bezeichnung": ""},
        {"typ": "fest", "wert_watt": 30, "bezeichnung": "Rest"},
    ],
}


def _xdg() -> tuple[Path, Path]:
    if util.IS_WINDOWS:
        cfg = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
        data = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        cfg = data = Path.home() / "Library" / "Application Support"
    else:
        cfg = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
        data = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
    return cfg / "powermeter", data / "powermeter"


class Paths:
    def __init__(self, config: Path, data: Path):
        self.config, self.data = config, data
        self.devices = config.parent / "devices.json"
        self.state = data / "state.json"
        self.errlog = data / "error.log"

    def ensure(self) -> None:
        self.data.mkdir(parents=True, exist_ok=True)
        self.config.parent.mkdir(parents=True, exist_ok=True)


def resolve_paths(explicit: str | None = None) -> Paths:
    """Siehe Modul-Doku; explicit = Wert von --data."""
    here = Path(__file__).resolve().parent.parent  # Checkout-Wurzel (enthaltaelt powermeter/)
    hint = explicit or os.environ.get("POWERMETER_DATA")
    if hint:
        p = Path(hint).expanduser().resolve()
        cfg = p if p.suffix == ".json" else p
        cfgdir = cfg.parent if p.suffix == ".json" else p
        return Paths(cfg if p.suffix == ".json" else cfgdir / "config.json",
                     p if p.suffix != ".json" else p.parent / "data")
    if (here / "config.json").exists() or (here / ".portable").exists():
        return Paths(here / "config.json", here / "data")
    cfgdir, datadir = _xdg()
    return Paths(cfgdir / "config.json", datadir)


# Englische Schluessel sind die Referenz. Die deutschen Namen der ersten Version
# bleiben akzeptiert, damit bestehende Installationen unveraendert weiterlaufen.
GERAET_KEYS = {"devices": "geraete", "efficiency": "wirkungsgrad", "sources": "quellen",
               "price_ct_kwh": "preis_ct_kwh", "enabled": "aktiv"}
# Englische Quellentypen sind die Referenz; die deutschen Namen bleiben gültig.
QUELLEN_TYP = {"fixed": "fest", "file": "datei", "battery": "batterie", "powercap": "rapl",
               "nvidia-gpu": "nvml", "amd-gpu": "adlx", "netio": "http",
               "other": "rest", "others": "rest", "display": "monitor", "network": "http"}
QUELLE_KEYS = {"type": "typ", "label": "bezeichnung", "watts": "wert_watt", "file": "pfad",
               "path": "pfad", "interval_s": "intervall_s", "unit": "einheit",
               "factor": "faktor", "max_age_s": "max_alter_s"}
CONFIG_KEYS = {"devices": "geraete", "interval_seconds": "intervall_sekunden", "price_ct_kwh": "strompreis_kwh_ct",
               "daily_base_fee_eur": "grundpreis_eur_tag", "include_base_fee": "grundpreis_einrechnen",
               "tariff_name": "tarif_name", "hosts": "rechner", "opacity": "deckkraft"}


def _umbenennen(d: dict, tabelle: dict) -> dict:
    out = dict(d)
    for englisch, deutsch in tabelle.items():
        if englisch in out and deutsch not in out:
            out[deutsch] = out[englisch]
    return out


def _clamp(x, lo, hi, default):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return min(hi, max(lo, v))


@dataclass
class Config:
    intervall_sekunden: float = 2.0
    strompreis_kwh_ct: float = 29.87
    grundpreis_eur_tag: float = 0.43
    grundpreis_einrechnen: bool = False
    deckkraft: float = 0.92           # Alpha des Widget-Fensters (0.4 - 1.0)
    tarif_name: str = ""
    rechner: list = field(default_factory=list)   # Fernrechner fuer `powermeter sync`
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict) -> "Config":
        raw = _umbenennen(raw, CONFIG_KEYS)
        d = {**DEFAULT_CONFIG, **{k: v for k, v in raw.items() if not k.startswith("_")}}
        return cls(
            intervall_sekunden=_clamp(d["intervall_sekunden"], 1, 60, 2.0),
            strompreis_kwh_ct=_clamp(d["strompreis_kwh_ct"], 0, 200, 29.87),
            grundpreis_eur_tag=_clamp(d["grundpreis_eur_tag"], 0, 20, 0.43),
            grundpreis_einrechnen=bool(d["grundpreis_einrechnen"]),
            deckkraft=_clamp(d.get("deckkraft", 0.92), 0.4, 1.0, 0.92),
            tarif_name=str(d.get("tarif_name") or ""),
            rechner=[r for r in (raw.get("rechner") or []) if isinstance(r, dict)],
            raw=raw,
        )

    def price_eur_kwh(self, override=None) -> float:
        return (_clamp(override, 0, 200, self.strompreis_kwh_ct)) / 100.0


def load_config(paths: Paths, prev: Config | None = None) -> Config:
    try:
        return Config.from_dict(json.loads(paths.config.read_text(encoding="utf-8")))
    except FileNotFoundError:
        try:
            paths.ensure()
            paths.config.write_text(json.dumps(DEFAULT_CONFIG, indent=2, ensure_ascii=False) + "\n",
                                    encoding="utf-8")
        except OSError as e:
            log_error(f"config.json kann nicht angelegt werden: {e}")
        return Config()
    except (OSError, ValueError, TypeError) as e:
        log_error(f"config.json fehlerhaft, nutze vorherige Werte: {e}")
        return prev or Config()


# ------------------------------------------------------------------- Geraete
@dataclass
class SourceSpec:
    typ: str                      # auto | fest | http | rapl | nvml | adlx | hwmon | batterie | sysfs
    bezeichnung: str = ""
    value: dict = field(default_factory=dict)   # Zusatzfelder (wert_watt, url, pfad, datei, host …)


@dataclass
class Device:
    id: str
    name: str
    host: str = ""                          # Rechnername, '' = auf jedem Rechner messbar
    wirkungsgrad: float = 0.90              # Steckdosenwirkungsgrad dieses Geraets
    preis_ct_kwh: float | None = None       #_override des globalen Arbeitspreises
    tarif: str = ""
    aktiv: bool = True
    quellen: list = field(default_factory=list)

    def owns_on(self, hostname: str) -> bool:
        """Darf dieses Geraet auf diesem Rechner gemessen werden?"""
        if not self.aktiv:
            return False
        if not self.host:
            return True
        return self.host.lower() == (hostname or "").lower()


@dataclass
class DeviceFile:
    devices: list
    raw: dict = field(default_factory=dict)

    def by_id(self, did: str) -> Device | None:
        for d in self.devices:
            if d.id == did:
                return d
        return None

    def ids(self) -> list:
        return [d.id for d in self.devices]


def _legacy_device_from_config(raw: dict) -> dict:
    """Alte Einzeldatei-Konfiguration (rest_watt/netzteil_wirkungsgrad) -> ein Geraet."""
    dev = dict(DEFAULT_DEVICE)
    dev["wirkungsgrad"] = _clamp(raw.get("netzteil_wirkungsgrad", 0.90), 0.5, 1.0, 0.90)
    dev["quellen"] = [
        {"typ": "auto", "bezeichnung": ""},
        {"typ": "fest", "wert_watt": _clamp(raw.get("rest_watt", 30), 0, 2000, 30),
         "bezeichnung": "Rest"},
    ]
    return dev


def load_devices(paths: Paths) -> DeviceFile:
    """devices.json lesen; fehlt sie, wird sie aus einer alten config.json erzeugt."""
    if not paths.devices.exists():
        legacy = {}
        try:
            legacy = json.loads(paths.config.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        data = {"_hinweis": "Ein Eintrag pro Geraet. 'host' leer = Geraet wird auf dem Rechner "
                            "gemessen, an dem powermeter laeuft (auto-Quellen erkennen die "
                            "Hardware). 'host' gesetzt = Geraet gehoert zu diesem Rechner, auf "
                            "anderen Rechnern wird es uebersprungen. Quellen-Typen: auto, fest, "
                            "http, rapl, nvml, adlx, hwmon, batterie. Siehe docs/ARCHITECTURE.md.",
                "geraete": [_legacy_device_from_config(legacy) if legacy else dict(DEFAULT_DEVICE)]}
        try:
            paths.ensure()
            paths.devices.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                                     encoding="utf-8")
        except OSError as e:
            log_error(f"devices.json kann nicht angelegt werden: {e}")
    try:
        raw = json.loads(paths.devices.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log_error(f"devices.json fehlerhaft: {e}")
        return DeviceFile([_parse_device(dict(DEFAULT_DEVICE))])
    out, seen = [], set()
    for entry in _umbenennen(raw, GERAET_KEYS).get("geraete", []):
        if not isinstance(entry, dict):
            continue
        dev = _parse_device(entry)
        if not dev.id or dev.id in seen:
            log_error(f"Geraet uebersprungen (id fehlt oder doppelt): {entry}")
            continue
        seen.add(dev.id)
        out.append(dev)
    if not out:
        out = [_parse_device(dict(DEFAULT_DEVICE))]
    return DeviceFile(out, raw)


def _parse_device(entry: dict) -> Device:
    did = str(entry.get("id") or "").strip().lower()
    if did and not SLUG.match(did):
        log_error(f"Geraet-id '{did}' nicht erlaubt (a-z0-9._-), ersetzt durch slug")
        did = re.sub(r"[^a-z0-9_-]", "", did)[:32] or "geraet"
    entry = _umbenennen(entry, GERAET_KEYS)
    specs = []
    for q in entry.get("quellen") or DEFAULT_DEVICE["quellen"]:
        if not isinstance(q, dict):
            continue
        q = _umbenennen(q, QUELLE_KEYS)
        if str(q.get("typ", "")) in QUELLEN_TYP:
            q["typ"] = QUELLEN_TYP[str(q["typ"])]
        extra = {k: v for k, v in q.items() if k not in ("typ", "bezeichnung")}
        specs.append(SourceSpec(typ=str(q.get("typ", "auto")),
                                bezeichnung=str(q.get("bezeichnung") or ""), value=extra))
    if not specs:
        specs = [SourceSpec(**s) for s in DEFAULT_DEVICE["quellen"]]
    return Device(
        id=did or "pc",
        name=str(entry.get("name") or did or "Geraet"),
        host=str(entry.get("host") or ""),
        wirkungsgrad=_clamp(entry.get("wirkungsgrad", 0.90), 0.3, 1.0, 0.90),
        preis_ct_kwh=(None if entry.get("preis_ct_kwh") in (None, "")
                      else _clamp(entry.get("preis_ct_kwh"), 0, 200, 0.0)),
        tarif=str(entry.get("tarif") or ""),
        aktiv=bool(entry.get("aktiv", True)),
        quellen=specs,
    )
