"""Doppelklick-Start unter Windows (laeuft mit pythonw, ohne Konsole).

Zeile fuer Zeile absichtlich knapp: sie setzt nur den Importpfad auf den Checkout
und ruft dann dieselbe Kommandozeile wie `powermeter run`.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from powermeter.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or ["run"]))
