"""Tests fuer das Bildserien-Werkzeug (tools/screenshot_linux.py).

Ohne Bildschirmaufnahme: die Rahmenvermessung ist der Teil, der real falsch lag —
Nachbarnfenster wurden eingezogen (622 statt 430 Pixel) und wegen der abgerundeten Ecken
wurde die Zahlenspalte abgeschnitten (422 statt 430). Beides ist hier nachbaubar, weil die
Marke ein einfaches Muster ist. Der Rest des Werkzeugs (Portal, D-Bus) braucht einen
laufenden Desktop und wird nicht getestet.
"""
from importlib import util
from pathlib import Path

import pytest

gi = pytest.importorskip("gi")
try:
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf
except (ImportError, ValueError):        # pragma: no cover - Runner ohne GI
    pytest.skip("GdkPixbuf fehlt", allow_module_level=True)

WERKZEUG = Path(__file__).resolve().parent.parent / "tools" / "screenshot_linux.py"


def _laden():
    spec = util.spec_from_file_location("screenshot_linux", WERKZEUG)
    modul = util.module_from_spec(spec)
    spec.loader.exec_module(modul)
    return modul


def _marke(breite: int = 430, hoehe: int = 760, rand_x: int = 300, rand_y: int = 120):
    """Bildschirm-Attrappe mit einem Magenta-Ring in bekannter Lage."""
    grund = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, 1200, 1400)
    grund.fill(0x101418FF)
    d = 6
    grund.new_subpixbuf(rand_x, rand_y, breite, d).fill(0xFF00FFFF)                 # oben
    grund.new_subpixbuf(rand_x, rand_y + hoehe - d, breite, d).fill(0xFF00FFFF)     # unten
    grund.new_subpixbuf(rand_x, rand_y, d, hoehe).fill(0xFF00FFFF)                  # links
    grund.new_subpixbuf(rand_x + breite - d, rand_y, d, hoehe).fill(0xFF00FFFF)     # rechts
    return grund


def test_Rahmen_findet_das_Fenster_und_nicht_die_Nachbarn(tmp_path):
    werkzeug = _laden()
    grund = _marke()
    # Fremdfenster mit denselben Magenta-Tönen, klein und versprengt: wer alle Pixel
    # vereinigt, bläht den Rahmen auf.
    grund.new_subpixbuf(100, 200, 8, 8).fill(0xFF00FFFF)
    grund.new_subpixbuf(1100, 900, 10, 10).fill(0xFF00FFFF)
    pfad = str(tmp_path / "marke.png")
    grund.savev(pfad, "png", [], [])
    x, y, breite, hoehe = werkzeug.magenta_rechteck(pfad)
    assert abs(x - 300) <= 4 and abs(y - 120) <= 4
    assert abs(breite - 430) <= 6 and abs(hoehe - 760) <= 6


def test_Rahmen_schneidet_keine_Kante_ab(tmp_path):
    """Breite von den senkrechten Kanten: an den waagerechten fehlen die Ecken."""
    werkzeug = _laden()
    grund = _marke(breite=520, hoehe=700, rand_x=200, rand_y=200)
    pfad = str(tmp_path / "marke2.png")
    grund.savev(pfad, "png", [], [])
    _, _, breite, hoehe = werkzeug.magenta_rechteck(pfad)
    assert breite >= 520 and hoehe >= 700


def test_laeuft_ueber_den_ganzen_Bildschirm(tmp_path):
    werkzeug = _laden()
    assert werkzeug._lauf(lambda i: 200 <= i < 400, 0, 1000) == (200, 200)
    assert werkzeug._lauf(lambda i: False, 0, 100) == (0, 0)
