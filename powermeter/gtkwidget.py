"""Desktop-Anzeige fuer Linux (GTK4/Libadwaita) – ohne Tk, weil Tk hier fehlt.

Anzeige und Sammler sind getrennt: der Sammler (`powermeter run --headless`)
misst und schreibt, dieses Fenster liest nur (siehe gtkdata). Ein zweiter Aufruf
startet kein zweites Fenster, sondern holt das laufende nach vorn.

Zwei Seiten: Ueberblick und – ein Klick auf ein Geraet – dessen Details. Wayland
erlaubt keinem Fenster, seine Position selbst zu setzen, gemerkt wird deshalb nur
die Groesse.
"""
from __future__ import annotations

import sys

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from .gtkdata import BEREICHE, lese_anzeige  # noqa: E402

APP_ID = "de.powermeter.Widget"
TICK_S = 5

FARBEN = ["#5ad1c8", "#ffc94d", "#8ea2ff", "#7ee787", "#ff8f6b", "#c98bff"]
GERAET_FARBE = "#7d8ea8"
AKZENT = (0.35, 0.82, 0.78)

CSS = """
.pm-fenster { background-image: linear-gradient(168deg, #1c222b 0%, #151a21 46%, #0f1318 100%); }
pm-karte, .pm-karte { background-color: alpha(#ffffff, 0.035); border: 1px solid alpha(#ffffff, 0.075);
                      border-radius: 14px; padding: 10px 12px; }
.pm-karte-flach { background-color: transparent; border: 1px solid transparent; }
.pm-anklickbar:hover { background-color: alpha(#5ad1c8, 0.09); border-color: alpha(#5ad1c8, 0.28); }
pm-knopf { border-radius: 12px; padding: 4px 10px; }
.pm-hero { font-family: monospace; font-size: 36pt; font-weight: 800; }
.pm-hero-klein { font-family: monospace; font-size: 24pt; font-weight: 800; }
.pm-einheit { font-size: 13pt; font-weight: 600; color: alpha(currentColor, 0.5); }
.pm-marke { font-size: 9pt; font-weight: 800; color: alpha(currentColor, 0.42); }
.pm-zeile { font-size: 11.5pt; font-weight: 600; }
.pm-zahl { font-family: monospace; font-weight: 700; font-size: 11.5pt; }
.pm-klein { font-size: 9.5pt; color: alpha(currentColor, 0.58); }
.pm-chip { font-family: monospace; font-size: 9.5pt; font-weight: 700; border-radius: 999px;
           padding: 3px 9px; background-color: alpha(#ffffff, 0.07); }
.pm-chip-hoch { background-color: alpha(#ff8f6b, 0.18); color: #ff9d7d; }
.pm-chip-runter { background-color: alpha(#5ad1c8, 0.16); color: #6fdcd0; }
.pm-trenn { background-color: alpha(#ffffff, 0.07); min-height: 1px; margin-top: 2px; }
.pm-punkt-ok { color: #4fd39a; }
.pm-punkt-warn { color: #e5a50a; }
.pm-punkt-schlecht { color: #e5534b; }
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
        return f"Daten {_zahl(alt_s / 60)} min alt"
    return f"Daten {_zahl(alt_s / 3600, 1)} h alt"


def _uhr(zeit: str) -> str:
    return zeit[11:16] if len(zeit) >= 16 else zeit


def _marke(text: str) -> Gtk.Widget:
    return Gtk.Label(label=text, css_classes=["pm-marke"], xalign=0, margin_top=2)


def _wertzeile(titel: str, wert: str, unten: str = "") -> Gtk.Widget:
    reihe = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
    links = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
    links.set_hexpand(True)
    links.append(Gtk.Label(label=titel, css_classes=["pm-zeile"], xalign=0))
    if unten:
        links.append(Gtk.Label(label=unten, css_classes=["pm-klein"], xalign=0))
    reihe.append(links)
    reihe.append(Gtk.Label(label=wert, css_classes=["pm-zahl"], xalign=1))
    return reihe


def _farbindex(name: str) -> int:
    """Farbe pro Quellenname stabil: sonst ist dieselbe Quelle mal türkis, mal blau."""
    return sum(ord(zeichen) for zeichen in name) % len(FARBEN)


def _pfeil(trend: float) -> str:
    return "↗" if trend > 15 else "↘" if trend < -15 else "→"


def _chip(text: str, richtung: int) -> Gtk.Widget:
    klassen = ["pm-chip"] + (["pm-chip-hoch"] if richtung > 0 else ["pm-chip-runter"] if richtung < 0 else [])
    knopf = Gtk.Label(label=text, css_classes=klassen)
    knopf.set_valign(Gtk.Align.CENTER)      # sonst wird aus der Pille ein Kreis
    return knopf


def _karte(kind: int = Gtk.Orientation.VERTICAL, abstand: int = 4) -> Gtk.Box:
    kaste = Gtk.Box(orientation=kind, spacing=abstand)
    kaste.add_css_class("pm-karte")
    return kaste


class Streifen(Gtk.DrawingArea):
    """Balken von Hand: LevelBar faerbt man in dieser GTK-Version nicht pro Instanz."""

    def __init__(self, anteil: float, farbe: str, hoehe: int = 8) -> None:
        super().__init__()
        self.anteil = max(0.0, min(1.0, anteil))
        self.farbe = farbe
        self.set_content_height(hoehe)
        self.set_hexpand(True)
        self.set_draw_func(self._zeichne)

    def _zeichne(self, _bereich, kr, breite: int, hoehe: int) -> None:
        halbe = hoehe / 2.0
        kr.set_source_rgba(1, 1, 1, 0.07)
        self._rund(kr, 0.5, 0.5, breite - 1.0, hoehe - 1.0, halbe)
        kr.fill()
        fuellung = self.anteil * (breite - 1.0)
        if fuellung > 1.0:
            kr.set_source_rgb(*self._rgb(self.farbe))
            self._rund(kr, 0.5, 0.5, fuellung, hoehe - 1.0, halbe)
            kr.fill()

    @staticmethod
    def _rund(kr, x, y, b, h, r) -> None:
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
    """Verlauf: Flaeche mit Farbverlauf, Linie, letzter Punkt. Kompakt = Miniatur."""

    def __init__(self, hoehe: int = 92, kompakt: bool = False) -> None:
        super().__init__()
        self.punkte: list[float] = []
        self.mittel = 0.0
        self.spitze = 0.0
        self.kompakt = kompakt
        self.set_content_height(hoehe)
        self.set_hexpand(True)
        self.set_draw_func(self._zeichne)

    def setze(self, punkte: list[float], mittel: float = 0.0, spitze: float = 0.0) -> None:
        self.punkte, self.mittel, self.spitze = punkte, mittel, spitze
        self.queue_draw()

    def _zeichne(self, _bereich, kr, breite: int, hoehe: int) -> None:
        oben, unten = (2.0, hoehe - 2.0) if self.kompakt else (8.0, hoehe - 10.0)
        if len(self.punkte) < 2:
            kr.set_source_rgba(1, 1, 1, 0.06)
            self._rund(kr, 0.5, 0.5, breite - 1.0, hoehe - 1.0, 8)
            kr.fill()
            return
        decke = max(self.spitze, max(self.punkte), 1.0) * 1.10
        stuetz = len(self.punkte) - 1
        schritt = (breite - 4.0) / stuetz

        def x(i: int) -> float:
            return 2.0 + i * schritt

        def y(w: float) -> float:
            return unten - (unten - oben) * (w / decke)

        if not self.kompakt:  # dezente Rasterlinien bei 1/3 und 2/3
            kr.set_source_rgba(1, 1, 1, 0.05)
            kr.set_line_width(1.0)
            for anteil in (1 / 3, 2 / 3):
                kr.move_to(2.0, unten - (unten - oben) * anteil)
                kr.line_to(breite - 2.0, unten - (unten - oben) * anteil)
                kr.stroke()

        kr.move_to(x(0), unten)
        for i, w in enumerate(self.punkte):
            kr.line_to(x(i), y(w))
        kr.line_to(x(stuetz), unten)
        kr.close_path()
        fuellung = cairo.LinearGradient(0, oben, 0, unten)
        fuellung.add_color_stop_rgba(0.0, *AKZENT, 0.34 if not self.kompakt else 0.30)
        fuellung.add_color_stop_rgba(1.0, *AKZENT, 0.02)
        kr.set_source(fuellung)
        kr.fill()

        linie = cairo.LinearGradient(0, 0, breite, 0)
        linie.add_color_stop_rgb(0.0, 0.30, 0.71, 0.86)
        linie.add_color_stop_rgb(1.0, *AKZENT)
        kr.set_source(linie)
        kr.set_line_width(1.3 if self.kompakt else 1.9)
        kr.set_line_join(cairo.LINE_JOIN_ROUND)
        kr.move_to(x(0), y(self.punkte[0]))
        for i, w in enumerate(self.punkte):
            kr.line_to(x(i), y(w))
        kr.stroke()

        if not self.kompakt and self.mittel:
            kr.set_dash([3.0, 4.0], 0.0)
            kr.set_line_width(1.0)
            kr.set_source_rgba(1, 1, 1, 0.26)
            kr.move_to(2.0, y(self.mittel))
            kr.line_to(breite - 2.0, y(self.mittel))
            kr.stroke()
            kr.set_dash([], 0.0)

        kr.set_source_rgb(*AKZENT)
        kr.arc(x(stuetz), y(self.punkte[-1]), 2.0 if self.kompakt else 2.8, 0, 2 * 3.14159)
        kr.fill()

    @staticmethod
    def _rund(kr, x, y, b, h, r) -> None:
        r = min(r, b / 2.0, h / 2.0)
        kr.new_sub_path()
        kr.arc(x + b - r, y + r, r, -1.5708, 0)
        kr.arc(x + b - r, y + h - r, r, 0, 1.5708)
        kr.arc(x + r, y + h - r, r, 1.5708, 3.1416)
        kr.arc(x + r, y + r, r, 3.1416, 4.7124)
        kr.close_path()


class Fenster(Adw.ApplicationWindow):
    def __init__(self, anwendung: Adw.Application, pfade, bereich: str = "h") -> None:
        super().__init__(application=anwendung)
        self.add_css_class("pm-fenster")
        self.cfg = pfade.config.parent
        self.daten = pfade.data
        self.anzeige: dict = {}
        self.bereich = bereich if bereich in BEREICHE else "h"
        self._stempel = 0.0
        self._alter = 0.0
        self._tween_zaehler = None

        titel = Adw.WindowTitle(title="Strom", subtitle="powermeter")
        kopf = Gtk.HeaderBar()
        kopf.set_title_widget(titel)
        self.titel = titel
        knopf = Gtk.Button(icon_name="view-refresh-symbolic", css_classes=["flat"])
        knopf.set_tooltip_text("neu laden")
        knopf.connect("clicked", lambda *_: self.aktualisiere(erzwingen=True))
        kopf.pack_end(knopf)
        menue_knopf = Gtk.MenuButton(icon_name="open-menu-symbolic", css_classes=["flat"])
        modell = Gio.Menu()
        modell.append("neu laden", "app.aktualisieren")
        modell.append("zurueck zum Ueberblick", "app.zurueck")
        modell.append("beenden", "app.beenden")
        menue_knopf.set_menu_model(modell)
        kopf.pack_end(menue_knopf)

        self.werkzeug = Adw.ToolbarView()
        self.werkzeug.add_top_bar(kopf)

        self.seiten = Gtk.Stack(transition_type=Gtk.StackTransitionType.SLIDE_LEFT_RIGHT,
                                transition_duration=220)
        self.werkzeug.set_content(self.seiten)
        self.set_content(self.werkzeug)
        self.seiten.set_vexpand(True)
        self._bau_ueberblick()
        self._bau_details()

        self.set_default_size(*self._groesse())
        self.connect("unmap", self._groesse_merken)
        druecker = Gtk.EventControllerKey()
        druecker.connect("key-pressed", self._taste)
        self.add_controller(druecker)
        self.aktualisiere()

    # ------------------------------------------------------------------ Aufbau
    def _bau_ueberblick(self) -> None:
        roll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        spalte = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        spalte.set_margin_top(10)
        spalte.set_margin_bottom(14)
        spalte.set_margin_start(14)
        spalte.set_margin_end(14)
        roll.set_child(spalte)

        karte = _karte()
        self.hero = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.hero_zahl = Gtk.Label(label="–", css_classes=["pm-hero"], xalign=0)
        self.hero_einheit = Gtk.Label(label="W", css_classes=["pm-einheit"], xalign=0,
                                      valign=Gtk.Align.END, margin_bottom=6)
        self.hero_chip = _chip("–", 0)
        abstand = Gtk.Box()
        abstand.set_hexpand(True)
        self.hero.append(self.hero_zahl)
        self.hero.append(self.hero_einheit)
        self.hero.append(abstand)
        self.hero.append(self.hero_chip)
        self.hero_unten = Gtk.Label(label="", css_classes=["pm-klein"], xalign=0, wrap=True)
        self.kurve = Kurve(hoehe=78)
        karte.append(self.hero)
        karte.append(self.hero_unten)
        karte.append(self.kurve)
        # Diese Libadwaita-Version hat keinen SegmentedButton -> verknüpfte
        # ToggleButtons (Gtk „linked“) tun es genauso und sind versionsfest.
        self.bereich_knoepfe: dict[str, Gtk.ToggleButton] = {}
        self._bereich_sperre = False
        leiste = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0,
                         css_classes=["linked"], halign=Gtk.Align.CENTER, margin_top=4)
        for wert in ("h", "tag", "woche"):
            knopf = Gtk.ToggleButton(label=BEREICHE[wert][0].replace("letzte ", ""))
            knopf.set_active(wert == self.bereich)
            # Signal "clicked", nicht "toggled": beim "toggled" löst jedes
            # programmatische set_active den nächsten Handler aus – die Schalter
            # haben sich gegenseitig zurückgesetzt bis der Stack überlief
            # (RecursionError, vom Schreibtisch als „antwortet nicht“ gemeldet).
            knopf.connect("clicked", self.bereich_klick, wert)
            leiste.append(knopf)
            self.bereich_knoepfe[wert] = knopf
        karte.append(leiste)
        karte.append(Gtk.Box(css_classes=["pm-trenn"], margin_top=2))
        karte.append(_marke("KOSTEN"))
        self.zeile_heute = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.zeile_gesamt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        karte.append(self.zeile_heute)
        karte.append(self.zeile_gesamt)
        spalte.append(karte)

        anteile = _karte()
        anteile.append(_marke("ANTEILE GERADE"))
        self.anteile_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        anteile.append(self.anteile_box)
        spalte.append(anteile)

        geraete = _karte()
        geraete.append(_marke("GERÄTE · DETAILS MIT KLICK"))
        self.geraete_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        geraete.append(self.geraete_box)
        spalte.append(geraete)

        status = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, margin_top=2)
        self.punkt = Gtk.Label(label="●", css_classes=["pm-punkt-ok"])
        self.status_text = Gtk.Label(label="", css_classes=["pm-klein"], xalign=0, hexpand=True,
                                     wrap=True)
        self.status_seit = Gtk.Label(label="", css_classes=["pm-klein"], xalign=1)
        status.append(self.punkt)
        status.append(self.status_text)
        status.append(self.status_seit)
        spalte.append(status)

        self.seiten.add_named(roll, "ueberblick")

    def _bau_details(self) -> None:
        roll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        spalte = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        spalte.set_margin_top(10)
        spalte.set_margin_bottom(14)
        spalte.set_margin_start(14)
        spalte.set_margin_end(14)
        roll.set_child(spalte)

        oben = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        zurueck = Gtk.Button(icon_name="go-previous-symbolic", css_classes=["flat", "pm-knopf"])
        zurueck.set_tooltip_text("zurueck (Esc)")
        zurueck.connect("clicked", lambda *_: self.seite("ueberblick"))
        self.detail_name = Gtk.Label(label="", css_classes=["pm-zeile"], xalign=0, hexpand=True,
                                     ellipsize=Pango.EllipsizeMode.END)
        oben.append(zurueck)
        oben.append(self.detail_name)
        spalte.append(oben)

        karte = _karte()
        self.detail_hero = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.detail_zahl = Gtk.Label(label="–", css_classes=["pm-hero-klein"], xalign=0)
        self.detail_einheit = Gtk.Label(label="W", css_classes=["pm-einheit"], xalign=0,
                                        valign=Gtk.Align.END, margin_bottom=4)
        self.detail_chip = _chip("–", 0)
        self.detail_hero.append(self.detail_zahl)
        self.detail_hero.append(self.detail_einheit)
        self.detail_hero.append(self.detail_chip)
        self.detail_unten = Gtk.Label(label="", css_classes=["pm-klein"], xalign=0, wrap=True)
        self.detail_kurve = Kurve(hoehe=76)
        karte.append(self.detail_hero)
        karte.append(self.detail_unten)
        karte.append(self.detail_kurve)
        spalte.append(karte)

        quellen = _karte()
        quellen.append(_marke("QUELLEN DIESES GERÄTS"))
        self.detail_quellen = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        quellen.append(self.detail_quellen)
        spalte.append(quellen)

        tage = _karte()
        tage.append(_marke("LETZTE TAGE"))
        self.detail_tage = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        tage.append(self.detail_tage)
        spalte.append(tage)

        self.seiten.add_named(roll, "details")
        self.detail_id = ""

    # --------------------------------------------------------------- Einstellungen
    def _einstellung(self) -> dict:
        try:
            import json
            return json.loads((self.cfg / "widget.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _groesse(self) -> tuple[int, int]:
        g = self._einstellung().get("groesse") or []
        if len(g) == 2 and all(isinstance(w, int) and 300 <= w <= 2000 for w in g):
            return (g[0], g[1])
        return (428, 764)

    def _groesse_merken(self, *_):
        try:
            import json
            b = self.get_default_size()
            dat = self._einstellung()
            dat["groesse"] = [b.width, b.height]
            (self.cfg / "widget.json").write_text(json.dumps(dat), encoding="utf-8")
        except (OSError, AttributeError):
            pass

    def _taste(self, _handler, schluessel, *_):
        if schluessel == Gdk.KEY_Escape and self.seiten.get_visible_child_name() == "details":
            self.seite("ueberblick")
            return True
        return False

    def seite(self, name: str) -> None:
        self.seiten.set_visible_child_name(name)

    def bereich_klick(self, knopf, wert: str) -> None:
        """Bereich wechseln: ein Schalter an, die anderen aus, dann neu rechnen."""
        if self._bereich_sperre:
            return                                  # Rückkopplung: hier nichts tun
        if wert == self.bereich:
            knopf.set_active(True)                  # Klick auf den aktiven Schalter
            return
        self._bereich_sperre = True      # deckt auch ein Nachziehen ab: kein Signal,
        try:                             # das hier erneut hereinfällt, kann schleifen
            self.bereich = wert
            for anderer, schalter in self.bereich_knoepfe.items():
                schalter.set_active(anderer == wert)
            self.aktualisiere(erzwingen=True)
        finally:
            self._bereich_sperre = False

    # ------------------------------------------------------------------ Anzeige
    def _geraet_zeile(self, g: dict, gesamt_w: float) -> Gtk.Widget:
        knopf = Gtk.Button(css_classes=["flat", "pm-karte", "pm-anklickbar"])
        knopf.connect("clicked", lambda *_: self.zeige_details(g["id"]))
        kaste = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        reihe = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        reihe.append(Gtk.Label(label=g["name"], css_classes=["pm-zeile"], xalign=0, hexpand=True,
                               ellipsize=Pango.EllipsizeMode.END))
        if g["aktuell"]:
            wert = f"{_zahl(g['w_aktuell'])} W"
        elif g.get("verspaetet"):
            wert = f"{_zahl(g['w_aktuell'])} W  ·  {_uhr(g.get('zuletzt', ''))}"
        else:
            wert = "—"
        reihe.append(Gtk.Label(label=wert, css_classes=["pm-zahl"], xalign=1))
        kaste.append(reihe)
        mini = Kurve(hoehe=18, kompakt=True)
        mini.setze(g.get("verlauf", []))
        kaste.append(mini)
        kaste.append(Gtk.Label(
            label=f"heute {_kurz(g['wh_heute'])} · gesamt {_kurz(g['wh_gesamt'])}"
                  + (" · Import vom anderen Rechner" if g["gemutet"] else ""),
            css_classes=["pm-klein"], xalign=0))
        knopf.set_child(kaste)
        return knopf

    def zeige_details(self, geraet_id: str) -> None:
        g = next((x for x in self.anzeige.get("geraete", []) if x["id"] == geraet_id), None)
        if g is None:
            return
        self.detail_id = geraet_id
        self.detail_name.set_text(g["name"])
        trend = 0.0
        reihe = g.get("verlauf", [])
        if len(reihe) >= 6:
            trend = reihe[-1] - reihe[-6]
        if g["aktuell"] or g.get("verspaetet"):
            self.detail_zahl.set_text(_zahl(g["w_aktuell"]))
            self.detail_chip.set_text(f"{_pfeil(trend)} {_zahl(abs(trend))} W")
            self.detail_chip.set_tooltip_text("Differenz gegen den vorherigen Messkasten")
        else:
            self.detail_zahl.set_text("—")
            self.detail_chip.set_text("kein Messwert")
        self.detail_chip.set_css_classes(["pm-chip"] +
                                         (["pm-chip-hoch"] if trend > 15 else
                                          ["pm-chip-runter"] if trend < -15 else []))
        anfang = g["tage"][0][0] if g.get("tage") else ""
        note = (f"gesamt {_kurz(g['wh_gesamt'])}" + (f" seit {anfang}" if anfang else "")
                + f" · {_uhr(g.get('zuletzt', ''))} zuletzt")
        if g.get("verspaetet"):
            note += " · dieser Rechner liefert in Schwüben (Import), nicht jede Minute"
        self.detail_unten.set_text(note)
        self.detail_kurve.setze(reihe)

        while self.detail_quellen.get_last_child() is not None:
            self.detail_quellen.remove(self.detail_quellen.get_last_child())
        quellen = sorted(g["quellen"].items(), key=lambda kv: -kv[1]) or []
        groest = max((w for _, w in quellen), default=1.0)
        for i, (name, watt) in enumerate(quellen):
            self.detail_quellen.append(_quellenzeile(name, watt, watt / groest, i))
        if not quellen:
            self.detail_quellen.append(Gtk.Label(label="in der letzten Minute keine Quellen gemeldet",
                                                 css_classes=["pm-klein"], xalign=0))

        while self.detail_tage.get_last_child() is not None:
            self.detail_tage.remove(self.detail_tage.get_last_child())
        tage = g.get("tage", [])
        groesster = max((wh for _, wh in tage), default=1.0)
        for tag, wh in tage:
            reihe = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            reihe.append(Gtk.Label(label=tag, css_classes=["pm-klein"], xalign=0,
                                   valign=Gtk.Align.CENTER))
            balken = Streifen(wh / groesster, FARBEN[2], hoehe=14)
            balken.set_valign(Gtk.Align.CENTER)
            reihe.append(balken)
            reihe.append(Gtk.Label(label=_kurz(wh), css_classes=["pm-zahl"], xalign=1,
                                   valign=Gtk.Align.CENTER))
            self.detail_tage.append(reihe)
        self.seite("details")

    def aktualisiere(self, *_, erzwingen: bool = False):
        stempel = self._frischestempel()
        if not erzwingen and stempel == self._stempel and self.anzeige:
            self._alter_nachziehen()
            return False
        self._stempel = stempel
        self.anzeige = lese_anzeige(self.daten, self.cfg, bereich=self.bereich)
        self._zeige()
        return False

    def _frischestempel(self):
        try:
            return round(max([p.stat().st_mtime for p in self.daten.glob("20*-*csv")] +
                              [self.daten.joinpath("state.json").stat().st_mtime]), 1)
        except (OSError, ValueError):
            return 0.0

    def _unterzeile(self, anzeige: dict) -> str:
        tag = anzeige["tag"]
        text = (f"Spitze {_zahl(tag['spitze'])} W · Ø {_zahl(tag['mittel'])} W · "
                f"tief {_zahl(tag['tief'])} W · {anzeige['bereich_text']} · Trend ggü. 5 min")
        tag = anzeige["tag"]
        if anzeige["bereich"] == "woche" and tag.get("wh_tag"):
            text += f" · Ø {_zahl(tag['wh_tag'] / 1000, 2)} kWh/Tag"
        return text

    def _alter_nachziehen(self) -> None:
        self.status_text.set_text(_alt(self.anzeige["alt_s"]))

    def _tween(self, ziel: float) -> None:
        """Hero-Zahl in wenigen Schritten nachziehen – springende Zahlen nerven."""
        start = self._alter
        self._alter = ziel
        schritte = 6
        if abs(ziel - start) < 1.0 or not hasattr(self, "_tween_zaehler"):
            self.hero_zahl.set_text(_zahl(ziel))
            return

        def einer( nr: int) -> bool:
            if nr >= schritte:
                self.hero_zahl.set_text(_zahl(ziel))
                self._tween_zaehler = None
                return False
            wert = start + (ziel - start) * (nr + 1) / schritte
            self.hero_zahl.set_text(_zahl(wert))
            self._tween_zaehler = nr + 1
            return True

        self._tween_zaehler = 0
        GLib.timeout_add(45, lambda: einer(self._tween_zaehler or 0))

    def _zeige(self) -> None:
        anzeige = self.anzeige
        ges = anzeige["gesamt"]
        tag = anzeige["tag"]
        self.titel.set_subtitle(f"seit {anzeige['seit']}")
        self._tween(float(ges["w_aktuell"]))
        self.hero_unten.set_text(self._unterzeile(anzeige))
        self.kurve.setze(anzeige["verlauf"], tag["mittel"], tag["spitze"])
        trend = anzeige.get("trend", 0.0)
        self.hero_chip.set_text(f"{_pfeil(trend)} {_zahl(abs(trend))} W")
        self.hero_chip.set_tooltip_text("Differenz gegen vor fünf Minuten")
        self.hero_chip.set_css_classes(["pm-chip"] +
                                       (["pm-chip-hoch"] if trend > 15 else
                                        ["pm-chip-runter"] if trend < -15 else []))

        for kasten in (self.zeile_heute, self.zeile_gesamt, self.anteile_box, self.geraete_box):
            while kasten.get_last_child() is not None:
                kasten.remove(kasten.get_last_child())

        self.zeile_heute.append(_wertzeile("heute", _kurz(ges["wh_heute"]),
                                           f"Kosten {_geld(ges['ct_heute'])}"))
        self.zeile_gesamt.append(_wertzeile("seit Messbeginn", _kurz(ges["wh_gesamt"]),
                                            f"Kosten {_geld(ges['ct_gesamt'])} · Tarif "
                                            f"{_zahl(anzeige['ct_kwh'], 2)} ct/kWh"))

        groest = max((w for _, w in anzeige["anteile"]), default=1.0)
        for i, (name, watt) in enumerate(anzeige["anteile"][:5]):
            self.anteile_box.append(_quellenzeile(name, watt, watt / groest, i))
        if not anzeige["anteile"]:
            self.anteile_box.append(Gtk.Label(label="in der neuesten Minute nichts gemessen",
                                              css_classes=["pm-klein"], xalign=0))

        for g in anzeige["geraete"]:
            self.geraete_box.append(self._geraet_zeile(g, ges["w_aktuell"]))

        if self.seiten.get_visible_child_name() == "details" and self.detail_id:
            self.zeige_details(self.detail_id)

        self.punkt.set_css_classes(["pm-punkt-schlecht" if anzeige["veraltet"]
                                    else "pm-punkt-warn" if anzeige["fehler"] else "pm-punkt-ok"])
        self.status_text.set_text(_alt(anzeige["alt_s"]) if not anzeige["veraltet"]
                                  else "Sammler liefert keine neuen Zeilen")
        self.status_seit.set_text(" · ".join(anzeige["fehler"])[:60])


def _quellenzeile(name: str, watt: float, anteil: float, platz: int = -1) -> Gtk.Widget:
    kaste = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
    reihe = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
    reihe.append(Gtk.Label(label=name, css_classes=["pm-zeile"], xalign=0, hexpand=True,
                           ellipsize=Pango.EllipsizeMode.END))
    reihe.append(Gtk.Label(label=f"{_zahl(watt)} W", css_classes=["pm-zahl"], xalign=1, wrap=False))
    kaste.append(reihe)
    kaste.append(Streifen(anteil, FARBEN[_farbindex(name)]))
    return kaste


class Anwendung(Adw.Application):
    def __init__(self, pfade, detail: str = "", bereich: str = "h") -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)
        self.pfade = pfade
        self.detail_start = detail
        self.bereich_start = bereich
        self.fenster = None

    def do_startup(self):
        Adw.Application.do_startup(self)
        for name, handler in (("beenden", lambda *_: self.quit()),
                              ("aktualisieren", lambda *_: self.fenster and self.fenster.aktualisiere(erzwingen=True)),
                              ("zurueck", lambda *_: self.fenster and self.fenster.seite("ueberblick"))):
            eigenschaft = Gio.SimpleAction.new(name, None)
            eigenschaft.connect("activate", handler)
            self.add_action(eigenschaft)
        anbieter = Gtk.CssProvider()
        anbieter.load_from_data(CSS.encode("utf-8"))
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), anbieter, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def do_activate(self):
        if self.fenster is None:
            self.fenster = Fenster(self, self.pfade, self.bereich_start)
            GLib.timeout_add_seconds(TICK_S, self._tick)
            if self.detail_start:      # --detail: direkt auf dieser Geraeteseite oeffnen
                self.fenster.zeige_details(self.detail_start)
        self.fenster.present()

    def _tick(self):
        try:
            if self.fenster is not None:
                self.fenster.aktualisiere()
        except Exception as e:  # Anzeige darf an Daten nicht zerbrechen
            print(f"Anzeige: {e}", file=sys.stderr)
        return True


def run_widget(paths, detail: str = "", bereich: str = "h") -> int:
    """Anzeige starten; Rueckkehr von `Adw.Application.run` ist normales Ende."""
    anwendung = Anwendung(paths, detail, bereich)
    return anwendung.run(sys.argv[:1])
