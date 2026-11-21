"""Linux-Quellen: sysfs/powercap. Alles ohne root-Recht moeglich, *falls* die
Dateien lesbar sind – siehe README (udev-Regel fuer RAPL).

Unterstuetzte Quellen:
  rapl      /sys/class/powercap/intel-rapl:*/energy_uj  (µJ-Zaehler, Differenz/Dt)
  hwmon     /sys/class/hwmon/hwmon*/power*_input oder card*/device/hwmon (W)
  batterie  /sys/class/power_supply/BAT*/power_now (µW) bzw. Volt*Strom

Einheiten sind bei hwmon leider treiberabhaengig; deshalb ist die Einheit
Konfiguration ("einheit": "mw" | "uw"), nicht Raten. `powermeter scan` zeigt die
Rohwerte, damit man sie mit einem Steckdosenmessgeraet abgleichen kann.
"""
from __future__ import annotations

import glob
import os
import time
from pathlib import Path

from ..util import IS_LINUX, log_error
from .base import Source, SourceError

SYSFS_ENV = "POWERMETER_SYSFS"   # Fuer Tests/Container auf einen anderen Pfad setzbar


def sysfs_root(spec=None) -> Path:
    if spec is not None:
        p = getattr(spec, "value", {}).get("sysfs")
        if p:
            return Path(p)
    env = os.environ.get(SYSFS_ENV)
    return Path(env) if env else Path("/sys")


class _Delta:
    """Zaehler (µJ o. a.) in Leistung umrechnen, inkl. Ueberlaufbehandlung."""

    def __init__(self, scale_to_w: float):
        self.prev_v = self.prev_t = None
        self.scale = scale_to_w

    def power(self, value: float, maximum: float | None, now: float) -> float | None:
        p_v, p_t = self.prev_v, self.prev_t
        self.prev_v, self.prev_t = value, now
        if p_v is None or p_t is None:
            return None
        d, dt = value - p_v, now - p_t
        if d < 0:
            if maximum and maximum > 0:
                d += maximum
            else:
                return None
        if dt <= 0 or d < 0:
            return None
        return d * self.scale / dt


class PowercapRapl(Source):
    """Paketleistung ueber powercap (Intel RAPL; auf AMD-CPUs oft nicht vorhanden)."""

    kind, key = "rapl", "cpu"
    label = "CPU (RAPL)"

    def __init__(self, spec=None):
        self.root = sysfs_root(spec) / "class" / "powercap"
        self.domains, names = [], []
        for d in sorted(glob.glob(str(self.root / "intel-rapl:*"))):
            if d.count(":") != 1 or not os.path.isfile(os.path.join(d, "energy_uj")):
                continue  # Untersdomains (intel-rapl:0:0) nicht doppelt zaehlen
            energy = Path(d) / "energy_uj"
            try:
                energy.read_text().strip()
            except OSError as e:
                raise SourceError(f"{energy} nicht lesbar (Berechtigung?): {e}")
            maximum = 0.0
            try:
                maximum = float((Path(d) / "max_energy_range_uj").read_text().strip())
            except (OSError, ValueError):
                pass
            label = ""
            try:
                label = (Path(d) / "label").read_text().strip()
            except OSError:
                pass
            self.domains.append((energy, maximum, _Delta(1e-6)))  # µJ -> J -> W
            names.append(label or Path(d).name)
        if not self.domains:
            raise SourceError(f"keine RAPL-Domainen unter {self.root} gefunden")
        self.label = "CPU (" + ", ".join(names) + ")"

    def read(self):
        now = time.time()
        vals = []
        for energy, maximum, acc in self.domains:
            try:
                v = float(energy.read_text().strip())
            except (OSError, ValueError):
                return None
            p = acc.power(v, maximum, now)
            if p is None:
                return None
            vals.append(p)
        return sum(vals)


