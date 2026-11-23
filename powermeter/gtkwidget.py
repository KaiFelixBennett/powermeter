"""Desktop-Anzeige fuer Linux (GTK4/Libadwaita) – ohne Tk, weil Tk hier fehlt.

Anzeige und Sammler sind getrennt: der Sammler (`powermeter run --headless`)
misst und schreibt, dieses Fenster liest nur (siehe gtkdata). Ein zweiter Aufruf
startet kein zweites Fenster, sondern holt das laufende nach vorn.

Wayland erlaubt keinem Fenster, seine Position selbst zu setzen – gemerkt wird
deshalb nur die Groesse. Was man sieht: eine Kurve der letzten gut Stunde, die
Anteile der Quellen als Balken, und je Geraet eine Zeile. Zahlen ohne Verlauf
erzaehlen naemlich nichts.
"""
from __future__ import annotations

import sys
import time

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from .gtkdata import lese_anzeige  # noqa: E402

APP_ID = "de.powermeter.Widget"
TICK_S = 5

FARBEN = ["#5ad1c8", "#ffc94d", "#8ea2ff", "#7ee787", "#ff8f6b", "#c98bff"]
GERAET_FARBE = "#7d8ea8"  # Geraetebalken bleiben neutral, die Quellen sind farbig

CSS = """
.pm-fenster { background-image: linear-gradient(168deg, #1b2028 0%, #14181e 52%, #0f1218 100%); }
.pm-hero { font-family: monospace; font-size: 34pt; font-weight: 800; }
.pm-hero-einheit { font-size: 14pt; font-weight: 600; color: alpha(currentColor, 0.55); }
.pm-marke { font-size: 9pt; font-weight: 800; color: alpha(currentColor, 0.45); }
.pm-zeile { font-size: 11pt; }
.pm-zahl { font-family: monospace; font-weight: 700; font-size: 11pt; }
.pm-klein { font-size: 9pt; color: alpha(currentColor, 0.58); }
.pm-rand { background-color: alpha(#ffffff, 0.06); min-height: 1px; }
.pm-punkt-ok { color: #4fd39a; }
.pm-punkt-warn { color: #e5a50a; }
.pm-punkt-schlecht { color: #e5534b; }
pm-balken { min-height: 8px; border-radius: 4px; background-color: alpha(#ffffff, 0.07); }
pm-balken > progress { border-radius: 4px; background-image: none; }

"""


def _zahl(wert: float, stellen: int = 0) -> str:
    """Deutsches Dezimalkomma – 18.54 kWh neben 5,54 EUR liest sich wie ein Tippfehler."""
    text = f"{wert:,.{stellen}f}"
    return text.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def _kurz(wh: float) -> str:
    return f"{_zahl(wh / 1000.0, 2)} kWh" if abs(wh) >= 1000 else f"{_zahl(wh)} Wh"


def _geld(ct: float) -> str:
    return f"{_zahl(ct)} ct" if abs(ct) < 100 else f"{_zahl(ct / 100.0, 2)} EUR"


def _alt(alt_s) -> str:
    if alt_s is None:
        return "keine Messung"
    if alt_s < 75:
        return "gerade gemessen"
    if alt_s < 5400:
        return f"Daten {alt_s / 60:.0f} min alt"
    return f"Daten {alt_s / 3600:.1f} h alt"


def _unterzeile(anzeige: dict) -> str:
    """Spitze / Mittel / Tiefe und die Richtung der letzten fuenf Minuten.

    Das Alter der Daten steht einmal, in der Statuszeile unten — doppelt geoert
    wirkt es wie ein Fehler, wenn es sich nicht bewegt.
    """
    tag = anzeige["tag"]
    trend = anzeige.get("trend", 0.0)
    pfeil = "↗" if trend > 15 else "↘" if trend < -15 else "→"
    return (f"Spitze {_zahl(tag['spitze'])} W · Ø {_zahl(tag['mittel'])} W · "
            f"tief {_zahl(tag['tief'])} W · {pfeil} {_zahl(abs(trend))} W gegen vor 5 min")


