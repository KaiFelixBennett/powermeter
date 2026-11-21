"""Autostart auf Windows und Linux.

Windows: Run-Schluessel im HKCU (wie im alten Tool), startet das Widget mit pythonw.
Linux: XDG-Autostart (.desktop) fuer das Widget, dazu optional ein systemd-User-Dienst
fuer den Logger im Hintergrund (`install-service`). Beide Wege schreiben nur im
eigenen Konto, kein sudo noetig.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from .util import IS_WINDOWS, log_error

APP = "powermeter"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
LAUNCHER = "powermeter-widget.pyw"


def launcher_path() -> Path:
    """Das Startskript neben dem Paket (Checkout-Wurzel)."""
    return Path(__file__).resolve().parent.parent / LAUNCHER


def pythonw() -> Path:
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    return w if w.exists() else exe


def autostart_get() -> bool:
    if IS_WINDOWS:  # pragma: no cover - Windows-registrierung
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
                winreg.QueryValueEx(k, APP)
                return True
        except OSError:
            return False
    return (Path.home() / ".config" / "autostart" / f"{APP}.desktop").exists()


def autostart_set(on: bool, headless: bool = False) -> None:
    if IS_WINDOWS:  # pragma: no cover - Windows-registrierung
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if on:
                exe = pythonw() if not headless else Path(sys.executable)
                target = f'"{exe}" "{launcher_path()}"' + (" run --headless" if headless else "")
                winreg.SetValueEx(k, APP, 0, winreg.REG_SZ, target)
            else:
                try:
                    winreg.DeleteValue(k, APP)
                except FileNotFoundError:
                    pass
        return
    desk = Path.home() / ".config" / "autostart" / f"{APP}.desktop"
    if not on:
        try:
            desk.unlink()
        except FileNotFoundError:
            pass
        return
    desk.parent.mkdir(parents=True, exist_ok=True)
    mode = "run --headless" if headless else "run"
    desk.write_text(
        "[Desktop Entry]\n"
        f"Type=Application\nName={APP}\nComment=Strommessung und Kosten\n"
        f"Exec={sys.executable} {launcher_path()} {mode}\nTerminal=false\n"
        "X-GNOME-Autostart-enabled=true\n", encoding="utf-8")


# ------------------------------------------------------- systemd (Linux, User)
UNIT = "powermeter.service"


def unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / UNIT


def service_install(start: bool = True) -> str:
    if IS_WINDOWS:
        return "systemd gibt es unter Windows nicht – bitte Autostart nutzen."
    p = unit_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "[Unit]\nDescription=powermeter – Strommessung und Kosten\n"
        "After=network-online.target\nWants=network-online.target\n\n"
        "[Service]\nType=simple\n"
        f"ExecStart={sys.executable} {launcher_path()} run --headless\n"
        "Restart=always\nRestartSec=5\n\n[Install]\nWantedBy=default.target\n", encoding="utf-8")
    try:
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False, timeout=30)
        if start:
            subprocess.run(["systemctl", "--user", "enable", "--now", UNIT], check=False, timeout=60)
        return f"dienst installiert und gestartet: {p}"
    except (OSError, subprocess.SubprocessError) as e:
        log_error(f"systemctl: {e}")
        return f"Unit-Datei geschrieben: {p} (systemctl-Aufruf fehlgeschlagen: {e})"


def service_remove() -> str:
    if IS_WINDOWS:
        return "kein systemd unter Windows"
    try:
        subprocess.run(["systemctl", "--user", "disable", "--now", UNIT], check=False, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        log_error(f"systemctl disable: {e}")
    try:
        unit_path().unlink()
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False, timeout=30)
        return "dienst entfernt"
    except FileNotFoundError:
        return "kein Dienst installiert"
