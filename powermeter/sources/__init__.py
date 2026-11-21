"""Quellen-Registry: aus den Angaben in devices.json werden konkrete Objekte.

Neue Quelle anlegen = eine Klasse mit kind/key/writeable read() schreiben und hier
in REGISTRY eintragen. Fertig – CLI, Logger, Widget und Report nutzen sie automatisch.
"""
from __future__ import annotations

import socket

from ..util import IS_LINUX, IS_WINDOWS, log_error
from .base import Source, SourceError
from .fixed import FilePower, FixedSource
from .net import HttpPower


def _win_classes():
    from .win import WinAmdGpus, WinCpuRapl  # importiert nur auf Windows

    return WinCpuRapl, WinAmdGpus


def _local_platform_sources() -> list[Source]:
    """Quellen des Rechners, auf dem powermeter gerade laeuft."""
    out: list[Source] = []
    if IS_WINDOWS:
        CpuRapl, AmdGpus = _win_classes()
        for cls in (CpuRapl, _nvidia_cls(), AmdGpus):
            if cls is None:
                continue
            try:
                out.append(cls())
            except (SourceError, OSError) as e:
                log_error(f"{cls.__name__} nicht verfuegbar: {e}")
    else:
        from .linux import auto_linux

        out = auto_linux()
    return out


def _nvidia_cls():
    try:
        from .nvidia import NvidiaGpus

        return NvidiaGpus
    except (SourceError, OSError) as e:  # pragma: no cover
        log_error(f"NVML-Modul nicht nutzbar: {e}")
        return None


REGISTRY = ("auto", "fest", "rest", "monitor", "datei", "http", "nvml", "rapl", "adlx",
            "hwmon", "batterie")


def _linux_cls(name: str):
    from . import linux

    return getattr(linux, name)


def _resolve(typ: str):
    if typ in ("fest", "rest", "monitor"):
        return FixedSource
    if typ == "datei":
        return FilePower
    if typ == "http":
        return HttpPower
    if typ == "nvml":
        return _nvidia_cls()
    if typ == "rapl":
        return _win_classes()[0] if IS_WINDOWS else _linux_cls("PowercapRapl")
    if typ == "adlx":
        if not IS_WINDOWS:
            raise SourceError("'adlx' gibt es nur unter Windows (AMD-GPU: unter Linux 'hwmon')")
        return _win_classes()[1]
    if typ in ("hwmon", "batterie"):
        return _linux_cls("HwmonPower" if typ == "hwmon" else "BatteryPower")
    raise SourceError(f"unbekannter Quellentyp {typ!r} (erlaubt: {', '.join(REGISTRY)})")


def make_sources(device, hostname: str | None = None) -> list[Source]:
    """Alle Quellen eines Geraets, die auf diesem Rechner messbar sind."""
    host = (hostname or socket.gethostname()).lower()
    out: list[Source] = []
    if not device.owns_on(host):
        return out
    for spec in device.quellen:
        typ = (spec.typ or "auto").lower()
        try:
            if typ == "auto":
                new = _local_platform_sources()
            else:
                new = [_resolve(typ)(spec=spec)]
        except (SourceError, OSError) as e:
            log_error(f"Geraet {device.id}, Quelle {typ}: {e}")
            continue
        for src in new:
            if spec.bezeichnung and src.kind != "auto":
                src.label = spec.bezeichnung
            out.append(src)
    _dedup_keys(out)
    return out


def _dedup_keys(sources: list[Source]) -> None:
    """CSV-Spalten/Keys pro Geraet eindeutig machen."""
    seen: dict[str, int] = {}
    for s in sources:
        k = s.key
        if k in seen:
            seen[k] += 1
            s.key = f"{k}{seen[k]}"
        else:
            seen[k] = 1


def close_all(sources: list[Source]) -> None:
    for s in sources:
        try:
            s.close()
        except Exception as e:  # pragma: no cover
            log_error(f"close({s.key}) schief: {e}")