def _marke(kasten: Gtk.Box, text: str) -> None:
    kasten.append(Gtk.Label(label=text, css_classes=["pm-marke"], xalign=0, margin_top=8))


def _wertzeile(kasten: Gtk.Box, titel: str, wert: str, unten: str = "") -> None:
    reihe = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    links = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
    links.set_hexpand(True)
    links.append(Gtk.Label(label=titel, css_classes=["pm-zeile"], xalign=0))
    if unten:
        links.append(Gtk.Label(label=unten, css_classes=["pm-klein"], xalign=0))
    reihe.append(links)
    rechts = Gtk.Label(label=wert, css_classes=["pm-zahl"], xalign=1)
    rechts.set_justify(Gtk.Justification.RIGHT)
    reihe.append(rechts)
    kasten.append(reihe)


class Streifen(Gtk.DrawingArea):
    """Ein Balken von Hand: Track, gefuellte Runde, eigene Farbe.

    LevelBar faerbt man ueber Offset-Namen – diese GTK-Version macht daraus aber
    genau eine Farbe fuer alle Balken. Also selbst gezeichnet, 8 px hoch.
    """

    def __init__(self, anteil: float, farbe: str) -> None:
        super().__init__()
        self.anteil = max(0.0, min(1.0, anteil))
        self.farbe = farbe
        self.set_content_height(8)
        self.set_hexpand(True)
        self.set_draw_func(self._zeichne)

    def _zeichne(self, _bereich, kr, breite: int, hoehe: int) -> None:
        halbe = hoehe / 2.0
        kr.set_source_rgba(1, 1, 1, 0.07)
        self._abgerundet(kr, 0.5, 0.5, breite - 1.0, hoehe - 1.0, halbe)
        kr.fill()
        fuellung = max(0.0, self.anteil) * (breite - 1.0)
        if fuellung > 1.0:
            rgb = self._rgb(self.farbe)
            kr.set_source_rgb(*rgb)
            self._abgerundet(kr, 0.5, 0.5, fuellung, hoehe - 1.0, halbe)
            kr.fill()
            kr.set_source_rgba(*rgb, 0.35)
            kr.rectangle(max(0.0, fuellung - halbe), 0.5, halbe, hoehe - 1.0)
            kr.fill()

    @staticmethod
    def _abgerundet(kr, x, y, b, h, r) -> None:
        r = min(r, b / 2.0, h / 2.0)
        kr.new_sub_path()
        kr.arc(x + b - r, y + r, r, -1.5708, 0)
        kr.arc(x + b - r, y + h - r, r, 0, 1.5708)
        kr.arc(x + r, y + h - r, r, 1.5708, 3.1416)
        kr.arc(x + r, y + r, r, 3.1416, 4.7124)
        kr.close_path()

    @staticmethod
    def _rgb(hexwert: str) -> tuple:
        wert = hexwert.lstrip("#")
        return tuple(int(wert[i:i + 2], 16) / 255.0 for i in (0, 2, 4))


