"""Messzeilen anderer Rechner uebernehmen – per SSH, ohne eigenen Server.

Grundidee: jeder Rechner bleibt fuer seine eigenen Messungen zustaendig und
schreibt sie lokal. Der Sammler holt sich die Zeilen, indem er den *anderen*
Rechner per SSH seinen eigenen Export drucken laesst:

    ssh <host>  python3 -m powermeter --data <daten> export --month 2026-11

Angekommenes wird duplettenfrei (zeit, geraet, quelle) eingehaengt und die
Summen in state.json neu gebaut. Damit braucht es keinen offenen Port, keinen
Dienst auf beiden Seiten und keine Datenbank – nur SSH-Publickey, was auf einem
heimischen Netzwerk der unangreifbarste Weg ist.

Wer kein SSH will: `merge <ordner>` kann weiterhin Ordner (USB, Sync-Ordner,
Netzlaufwerk) uebernehmen, und `--befehl` akzeptiert jedes Kommando, das CSV auf
stdout schreibt (z. B. `pscp user@host:2026-11.csv` oder ein Umweg ueber
`sftp`/`scp` plus cat).
"""
from __future__ import annotations

import subprocess

from .config import load_devices
from .store import Row, append_rows, parse_rows, rebuild_state
from .util import log_error

SYNC_BEFEHL = "{python} -m powermeter {daten}export{monat}"


def remote_command(eintrag: dict, month: str | None = None, seit: str | None = None,
                   bis: str | None = None) -> list[str]:
    """Bausteine heissen englisch (data/command/python/ssh); deutsche Alt-Namen gelten auch."""
    """Kommando bauen, das auf dem anderen Rechner CSV auf stdout schreibt."""
    if eintrag.get("command") or eintrag.get("befehl"):
        # Text (nicht Liste): laeuft durch die Shell, damit Pfade mit Leerzeichen funktionieren
        return eintrag.get("command") or eintrag["befehl"]
    ssh = list(eintrag.get("ssh") or ["ssh"])
    host = eintrag["host"]
    daten = eintrag.get("data") or eintrag.get("daten")
    python = eintrag.get("python") or "python3"
    teil = SYNC_BEFEHL.format(
        python=python,
        daten=f"--data {daten} " if daten else "",
        monat=f" --month {month}" if month else "",
    )
    if seit:
        teil += f" --since {seit}"
    if bis:
        teil += f" --until {bis}"
    return ssh + [host, teil]


def pull(paths, eintrag: dict, month: str | None = None, seit: str | None = None,
         command: list[str] | str | None = None, timeout: float = 300.0,
         eigene_ids: set[str] | None = None) -> dict:
    """Einmal einholen und einreihen. Gibt Zahlensatz zurueck (kein Werfen)."""
    cmd = command or remote_command(eintrag, month, seit)
    name = eintrag.get("name") or eintrag.get("host") or "fern"
    try:
        # Kommando als Text (z. B. --befehl) läuft durch die Shell; als Liste direkt.
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              shell=isinstance(cmd, str))
    except (OSError, subprocess.SubprocessError) as e:
        log_error(f"sync {name}: {e}")
        return {"rechner": name, "fehler": str(e), "neu": 0, "dupes": 0}
    if proc.returncode != 0:
        fehler = (proc.stderr or "").strip().splitlines()
        log_error(f"sync {name}: Ferndienst meldet {proc.returncode}: {fehler[:2]}")
        return {"rechner": name, "fehler": f"exit {proc.returncode}",
                "meldung": " · ".join(fehler[:2]), "neu": 0, "dupes": 0}
    zeilen = list(parse_rows(proc.stdout))
    # Geraete-Namen vergleichen: gleiche id auf zwei Rechnern waere mehrdeutig.
    # nur Geraete, die DIESER Rechner selbst misst, duernten kollidieren: ein Sammler
    # darf die Geraete anderer Rechner ruhig in seiner devices.json fhren.
    eigene = eigene_ids if eigene_ids is not None else set(load_devices(paths).ids())
    fremde = {r.geraet for r in zeilen}
    konflikt = sorted(eigene & fremde)
    neu, dupe = append_rows(paths, zeilen)
    if neu:
        rebuild_state(paths)
    letzte = None
    for zeile in proc.stdout.splitlines():
        feld = zeile.split(",")[0]
        if len(feld) >= 10 and feld[:4].isdigit() and (letzte is None or feld > letzte):
            letzte = feld
    out = {"rechner": name, "neu": neu, "dupes": dupe, "seit_zuletzt": letzte}
    if konflikt:
        out["konflikt"] = konflikt
        log_error(f"sync {name}: gleiche Geraete-Ids auf beiden Rechnern: {konflikt} "
                  f"– ids in devices.json trennen")
    return out


def sync_all(paths, eintraege: list[dict], marker: dict, month: str | None = None,
             command: list[str] | None = None, eigene_ids: set[str] | None = None) -> list[dict]:
    """Alle konfigurierten Rechner abholen; `marker` (state.json -> sync) steuert 'seit'."""
    ergebnisse = []
    for e in eintraege:
        name = e.get("name") or e.get("host")
        bisher = marker.get(name)
        erg = pull(paths, e, month=month, seit=None if month else bisher, command=command,
                   eigene_ids=eigene_ids)
        if erg.get("seit_zuletzt"):
            marker[name] = erg["seit_zuletzt"]
        ergebnisse.append(erg)
    return ergebnisse
