#!/usr/bin/env python3
"""Bildserie der Linux-Anzeige — reproduzierbar, ganzes Fenster, ohne Handarbeit.

Wayland erlaubt keinem Fenster, Position oder Größe zu erfahren, und das Portal kennt
nur den ganzen Bildschirm. Der Weg hier geht über die eigenen D-Bus-Aktionen der
Anzeige (`bereich`, `detail`, `zurueck`, `marke`, `groesse`): erst die Ansicht umschalten,
dann die eigene Magenta-Marke den Fensterrahmen vermessen lassen, dann das saubere Bild
zuschneiden. Kein Raten über Desktopfarben, kein Knopf-Klicken, kein manuell zugeschnittener
Ausschnitt.

    tools/screenshot_linux.py --zurueck --bereich h --aus docs/images/x.png
    tools/screenshot_linux.py --detail pc --aus docs/images/y.png
    tools/screenshot_linux.py --komponiere a.png b.png --aus docs/images/serie.png
"""
from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

import gi

try:
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    gi.require_version("Gio", "2.0")
    gi.require_version("GLib", "2.0")
    gi.require_version("GdkPixbuf", "2.0")
except ValueError:
    sys.exit("GTK4/Adwaita/GdkPixbuf fehlt — PyGObject installieren")
from gi.repository import GLib, Gio, GdkPixbuf  # noqa: E402

APP_ID = "de.powermeter.Widget"
OBJEKT = "/de/powermeter/Widget"
AKTIONEN = "org.gtk.Actions"   # GTK meldet Fenster-Aktionen auf diesem Interface
GRUND = 0x14181FFF


def aktion(name: str, parameter: str | None = None) -> None:
    """Aktion der laufenden Anzeige auslösen — wie ein Klick, aber nachvollziehbar.

    Die Anzeige läuft als eigene Anwendung und bietet ihre Aktionen auf dem Sitzungsbus an
    (org.gtk.Actions am Objektpfad des Fensters), deshalb führt dieses Werkzeug sie von
    außen aus, statt Knöpfe auf dem Bildschirm zu raten.
    """
    verbindung = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    variante = GLib.Variant("(sava{sv})",
                            (name, [GLib.Variant("s", parameter)] if parameter else [], {}))
    # Der Busname ist die App-ID der laufenden Anzeige, nicht ein GTK-Primitive.
    verbindung.call_sync(APP_ID, OBJEKT, AKTIONEN, "Activate",
                         variante, None, Gio.DBusCallFlags.NONE, 2000, None)


def bildschirm(aus: str) -> str:
    """Ganzen Bildschirm per XDG-Portal aufnehmen — ohne Root, ohne GNOME-Shell-Eval.

    Das Portal antwortet nicht mit der Datei, sondern mit einem Request-Pfad; die URI kommt
    über das `Response`-Signal desselben Requests. Also: Signal abonnieren, anfragen,
    Loop laufen lassen. (GNOME liefert hier grundsätzlich den ganzen Bildschirm — einzelne
    Fenster kann das Portal nicht, und Wayland verrät einem Fenster weder Position noch
    Größe.)
    """
    verbindung = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    schleife = GLib.MainLoop()
    ergebnis: dict[str, object] = {}

    def auf_response(_verbindung, _absender, _pfad, schnittstelle, signal, parameter, _daten):
        if signal != "Response" or schnittstelle != "org.freedesktop.portal.Request":
            return
        code, optionen = parameter.unpack()
        ergebnis["code"] = code
        ergebnis["uri"] = optionen.unpack().get("uri") if hasattr(optionen, "unpack") \
            else optionen.get("uri")
        schleife.quit()

    verbindung.signal_subscribe(None, "org.freedesktop.portal.Request", "Response", None,
                                None, Gio.DBusSignalFlags.NONE, auf_response, None)
    antwort = verbindung.call_sync(
        "org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop",
        "org.freedesktop.portal.Screenshot", "Screenshot",
        GLib.Variant("(sa{sv})", ("", {"interactive": GLib.Variant("b", False)})),
        None, Gio.DBusCallFlags.NONE, 30000, None)
    del antwort
    GLib.timeout_add_seconds(25, schleife.quit)
    schleife.run()
    uri = ergebnis.get("uri")
    if not uri:
        sys.exit(f"Portal lieferte keine Datei (code {ergebnis.get('code')})")
    quelle = uri[7:] if uri.startswith("file://") else uri
    # Das Portal legt die Aufnahme im Benutzer-Verzeichnis ab (anderes Dateisystem als
    # /tmp) — umbenennen schlägt dort fehl, also verschieben.
    shutil.move(quelle, aus)
    return aus