class Kurve(Gtk.DrawingArea):
    """Verlauf der letzten Minuten: Flaeche, Durchschnittslinie, letzter Punkt."""

    def __init__(self) -> None:
        super().__init__()
        self.punkte: list[float] = []
        self.mittel = 0.0
        self.spitze = 0.0
        self.set_content_height(84)
        self.set_vexpand(False)
        self.set_draw_func(self._zeichne)

    def setze(self, punkte: list[float], mittel: float, spitze: float) -> None:
        self.punkte, self.mittel, self.spitze = punkte, mittel, spitze
        self.queue_draw()

    def _zeichne(self, _bereich, kr, breite: int, hoehe: int) -> None:
        if len(self.punkte) < 2:
            kr.set_source_rgba(1, 1, 1, 0.22)
            kr.rectangle(2, 2, breite - 4, hoehe - 4)
            kr.fill()
            return
        oben, unten = 6.0, hoehe - 8.0
        decke = max(self.spitze, max(self.punkte), 1.0) * 1.06
        stuetz = len(self.punkte) - 1
        schritt = (breite - 4.0) / stuetz

        def x(i: int) -> float:
            return 2.0 + i * schritt

        def y(w: float) -> float:
            return unten - (unten - oben) * (w / decke)

        kr.move_to(x(0), unten)
        for i, w in enumerate(self.punkte):
            kr.line_to(x(i), y(w))
        kr.line_to(x(stuetz), unten)
        kr.close_path()
        fuellung = cairo.LinearGradient(0, oben, 0, unten)
        fuellung.add_color_stop_rgba(0.0, 0.36, 0.82, 0.78, 0.30)
        fuellung.add_color_stop_rgba(1.0, 0.36, 0.82, 0.78, 0.02)
        kr.set_source(fuellung)
        kr.fill()

        linie = (0.36, 0.82, 0.78)
        kr.set_line_width(1.6)
        kr.set_source_rgb(*linie)
        kr.move_to(x(0), y(self.punkte[0]))
        for i, w in enumerate(self.punkte):
            kr.line_to(x(i), y(w))
        kr.stroke()

        kr.set_dash([3.0, 4.0], 0.0)
        kr.set_line_width(1.0)
        kr.set_source_rgba(1, 1, 1, 0.28)
        kr.move_to(2.0, y(self.mittel))
        kr.line_to(breite - 2.0, y(self.mittel))
        kr.stroke()
        kr.set_dash([], 0.0)

        letzter = self.punkte[-1]
        kr.set_source_rgb(*linie)
        kr.arc(x(stuetz), y(letzter), 2.6, 0, 2 * 3.14159)
        kr.fill()
        # Maßstab steht in der Zeile darüber (Spitze / Ø / tief), nicht im Plot.
        kr.set_source_rgba(1, 1, 1, 0.10)
        kr.set_line_width(1.0)
        kr.move_to(2.0, oben)
        kr.line_to(breite - 2.0, oben)
        kr.stroke()


class Balken(Gtk.Box):
    """Eine Quelle als benannter Balken – Anteil sichtbar, nicht nur Zahl."""

    def __init__(self, name: str, watt: float, anteil: float, platz: int) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        reihe = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        reihe.append(Gtk.Label(label=name, css_classes=["pm-zeile"], xalign=0, hexpand=True,
                               ellipsize=Pango.EllipsizeMode.END))
        reihe.append(Gtk.Label(label=f"{_zahl(watt)} W", css_classes=["pm-zahl"], xalign=1))
        self.append(reihe)
        self.append(Streifen(anteil, FARBEN[platz % len(FARBEN)]))