class HwmonPower(Source):
    """Leistung aus hwmon-Attributen power<N>_input (meist Milliwatt).

    spec-Felder: name (hwmon-Treibername, z. B. 'nvidia', 'k10temp', 'amdgpu'),
    basis (z. B. '/sys/class/drm/card0/device' fuer GPU-hwmon), label (Teilstring
    des power*-label), einheit ('mw' Standard, 'uw' fuer einige amdgpu-Staende).
    """

    kind, key = "hwmon", "hwmon"

    def __init__(self, spec=None):
        v = getattr(spec, "value", {}) if spec else {}
        basis = v.get("basis") or str(sysfs_root(spec) / "class" / "hwmon")
        root = Path(basis)
        if not root.is_dir() and any(ch in basis for ch in "*?["):
            hits = [Path(p) for p in sorted(glob.glob(basis)) if Path(p).is_dir()]
            if not hits:
                raise SourceError(f"{basis} (Glob) trifft nichts")
            root = hits[0]
        self.root = root
        self.want_name = str(v.get("name") or "").lower()
        self.want_label = str(v.get("label") or "").lower()
        self.einheit = str(v.get("einheit") or "mw").lower()
        if not self.root.is_dir():
            raise SourceError(f"{self.root} existiert nicht")
        self.files = []
        for pat in ("hwmon*/power*_input", "power*_input", "hwmon*/device/power*_input"):
            for f in sorted(glob.glob(str(self.root / pat))):
                pf = Path(f)
                if pf in self.files or not self._wanted(pf):
                    continue
                self.files.append(pf)
        if not self.files:
            raise SourceError(f"keine power*_input unter {self.root} gefunden")
        self.key = str(v.get("key") or (self.want_name or "hwmon"))
        self.label = str(v.get("bezeichnung") or self.key)

    def _wanted(self, path: Path) -> bool:
        idx = path.name[5:-6]  # power3_input -> '3'
        base = path.parent
        name = ""
        try:
            name = (base / "name").read_text().strip().lower()
        except OSError:
            pass
        if self.want_name and name and self.want_name != name:
            return False
        if self.want_label:
            try:
                lab = (base / f"power{idx}_label").read_text().strip().lower()
            except OSError:
                lab = ""
            if self.want_label not in lab:
                return False
        return True

    def read(self):
        total = 0.0
        for f in self.files:
            try:
                raw = float(f.read_text().strip())
            except (OSError, ValueError):
                return None
            total += raw / (1_000_000 if self.einheit == "uw" else 1000)
        return total


class BatteryPower(Source):
    """Batteriemittlungsleistung eines Notebooks in Watt (µW nach Kernel-ABI)."""

    kind, key, label = "batterie", "batterie", "Akku"

    def __init__(self, spec=None):
        root = sysfs_root(spec) / "class" / "power_supply"
        if not root.is_dir():
            raise SourceError(f"{root} existiert nicht")
        self.batts = []
        for d in sorted(root.iterdir()):
            if not (d / "type").is_file():
                continue
            try:
                if (d / "type").read_text().strip().lower() != "battery":
                    continue
            except OSError:
                continue
            if (d / "status").is_file():
                try:
                    if (d / "status").read_text().strip().lower() == "unknown":
                        continue
                except OSError:
                    pass
            self.batts.append(d)
        if not self.batts:
            raise SourceError("kein Akku gefunden (kein Notebook?)")
        self.label = " + ".join(b.name for b in self.batts) + " (Akku)"

    def read(self):
        total = 0.0
        for b in self.batts:
            try:
                p = (b / "power_now").read_text().strip()
            except OSError:
                p = ""
            if p:
                try:
                    total += float(p) / 1_000_000  # µW -> W
                except ValueError:
                    return None
                continue
            try:
                uv = float((b / "voltage_now").read_text().strip())
                ia = float((b / "current_now").read_text().strip())
            except (OSError, ValueError):
                return None
            total += abs(uv * ia) / 1e12  # µV * µA -> W
        return total


def auto_linux() -> list[Source]:
    """Lokale Linux-Hardware: CPU-Leistung, NVIDIA-GPU, AMD-GPU (hwmon), Akku."""
    out: list[Source] = []
    try:
        out.append(PowercapRapl())
    except SourceError as e:
        log_error(f"RAPL nicht verfuegbar: {e}")
    try:
        from .nvidia import NvidiaGpus

        out.append(NvidiaGpus())
    except (SourceError, OSError) as e:
        log_error(f"NVML nicht verfuegbar: {e}")
    try:
        gpu = HwmonPower(_AmdSpec())
        out.append(gpu)
    except (SourceError, OSError) as e:
        log_error(f"AMD-GPU-hwmon nicht verfuegbar: {e}")
    try:
        out.append(BatteryPower())
    except SourceError as e:
        log_error(f"Akku nicht verfuegbar: {e}")
    if not out:
        log_error("Keine Linux-Hardwarequelle gefunden (RAPL-Ausnahme? siehe README udev-Regel)")
    return out


class _AmdSpec:
    """Vorbelegte hwmon-Quelle fuer amdgpu (Einheit treiberabhaengig -> 'uw')."""
    value = {"name": "amdgpu", "basis": "/sys/class/drm/card*/device", "einheit": "uw",
             "key": "amd", "bezeichnung": "AMD GPU"}
