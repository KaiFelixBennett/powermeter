"""Hilfen, die alle Module brauchen: Pfade, Log, Zahlformat, Einzelinstanz.

Bewusst nur Standardbibliothek, damit das Tool auf jeder Linux-Distribution und
auf Windows ohne virtuelle Umgebung laeuft (`python3 -m powermeter`).
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import time
from pathlib import Path

IS_WINDOWS = sys.platform.startswith("win")
IS_LINUX = sys.platform.startswith("linux")

_DATA_DIR: Path | None = None


def set_data_dir(path: Path) -> None:
    """Wird von config.init_paths() gesetzt; log_error() schreibt dann dorthin."""
    global _DATA_DIR
    _DATA_DIR = Path(path)


def data_dir() -> Path:
    return _DATA_DIR or Path.cwd() / "data"


def log_error(msg: str) -> None:
    """Fehlerprotokoll, damit auch ein Widget ohne Konsole etwas hinterlaesst."""
    try:
        d = data_dir()
        d.mkdir(parents=True, exist_ok=True)
        err = d / "error.log"
        if err.exists() and err.stat().st_size > 200_000:
            err.unlink()
        with err.open("a", encoding="utf-8") as f:
            f.write(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")
    except OSError:
        pass


def nf(x: float, nd: int = 0) -> str:
    """Format a number for display: 1,234.56 (grouped, dot as decimal mark)."""
    return f"{x:,.{nd}f}"


def short_name(name: str) -> str:
    """Grafikbezeichnung von Ballast befreien: 'NVIDIA GeForce RTX 5080' -> 'RTX 5080'."""
    skip = {"NVIDIA", "GEFORCE", "AMD", "RADEON", "AI", "PRO", "GRAPHICS"}
    return " ".join(p for p in name.split() if p.upper() not in skip) or name


def system_start() -> float:
    """Startzeit des Systems bzw (Windows) der Anmeldung, als Unix-Zeit.

    Windows nutzt die Anmeldezeit der Sitzung (robust gegen Schnellstart/Hybrid-Ruhezustand),
    sonst die Kernel-Startzeit; Linux liest /proc/uptime. Nur fuer die Fusszeile im Widget.
    """
    if IS_WINDOWS:  # pragma: no cover - Windows-spezifisch
        try:
            import ctypes as C
            from ctypes import wintypes as W

            class WTSINFOW(C.Structure):
                _fields_ = [("State", C.c_int), ("SessionId", W.DWORD), ("_counters", W.DWORD * 6),
                            ("WinStationName", C.c_wchar * 32), ("Domain", C.c_wchar * 17),
                            ("UserName", C.c_wchar * 21), ("ConnectTime", C.c_longlong),
                            ("DisconnectTime", C.c_longlong), ("LastInputTime", C.c_longlong),
                            ("LogonTime", C.c_longlong), ("CurrentTime", C.c_longlong)]

            wts = C.WinDLL("wtsapi32")
            wts.WTSQuerySessionInformationW.argtypes = [C.c_void_p, W.DWORD, C.c_int,
                                                        C.POINTER(C.c_void_p), C.POINTER(W.DWORD)]
            wts.WTSFreeMemory.argtypes = [C.c_void_p]
            buf, size = C.c_void_p(), W.DWORD()
            if wts.WTSQuerySessionInformationW(None, 0xFFFFFFFF, 24, C.byref(buf), C.byref(size)):
                logon = C.cast(buf, C.POINTER(WTSINFOW)).contents.LogonTime
                wts.WTSFreeMemory(buf)
                if logon > 0:
                    return (logon - 116444736000000000) / 1e7  # FILETIME -> Unix-Zeit
        except Exception as e:
            log_error(f"Anmeldezeit nicht lesbar: {e}")
        try:
            import ctypes as C

            k32 = C.WinDLL("kernel32")
            k32.GetTickCount64.restype = C.c_ulonglong
            return time.time() - k32.GetTickCount64() / 1000
        except Exception:
            return time.time()
    try:
        return time.time() - float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return time.time()


class SingleInstance:
    """Verhindert zwei laufende Instanzen desselben Namen.

    Windows: benannter Kernel-Mutex (wie im alten Einzeldatei-Tool).
    Linux/macOS: lockfile + flock. Rueckgabe None heisst 'laeuft schon'.
    """

    def __init__(self, name: str):
        self.name, self._handle, self._fh = name, None, None

    def acquire(self) -> bool:
        if IS_WINDOWS:
            import ctypes as C
            from ctypes import wintypes as W

            try:
                k32 = C.WinDLL("kernel32", use_last_error=True)
                k32.CreateMutexW.argtypes = [C.c_void_p, W.BOOL, C.c_wchar_p]
                k32.CreateMutexW.restype = C.c_void_p
                self._handle = k32.CreateMutexW(None, False, f"Local\\{self.name}")
                return C.get_last_error() != 183  # ERROR_ALREADY_EXISTS
            except Exception as e:  # pragma: no cover - Windows-exotisch
                log_error(f"Mutex nicht moeglich: {e}")
                return True
        try:
            import fcntl

            d = data_dir()
            d.mkdir(parents=True, exist_ok=True)
            self._fh = open(d / f"{self.name}.lock", "w")
            fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._fh.write(str(os.getpid()))
            self._fh.flush()
            return True
        except OSError as e:
            if e.errno in (11, 13, 9):  # EWOULDBLOCK/EACCES/EBADF -> belegt oder unbrauchbar
                return False
            log_error(f"Lock nicht moeglich: {e}")
            return True

    def release(self) -> None:
        if self._fh is not None:
            try:
                import fcntl

                fcntl.flock(self._fh, fcntl.LOCK_UN)
            except Exception:
                pass
            self._fh.close()
            self._fh = None
        if IS_WINDOWS and self._handle:  # pragma: no cover - Windows-exotisch
            try:
                import ctypes as C

                C.WinDLL("kernel32").CloseHandle(C.c_void_p(self._handle))
            except Exception:
                pass
            self._handle = None