class Fenster(Adw.ApplicationWindow):
    def __init__(self, anwendung: Adw.Application, pfade) -> None:
        super().__init__(application=anwendung)
        self.add_css_class("pm-fenster")
        self.cfg = pfade.config.parent
        self.daten = pfade.data
        self.anzeige: dict = {}
        self._stempel = 0.0

        titel = Adw.WindowTitle(title="Strom", subtitle="powermeter")
        kopf = Gtk.HeaderBar()
        kopf.set_title_widget(titel)
        self.titel = titel
        knopf = Gtk.Button(icon_name="view-refresh-symbolic")
        knopf.set_tooltip_text("neu laden")
        knopf.add_css_class("flat")
        knopf.connect("clicked", lambda *_: self.aktualisiere(erzwingen=True))
        kopf.pack_end(knopf)
        menue = Gtk.MenuButton(icon_name="open-menu-symbolic")
        menue.add_css_class("flat")
        modell = Gio.Menu()
        modell.append("neu laden", "app.aktualisieren")
        modell.append("beenden", "app.beenden")
        menue.set_menu_model(modell)
        kopf.pack_end(menue)

        self.werkzeug = Adw.ToolbarView()
        self.werkzeug.add_top_bar(kopf)

        basis = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        basis.set_margin_top(8)
        basis.set_margin_bottom(12)
        basis.set_margin_start(16)
        basis.set_margin_end(16)
        self.werkzeug.set_content(basis)
        self.set_content(self.werkzeug)

        # --------------------------------------------------------------- Hero
        self.hero = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.hero_zahl = Gtk.Label(label="–", css_classes=["pm-hero"], xalign=0)
        self.hero_einheit = Gtk.Label(label="W", css_classes=["pm-hero-einheit"], xalign=0, valign=Gtk.Align.END)
        self.hero.append(self.hero_zahl)
        self.hero.append(self.hero_einheit)
        self.hero_unten = Gtk.Label(label="", css_classes=["pm-klein"], xalign=0)
        basis.append(self.hero)
        basis.append(self.hero_unten)

        self.kurve = Kurve()
        basis.append(self.kurve)

        basis.append(Gtk.Box(css_classes=["pm-rand"], margin_top=8))
        _marke(basis, "KOSTEN")
        self.zeile_heute = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.zeile_gesamt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        basis.append(self.zeile_heute)
        basis.append(self.zeile_gesamt)

        _marke(basis, "ANTEILE")
        self.anteile_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        basis.append(self.anteile_box)

        _marke(basis, "GERÄTE")
        self.geraete_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        basis.append(self.geraete_box)

        self.status = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, margin_top=10)
        self.punkt = Gtk.Label(label="●", css_classes=["pm-punkt-ok"])
        self.status_text = Gtk.Label(label="", css_classes=["pm-klein"], xalign=0)
        self.status.append(self.punkt)
        self.status.append(self.status_text)
        basis.append(self.status)

        self.set_default_size(*self._groesse())
        self.connect("unmap", self._groesse_merken)
        self.aktualisiere()

    # --------------------------------------------------------------- Einstellungen
    def _einstellung(self) -> dict:
        try:
            import json
            return json.loads((self.cfg / "widget.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _groesse(self) -> tuple[int, int]:
        g = self._einstellung().get("groesse") or []
        if len(g) == 2 and all(isinstance(w, int) and 260 <= w <= 2000 for w in g):
            return (g[0], g[1])
        return (420, 596)

    def _groesse_merken(self, *_):
        try:
            import json
            b = self.get_default_size()
            dat = self._einstellung()
            dat["groesse"] = [b.width, b.height]
            (self.cfg / "widget.json").write_text(json.dumps(dat), encoding="utf-8")
        except (OSError, AttributeError):
            pass

    # ------------------------------------------------------------------ Anzeige
    def _geraet(self, g: dict, gesamt_w: float) -> Gtk.Widget:
        kaste = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        reihe = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        reihe.append(Gtk.Label(label=g["name"], css_classes=["pm-zeile"], xalign=0, hexpand=True,
                               ellipsize=Pango.EllipsizeMode.END))
        # 0 W ware eine Erfindung: ohne Zeile in der neuesten Minute meldet das
        # Geraet gerade nichts (aus, importiert, Netz weg).
        gemeldet = bool(g.get("aktuell"))
        reihe.append(Gtk.Label(label=f"{_zahl(g['w_aktuell'])} W" if gemeldet else "—",
                               css_classes=["pm-zahl"] + ([] if gemeldet else ["pm-klein"]), xalign=1))
        kaste.append(reihe)
        anteil = g["w_aktuell"] / gesamt_w if (gesamt_w and gemeldet) else 0.0
        kaste.append(Streifen(anteil, GERAET_FARBE if gemeldet else "#39424e"))
        klein = (f"heute {_kurz(g['wh_heute'])} · gesamt {_kurz(g['wh_gesamt'])}"
                 + (" · Messung importiert" if g["gemutet"] else "")
                 + ("" if gemeldet else f" · zuletzt {str(g.get('zuletzt', ''))[11:16]}"))
        kaste.append(Gtk.Label(label=klein, css_classes=["pm-klein"], xalign=0))
        return kaste

    def aktualisiere(self, *_ , erzwingen: bool = False):
        stempel = self._frischestempel()
        if not erzwingen and stempel == self._stempel and self.anzeige:
            self._alter_nachziehen()
            return False
        self._stempel = stempel
        self.anzeige = lese_anzeige(self.daten, self.cfg)
        self._zeige(self.anzeige)
        return False

    def _alter_nachziehen(self) -> None:
        """Daten sind unveraendert: nur die Altersangabe stimmt noch nicht."""
        self.hero_unten.set_text(_unterzeile(self.anzeige))
        if self.anzeige["alt_s"] is not None:
            self.status_text.set_text(_alt(self.anzeige["alt_s"]))

    def _frischestempel(self):
        try:
            return round(max([p.stat().st_mtime for p in self.daten.glob("20*-*csv")] +
                              [self.daten.joinpath("state.json").stat().st_mtime]), 1)
        except (OSError, ValueError):
            return 0.0

    def _zeige(self, anzeige: dict) -> None:
        ges = anzeige["gesamt"]
        tag = anzeige["tag"]
        self.titel.set_subtitle(f"seit {anzeige['seit']}")
        self.hero_zahl.set_text(_zahl(ges["w_aktuell"]))
        self.hero_zahl.set_css_classes(["pm-hero",
                                        "pm-punkt-warn" if ges["w_aktuell"] > 700 else "pm-punkt-ok"])
        self.hero_unten.set_text(_unterzeile(anzeige))
        self.kurve.setze(anzeige["verlauf"], tag["mittel"], tag["spitze"])

        for kind in (self.zeile_heute, self.zeile_gesamt, self.anteile_box, self.geraete_box):
            while kind.get_last_child() is not None:
                kind.remove(kind.get_last_child())

        _wertzeile(self.zeile_heute, "heute", _kurz(ges["wh_heute"]),
                   f"Kosten {_geld(ges['ct_heute'])}")
        _wertzeile(self.zeile_gesamt, "seit Messbeginn", _kurz(ges["wh_gesamt"]),
                   f"Kosten {_geld(ges['ct_gesamt'])} · Tarif {_zahl(anzeige['ct_kwh'], 2)} ct/kWh")

        groest = max((w for _, w in anzeige["anteile"]), default=1.0)
        for i, (name, watt) in enumerate(anzeige["anteile"][:5]):
            self.anteile_box.append(Balken(name, watt, watt / groest, i))

        for g in anzeige["geraete"]:
            self.geraete_box.append(self._geraet(g, ges["w_aktuell"]))

        meldung = " · ".join(anzeige["fehler"])
        self.punkt.set_css_classes(["pm-punkt-schlecht" if anzeige["veraltet"]
                                    else "pm-punkt-warn" if anzeige["fehler"] else "pm-punkt-ok"])
        self.status_text.set_text((f"Sammler: {_alt(anzeige['alt_s'])}" if not anzeige["veraltet"]
                                   else "Sammler liefert keine neuen Zeilen")
                                  + (f" — {meldung}" if meldung else ""))


class Anwendung(Adw.Application):
    def __init__(self, pfade) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)
        self.pfade = pfade
        self.fenster = None

    def do_startup(self):
        Adw.Application.do_startup(self)
        eigenschaft = Gio.SimpleAction.new("beenden", None)
        eigenschaft.connect("activate", lambda *_: self.quit())
        self.add_action(eigenschaft)
        neu = Gio.SimpleAction.new("aktualisieren", None)
        neu.connect("activate", lambda *_: self.fenster and self.fenster.aktualisiere(erzwingen=True))
        self.add_action(neu)
        anbieter = Gtk.CssProvider()
        anbieter.load_from_data(CSS.encode("utf-8"))
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), anbieter, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def do_activate(self):
        if self.fenster is None:
            self.fenster = Fenster(self, self.pfade)
            GLib.timeout_add_seconds(TICK_S, self._tick)
        self.fenster.present()

    def _tick(self):
        try:
            if self.fenster is not None:
                self.fenster.aktualisiere()
        except Exception as e:  # Anzeige darf an Daten nicht zerbrechen
            print(f"Anzeige: {e}", file=sys.stderr)
        return True


def run_widget(paths) -> int:
    """Anzeige starten; Rueckkehr von `Adw.Application.run` ist normales Ende."""
    anwendung = Anwendung(paths)
    return anwendung.run(sys.argv[:1])
