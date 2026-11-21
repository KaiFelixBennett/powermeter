"""Windows-Quellen: CPU-Leistung ueber den Leistungsindikator 'Energy Meter' (PDH/RAPL)
und AMD-GPU-Leistung ueber ADLX (amdadlx64.dll, COM-artige vtable).

Beides ohne Admin-Rechte und ohne Zusatzpakete (nur ctypes). Schluessel wie im
alten Datenformat: 'cpu' und 'amd'.
"""
from __future__ import annotations

import ctypes as C
import functools

from ..util import IS_WINDOWS, short_name
from .base import Source, SourceError

if not IS_WINDOWS:
    raise ImportError("powermeter.sources.win ist nur unter Windows importierbar")

from ctypes import wintypes as W  # noqa: E402


class _PdhValue(C.Structure):
    _fields_ = [("status", W.DWORD), ("value", C.c_double)]


class WinCpuRapl(Source):
    """Intel/AMD-Paketleistung ueber die PDH-Zaehlergruppe 'Energy Meter'."""

    kind, key, label = "rapl_win", "cpu", "CPU"

    def __init__(self, counter: str = r"\Energy Meter(RAPL_Package0_PKG)\Power", spec=None):
        if not IS_WINDOWS:
            raise SourceError("PDH gibt es nur unter Windows")
        self.counter = counter
        pdh = self.pdh = C.WinDLL("pdh")
        pdh.PdhOpenQueryW.argtypes = [C.c_wchar_p, C.c_size_t, C.POINTER(C.c_void_p)]
        pdh.PdhAddEnglishCounterW.argtypes = [C.c_void_p, C.c_wchar_p, C.c_size_t, C.POINTER(C.c_void_p)]
        pdh.PdhCollectQueryData.argtypes = [C.c_void_p]
        pdh.PdhGetFormattedCounterValue.argtypes = [C.c_void_p, W.DWORD, C.c_void_p, C.POINTER(_PdhValue)]
        pdh.PdhCloseQuery.argtypes = [C.c_void_p]
        self.query, self.ctr = C.c_void_p(), C.c_void_p()
        if pdh.PdhOpenQueryW(None, 0, C.byref(self.query)):
            raise SourceError("PDH nicht verfuegbar")
        if pdh.PdhAddEnglishCounterW(self.query, counter, 0, C.byref(self.ctr)):
            self.close()
            raise SourceError("Leistungsindikator 'Energy Meter' (RAPL) fehlt")
        pdh.PdhCollectQueryData(self.query)

    def read(self):
        v = _PdhValue()
        if self.pdh.PdhCollectQueryData(self.query) or self.pdh.PdhGetFormattedCounterValue(
                self.ctr, 0x200, None, C.byref(v)):  # PDH_FMT_DOUBLE
            return None
        return v.value / 1000  # mW -> W

    def close(self):
        try:
            self.pdh.PdhCloseQuery(self.query)
        except Exception:
            pass


# ------------------------------------------------------------------ ADLX (AMD)
@functools.lru_cache(maxsize=None)
def _proto(restype, *argtypes):
    return C.WINFUNCTYPE(restype, C.c_void_p, *argtypes)


def _vcall(obj, index, argtypes=(), args=(), restype=C.c_int):
    """Methode Nr. `index` aus der C-vtable eines ADLX-Objekts aufrufen."""
    vtbl = C.cast(obj, C.POINTER(C.POINTER(C.c_void_p)))[0]
    return _proto(restype, *argtypes)(vtbl[index])(obj, *args)


_PP, _PD = C.POINTER(C.c_void_p), C.POINTER(C.c_double)


class WinAmdGpus(Source):
    kind, key = "adlx", "amd"
    # vtable-Indizes laut ADLX-SDK-Headern (ISystem.h, IPerformanceMonitoring.h)
    RELEASE = 1
    SYS_GET_GPUS, SYS_GET_PERF = 1, 9
    LIST_SIZE, LIST_AT_GPU = 3, 11
    GPU_NAME = 7
    PERF_CURRENT_GPU_METRICS = 18
    MET_GPU_POWER, MET_BOARD_POWER = 9, 10

    def __init__(self, spec=None):
        if not IS_WINDOWS:
            raise SourceError("ADLX (amdadlx64.dll) gibt es nur unter Windows")
        self.dll = C.CDLL("amdadlx64.dll")
        dll = self.dll
        dll.ADLXQueryFullVersion.argtypes = [C.POINTER(C.c_uint64)]
        dll.ADLXInitialize.argtypes = [C.c_uint64, _PP]
        ver, system = C.c_uint64(), C.c_void_p()
        if dll.ADLXQueryFullVersion(C.byref(ver)) or dll.ADLXInitialize(ver.value, C.byref(system)):
            raise SourceError("ADLX-Initialisierung fehlgeschlagen")
        self.perf, self.gpus, names = C.c_void_p(), [], []
        try:
            gpu_list = C.c_void_p()
            if _vcall(system, self.SYS_GET_PERF, (_PP,), (C.byref(self.perf),)) or \
                    _vcall(system, self.SYS_GET_GPUS, (_PP,), (C.byref(gpu_list),)):
                raise SourceError("ADLX liefert keine Leistungsdaten")
            for i in range(_vcall(gpu_list, self.LIST_SIZE, restype=C.c_uint32)):
                gpu, name = C.c_void_p(), C.c_char_p()
                if _vcall(gpu_list, self.LIST_AT_GPU, (C.c_uint32, _PP), (i, C.byref(gpu))) == 0:
                    _vcall(gpu, self.GPU_NAME, (C.POINTER(C.c_char_p),), (C.byref(name),))
                    self.gpus.append(gpu)
                    names.append(short_name((name.value or b"AMD").decode(errors="replace")))
            _vcall(gpu_list, self.RELEASE, restype=C.c_long)
            if not self.gpus:
                raise SourceError("keine AMD-GPU gefunden")
        except Exception:
            self.close()
            raise
        self.label = " + ".join(names)

    def read(self):
        total = 0.0
        for gpu in self.gpus:
            metrics, watts = C.c_void_p(), C.c_double()
            if _vcall(self.perf, self.PERF_CURRENT_GPU_METRICS, (C.c_void_p, _PP),
                      (gpu, C.byref(metrics))):
                return None
            try:
                if _vcall(metrics, self.MET_BOARD_POWER, (_PD,), (C.byref(watts),)) and \
                        _vcall(metrics, self.MET_GPU_POWER, (_PD,), (C.byref(watts),)):
                    return None
            finally:
                _vcall(metrics, self.RELEASE, restype=C.c_long)
            total += watts.value
        return total

    def close(self):
        for gpu in getattr(self, "gpus", []):
            _vcall(gpu, self.RELEASE, restype=C.c_long)
        self.gpus = []
        if getattr(self, "perf", None) and self.perf.value:
            _vcall(self.perf, self.RELEASE, restype=C.c_long)
            self.perf = C.c_void_p()
        try:
            self.dll.ADLXTerminate()
        except Exception:
            pass
