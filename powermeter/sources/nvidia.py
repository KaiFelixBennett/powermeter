"""NVIDIA-Leistung ueber NVML – auf Windows und Linux gleich, nur der Bibliotheksname
unterscheidet sich. keine Zusatzpakete, ctypes reicht. Schluessel wie im alten Format: 'nvidia'.
"""
from __future__ import annotations

import ctypes as C

from ..util import IS_WINDOWS, short_name
from .base import Source, SourceError

LIBS = ("nvml.dll",) if IS_WINDOWS else ("libnvidia-ml.so.1", "libnvidia-ml.so")


def _load(explicit: str | None = None):
    names = (explicit,) if explicit else LIBS
    last = None
    for name in names:
        if not name:
            continue
        try:
            return C.CDLL(name)
        except OSError as e:
            last = e
    raise SourceError(f"NVML-Bibliothek nicht gefunden ({', '.join(n for n in names if n)}): {last}")


class NvidiaGpus(Source):
    kind, key = "nvml", "nvidia"

    def __init__(self, spec=None):
        nv = self.nv = _load(getattr(spec, "value", {}).get("bibliothek") if spec else None)
        nv.nvmlInit_v2.argtypes = []
        nv.nvmlInit_v2.restype = C.c_int
        nv.nvmlDeviceGetCount_v2.argtypes = [C.POINTER(C.c_uint)]
        nv.nvmlDeviceGetHandleByIndex_v2.argtypes = [C.c_uint, C.POINTER(C.c_void_p)]
        nv.nvmlDeviceGetName.argtypes = [C.c_void_p, C.c_char_p, C.c_uint]
        nv.nvmlDeviceGetPowerUsage.argtypes = [C.c_void_p, C.POINTER(C.c_uint)]
        if nv.nvmlInit_v2():
            raise SourceError("NVML-Initialisierung fehlgeschlagen (Treiber laeuft?)")
        count, self.gpus, names = C.c_uint(), [], []
        nv.nvmlDeviceGetCount_v2(C.byref(count))
        for i in range(count.value):
            handle, buf = C.c_void_p(), C.create_string_buffer(96)
            if nv.nvmlDeviceGetHandleByIndex_v2(i, C.byref(handle)) == 0:
                nv.nvmlDeviceGetName(handle, buf, len(buf))
                self.gpus.append(handle)
                names.append(short_name(buf.value.decode(errors="replace")))
        if not self.gpus:
            self.close()
            raise SourceError("keine NVIDIA-GPU gefunden")
        self.label = " + ".join(names)

    def read(self):
        total, mw = 0.0, C.c_uint()
        for handle in self.gpus:
            if self.nv.nvmlDeviceGetPowerUsage(handle, C.byref(mw)):
                return None
            total += mw.value / 1000  # mW -> W
        return total

    def close(self):
        try:
            self.nv.nvmlShutdown()
        except Exception:
            pass