def _magenta(pixel, schrittweite: int, kanale: int, x: int, y: int) -> bool:
    o = y * schrittweite + x * kanale
    return pixel[o] > 190 and pixel[o + 1] < 90 and pixel[o + 2] > 190


def _lauf(frage, von: int, bis: int) -> tuple[int, int]:
    """Längster zusammenhängender Abschnitt, in dem frage(i) ja sagt."""
    bester_start, bester_len, start = 0, 0, None
    i = von
    while i < bis:
        if frage(i):
            if start is None:
                start = i
        else:
            if start is not None and i - start > bester_len:
                bester_start, bester_len = start, i - start
            start = None
        i += 2
    if start is not None and bis - start > bester_len:
        bester_start, bester_len = start, bis - start
    return bester_start, bester_len


def magenta_rechteck(pfad: str) -> tuple[int, int, int, int]:
    """Rahmen der Marke (`marke an`) — Kanten dort gemessen, wo sie lang sind.

    Die Marke ist ein 6 Pixel breiter Ring. Senkrechte Kanten sind so lang wie das Fenster
    hoch, waagerechte so lang wie es breit; nur dort sind sie meßbar. Alles andere (alle
    Magenta-Pixel vereinigen, oder die Breite an den waagerechten Kanten ablesen) zieht
    Nachbarnfenster herein oder schneidet wegen der abgerundeten Ecken Werte ab — gemessen
    622 statt 430 Pixel breit bzw. 422 statt 430.
    """
    pb = GdkPixbuf.Pixbuf.new_from_file(pfad)
    kanale, schrittweite = pb.get_n_channels(), pb.get_rowstride()
    pixel = pb.get_pixels()
    breite, hoehe = pb.get_width(), pb.get_height()

    def frage_x(x: int, y: int) -> bool:
        return _magenta(pixel, schrittweite, kanale, x, y)

    senkrechte = {}
    for x in range(0, breite, 2):
        start, len_ = _lauf(lambda y, xx=x: frage_x(xx, y), 0, hoehe)
        if len_ >= 120:
            senkrechte[x] = (start, len_)
    wagerechte = {}
    for y in range(0, hoehe, 2):
        start, len_ = _lauf(lambda x, yy=y: frage_x(x, yy), 0, breite)
        if len_ >= 120:
            wagerechte[y] = (start, len_)
    if not senkrechte or not wagerechte:
        sys.exit("keine geschlossene Magenta-Markierung — laeuft `powermeter widget`?")

    laengste = max(len_ for _, len_ in senkrechte.values())
    kanten_x = [x for x, (_, len_) in senkrechte.items() if len_ >= 0.5 * laengste]
    laengster = max(len_ for _, len_ in wagerechte.values())
    kanten_y = [y for y, (_, len_) in wagerechte.items() if len_ >= 0.5 * laengster]
    if len(kanten_x) < 2 or len(kanten_y) < 2:
        sys.exit("Markierung ohne zwei Kanten — Fenster verdeckt oder zu klein?")
    x0, x1 = min(kanten_x), max(kanten_x) + 6            # +6: Ringdicte
    y0, y1 = min(kanten_y), max(kanten_y) + 6
    return x0, y0, x1 - x0, y1 - y0


def magenta_rest(pfad: str, rechteck: tuple[int, int, int, int]) -> float:
    """Anteil Magenta im Bild — nach `marke aus` muß der near null sein."""
    pb = GdkPixbuf.Pixbuf.new_from_file(pfad)
    kanale, schrittweite = pb.get_n_channels(), pb.get_rowstride()
    pixel = pb.get_pixels()
    x, y, breite, hoehe = rechteck
    treffer = gesamt = 0
    for yy in range(y, min(pb.get_height(), y + hoehe), 7):
        for xx in range(x, min(pb.get_width(), x + breite), 7):
            gesamt += 1
            if _magenta(pixel, schrittweite, kanale, xx, yy):
                treffer += 1
    return treffer / max(1, gesamt)


def aufnahme(aus: str, rand: int, warten: float) -> None:
    """Marke zweimal messen (das Fenster mißt sich nach einem Umschalten neu), dann sauber."""
    aktion("marke", "an")
    time.sleep(0.4)
    rechteck = None
    for _ in range(5):
        bildschirm("/tmp/powermeter-marke.png")
        neu = magenta_rechteck("/tmp/powermeter-marke.png")
        if rechteck and all(abs(a - b) <= 2 for a, b in zip(rechteck, neu)):
            rechteck = neu
            break
        rechteck = neu
        time.sleep(warten / 3)
    x, y, breite, hoehe = (rechteck[0] + rand, rechteck[1] + rand,
                           rechteck[2] - 2 * rand, rechteck[3] - 2 * rand)
    quelle = "/tmp/powermeter-vollbild.png"
    aktion("marke", "aus")
    for versuch in range(3):
        time.sleep(0.5 + 0.5 * versuch)
        bildschirm(quelle)
        rest = magenta_rest(quelle, rechteck)
        if rest < 0.002:
            break
        print(f"Nachzieh-Versuch {versuch + 1}: noch {rest * 100:.2f} % Markierung im Bild")
    GdkPixbuf.Pixbuf.new_from_file(quelle).new_subpixbuf(
        x, y, max(64, breite), max(64, hoehe)).savev(aus, "png", [], [])
    print(f"{aus}: Fenster {breite}×{hoehe} bei ({x}|{y})")


