"""Grundlage aller Messquellen.

Eine Quelle liefert Mittelwert-Leistung in Watt oder None, wenn sie in diesem
Durchgang nichts sagen kann. Alles Weitere (Effizienz, Restanteile, Preis) wird
erst bei der Ausrechnung angewendet – dadurch wirken Kalibrierwert-Aenderungen
rueckwirkend auf alle Summen (wie schon im alten Einzeldatei-Tool).
"""
from __future__ import annotations

class SourceError(Exception):
    """Quelle ist auf diesem Rechner/Netz nicht verfuegbar."""


class Source:
    kind = "?"
    #: stabil im CSV – ändere ihn nicht ohne Datenmigration
    key: str = "?"
    #: Anzeigename, darf sich aus der Hardware ergeben ("RTX 5080")
    label: str = ""

    def __init__(self, key: str | None = None, label: str | None = None, spec=None):
        self.key = key or self.kind
        self.label = label or self.kind
        self.spec = spec

    def read(self) -> float | None:  # pragma: no cover - abstrakt
        raise NotImplementedError

    def close(self) -> None:
        pass

    # --- Selbsttest fuer `powermeter scan` -----------------------------------
    def probe(self) -> tuple[bool, str]:
        try:
            v = self.read()
        except Exception as e:
            return False, f"Fehler: {e}"
        if v is None:
            return False, "liefert keinen Wert"
        return True, f"{v:.1f} W"