def ansicht(bereich: str | None, detail: str | None, zurueck: bool, warten: float,
            groesse: str | None) -> None:
    """Ansicht umschalten — die Anzeige tut das selbst, über ihre eigenen Aktionen."""
    if zurueck:
        aktion("zurueck")
    if bereich:
        aktion("bereich", bereich)
    if detail:
        aktion("detail", detail)
    if bereich or detail or zurueck:
        aktion("aktualisieren")
        time.sleep(warten)
    if groesse:
        # Größe erst NACH dem Umschalten pinsen: die neue Seite verlangt sonst ihr eigenes
        # Maß und überstimmt ein vorher gesetztes.
        for _ in range(3):
            aktion("groesse", groesse)
            time.sleep(0.4)


def komponiere(bilder: list[str], aus: str, abstand: int, hoehe: int) -> None:
    """Mehrere Aufnahmen als ein Bild: gleiche Höhe, dunkler Grund, Abstand.

    Die Anzeige passt ihre Größe dem Inhalt an, also werden die Teile auf eine gemeinsame
    Höhe skaliert — sonst steht eine Reihe unterschiedlich großer Fenster nebeneinander.
    """
    teile = []
    ziel = hoehe or max(GdkPixbuf.Pixbuf.new_from_file(p).get_height() for p in bilder)
    ziel = max(120, min(ziel, 900))
    for pfad in bilder:
        pb = GdkPixbuf.Pixbuf.new_from_file(pfad)
        if pb.get_height() != ziel:
            pb = pb.scale_simple(max(1, round(pb.get_width() * ziel / pb.get_height())),
                                 ziel, GdkPixbuf.InterpType.BILINEAR)
        teile.append(pb)
    # Ein Abstand mehr als Teile: links und rechts Rahmen, sonst rutscht das letzte Teil
    # vom Grund herunter (gemessen: Série 826 Pixel breit, zweites Bild abgeschnitten).
    breite = sum(pb.get_width() for pb in teile) + abstand * (len(teile) + 1)
    grund = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8,
                                 breite, ziel + 2 * abstand)
    grund.fill(GRUND)
    x = abstand
    for pb in teile:
        # composite verschiebt den *Quell*-Pixbuf auf das Ziel — grund ist das Ziel.
        pb.composite(grund, x, abstand, pb.get_width(), pb.get_height(),
                     x, abstand, 1.0, 1.0, GdkPixbuf.InterpType.NEAREST, 255)
        x += pb.get_width() + abstand
    grund.savev(aus, "png", [], [])
    print(f"{aus}: Série aus {len(teile)} Bildern, Höhe {ziel}")


def main() -> int:
    p = argparse.ArgumentParser(description="Bildserie der powermeter-Linux-Anzeige")
    p.add_argument("--aus", required=True, help="Ziel-PNG")
    p.add_argument("--bereich", choices=("h", "tag", "woche"),
                   help="Bereich der Übersichtsseite umschalten")
    p.add_argument("--detail", metavar="GERÄT", help="Detailseite dieses Geräts öffnen")
    p.add_argument("--zurueck", action="store_true", help="zurück zur Übersichtsseite")
    p.add_argument("--groesse", metavar="BxH", help="Fenstergröße pinsen (BxH)")
    p.add_argument("--warten", type=float, default=1.6, help="Sekunden nach dem Umschalten")
    p.add_argument("--rand", type=int, default=8, help="Pixel am Rand abschneiden")
    p.add_argument("--komponiere", nargs="+", metavar="PNG",
                   help="fertige PNGs zu einem Bild reihen (nicht aufnehmen)")
    p.add_argument("--hoehe", type=int, default=0,
                   help="gemeinsame Höhe der Reihe (0 = höchste Teilaufnahme)")
    p.add_argument("--abstand", type=int, default=16)
    args = p.parse_args()

    if args.komponiere:
        komponiere(args.komponiere, args.aus, args.abstand, args.hoehe)
        return 0
    ansicht(args.bereich, args.detail, args.zurueck, args.warten, args.groesse)
    aufnahme(args.aus, args.rand, args.warten)
    return 0


if __name__ == "__main__":
    sys.exit(main())
