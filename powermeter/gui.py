"""Desktop-Anzeige (tkinter) – auf Windows und Linux gleich aufgebaut.

Das Fenster ist bewusst randlos und halbtransparent. Windows legt es standardmaessig
auf den Desktop (uebersteht Win+D) und per Menu auch darueber; Linux verhaelt sich
ein randloses Fenster so, wie es der Fenstermanager vorgibt (topmost optional).
Position und die gerade angezeigte Auswahl (Geraet oder alle) liegen in state.json.
"""
from __future__ import annotations

import datetime as dt
import subprocess
import sys
import time
import traceback

import tkinter as tk

from .util import IS_WINDOWS, log_error, nf, system_start

BG, FG, DIM, ACCENT = "#15171b", "#e9eaec", "#8b9099", "#ffc94d"
DASH = "–"


def _font(core_size: int) -> tuple:
    """Windows-Fonts explicit setzen, Linux die Standard-Familie nehmen."""
    if IS_WINDOWS:  # pragma: no cover - nur Windows
        fam = "Segoe UI Semibold" if core_size >= 18 else "Segoe UI"
    else:
        fam = None
    return (fam, core_size) if fam else (core_size,)


def _win_user32():
    """user32 nur auf Windows holen; auf Linux einfach None."""
    if not IS_WINDOWS:  # pragma: no cover
        return None
    if getattr(_win_user32, "_inst", None) is None:  # pragma: no cover
        try:
            import ctypes as C

            u = C.WinDLL("user32")
            u.GetSystemMetrics.argtypes = [C.c_int]
            u.GetSystemMetrics.restype = C.c_int
            _win_user32._inst = u
        except Exception as e:
            log_error(f"user32: {e}")
            return None
    return _win_user32._inst  # pragma: no cover


class Widget:
    def __init__(self, core):
        self.core, self.quit, self._vars = core, False, []
        self._user32 = _win_user32()
        r = self.root = tk.Tk()
        r.withdraw()
        r.title("powermeter")
        r.overrideredirect(True)
        r.configure(bg=BG)
        r.attributes("-alpha", core.cfg.deckkraft)
        r.protocol("WM_DELETE_WINDOW", self.close)
        try:
            r.attributes("-type", "dock")  # KDE/GNOME: kein Taskleisteneintrag
        except tk.TclError:
            pass

        f = tk.Frame(r, bg=BG, padx=12, pady=6)
        f.pack()
        top = tk.Frame(f, bg=BG)
        top.grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 3))
        self.l_now = tk.Label(top, bg=BG, fg=ACCENT, font=_font(20), padx=0, pady=0)
        self.l_now.pack(side="left")
        self.l_scope = tk.Label(top, bg=BG, fg=DIM, font=_font(8), padx=7, pady=0)
        self.l_scope.pack(side="left")

        def label(row, col, size=10, fg=FG, sticky="e", **kw):
            lb = tk.Label(f, bg=BG, fg=fg, font=_font(size), padx=0, pady=0)
            lb.grid(row=row, column=col, sticky=sticky, padx=(0, 10), **kw)
            return lb

        self.kopf = [label(1, col, 8, DIM, "e") for col in (1, 2, 3)]
        for lb, name in zip(self.kopf, ("today", "total", "per day")):
            lb.config(text=name)
        self.cells = {}
        self.namen = []
        for i, name in enumerate(("avg power", "energy", "cost", "uptime"), start=2):
            self.namen.append(label(i, 0, 8, DIM, "w"))
            self.namen[-1].config(text=name)
            self.cells[name] = tuple(label(i, col) for col in (1, 2, 3))
        self.l_parts = label(6, 0, 8, DIM, "w", columnspan=4, pady=(4, 0))
        self.l_foot = label(7, 0, 8, DIM, "w", columnspan=4, pady=(1, 0))
        # alles, was im kompakten Zweizeiler weichen muss (Knopf und Quellenzeile bleiben)
        self._tabelle = self.kopf + self.namen + [c for n in self.cells for c in self.cells[n]]

        for seq, fn in (("<ButtonPress-1>", self._press), ("<B1-Motion>", self._drag),
                        ("<ButtonRelease-1>", self._release), ("<Button-3>", self._menu)):
            r.bind(seq, fn)

        self.tick()
        r.update_idletasks()
        pos = core.store.state.get("fenster", {})
        x, y = pos.get("x"), pos.get("y")
        if x is None or y is None or not self._on_screen(x, y):
            x, y = r.winfo_screenwidth() - r.winfo_reqwidth() - 24, 24
        x = max(0, min(x, r.winfo_screenwidth() - r.winfo_reqwidth() - 4))
        y = max(0, min(y, r.winfo_screenheight() - r.winfo_reqheight() - 4))
        r.geometry(f"+{x}+{y}")
        r.deiconify()
        r.update_idletasks()
        self._apply_layer()

    # ------------------------------------------------------------- Darstellung
    def scope(self) -> list[str] | None:
        gewaehlt = self.core.store.state.get("fenster", {}).get("geraet")
        ids = self.core.alle_geraete()
        return [gewaehlt] if gewaehlt and gewaehlt in ids else None

    def _kurzteil(self, w_now: float) -> str:
        """Drei Zeilen, wenn nur ein Geraet gemeint ist - der Knopf zeigt dasselbe.

        Die Tabelle unter einem einzelnen Leistungs­wert ist Zahlensalat: sie warnt
        davor und die Quellenzeile darunter zeigt ohnehin dieselbe Zerlegung.
        """
        fen = self.core.store.state.get("fenster", {})
        scope = fen.get("geraet") or "__laufend__"
        alle = self.core.alle_geraete()
        kurz = (scope == "__laufend__" and len(alle) <= 1) and fen.get("detail") is not True
        fen["kurz"] = kurz
        self.l_now.config(text=("  " if kurz else "") + f"{nf(w_now)} W")
        return scope

    def tick(self):
        try:
            self.core.step()
            self.render()
        except Exception:
            log_error(traceback.format_exc())
        self.root.after(int(self.core.cfg.intervall_sekunden * 1000), self.tick)

    def render(self):
        core, cfg = self.core, self.core.cfg
        kurzel = self._kurzteil(core.watt_gesamt(None))
        ids = self.scope()
        k = core.summary(60.0, ids)
        names = [core.kennung(i) for i in (ids or core.alle_geraete())]
        self.l_scope.config(text=("· " + " + ".join(names)) if names else "· no devices")

        a_day = k["wh_tag"] / k["std_tag"] if k["std_tag"] else 0.0
        a_all = k["wh_gesamt"] / k["std_gesamt"] if k["std_gesamt"] else 0.0
        val = {
            "avg power": (f"{nf(a_day)} W" if a_day else DASH,
                          f"{nf(a_all)} W" if a_all else DASH, DASH),
            "energy": (f"{nf(k['wh_tag'] / 1000, 2)} kWh", f"{nf(k['wh_gesamt'] / 1000, 2)} kWh",
                       f"{nf(k['kwh_pro_tag'], 2)} kWh"),
            "cost": (f"{nf(k['kosten_tag'], 2)} €", f"{nf(k['kosten_gesamt'], 2)} €",
                     f"{nf(k['kosten_pro_tag'], 2)} €"),
            "uptime": (f"{nf(k['std_tag'], 1)} h", f"{nf(k['std_gesamt'], 1)} h",
                       f"{nf(k['std_pro_tag'], 1)} h"),
        }
        for name, texts in val.items():
            for lb, txt in zip(self.cells[name], texts):
                lb.config(text=txt)

        scope = ids or core.device_ids()
        if len(scope) == 1:
            # ein Geraet im Bild: die einzelnen Quellen zeigen (CPU · GPU · Rest …)
            parts = [f"{lab} {nf(w)} W" for lab, w in core.parts(scope[0])]
        else:
            # mehrere Geraete: pro Geraet die Steckdosenleistung zeigen
            parts = [f"{core.kennung(g)} {nf(core.watt_geraet(g))} W" for g in scope]
        kompakt = core.store.state.get("fenster", {}).get("kurz")
        if kompakt:
            # Zweizeiler: Zahl und Zerlegung, keine Zahlenwiederholung in Tabellenform
            for lb in self._tabelle + [self.l_parts, self.l_foot]:
                lb.grid_forget()
            self.l_scope.config(text="· " + " · ".join(parts))
        else:
            for lb in self._tabelle:
                lb.grid()
            self.l_parts.grid()
            self.l_foot.grid()
            ende = "measured" if len(scope) == 1 else "at the wall"
            self.l_parts.config(text=" · ".join(parts) + " " + ende)

        start = dt.datetime.fromtimestamp(system_start())
        foot = (f"boot {start:%H:%M}" if start.date() == dt.date.today()
                else f"boot {start:%d.%m. %H:%M}")
        seit = core.store.state.get("seit") or ""
        if seit:
            foot += f" · total since {dt.date.fromisoformat(seit[:10]):%d.%m.%y}"
        tage = f"{k['tage']} measured day" + ("s" if k['tage'] != 1 else "")
        foot += f" · {tage} · {nf(k['ct_kwh'], 2)} ct/kWh"
        if cfg.grundpreis_einrechnen:
            foot += " · incl. base fee"
        self.l_foot.config(text=foot)

    # ------------------------------------------------------- Fenster-Ebene (Win)
    def _apply_layer(self):
        fen = self.core.store.state.setdefault("fenster", {})
        vorne = bool(fen.get("vorne"))
        self.root.attributes("-topmost", vorne)
        if not IS_WINDOWS:  # pragma: no cover - nur Windows
            return
        try:  # pragma: no cover - Windows-spezifisch
            import ctypes as C

            user32 = C.WinDLL("user32")
            user32.FindWindowW.argtypes = [C.c_wchar_p, C.c_wchar_p]
            user32.FindWindowW.restype = C.c_void_p
            user32.GetWindowLongW.argtypes = [C.c_void_p, C.c_int]
            user32.SetWindowLongW.argtypes = [C.c_void_p, C.c_int, C.c_long]
            user32.SetWindowLongPtrW.argtypes = [C.c_void_p, C.c_int, C.c_void_p]
            user32.SetWindowLongPtrW.restype = C.c_void_p
            user32.GetSystemMetrics.argtypes = [C.c_int]
            hwnd = int(self.root.wm_frame(), 16)
            ex = user32.GetWindowLongW(hwnd, -20)  # GWL_EXSTYLE
            user32.SetWindowLongW(hwnd, -20, (ex | 0x80) & ~0x40000)  # +TOOLWINDOW, -APPWINDOW
            user32.SetWindowLongPtrW(hwnd, -8, None if vorne else user32.FindWindowW("Progman", None))
            pref = C.c_int(2)  # DWMWCP_ROUND (Windows 11)
            C.windll.dwmapi.DwmSetWindowAttribute(C.c_void_p(hwnd), 33, C.byref(pref), 4)
            self._user32 = user32
        except Exception as e:
            log_error(f"Fensterebene: {e}")

    def _on_screen(self, x, y) -> bool:
        if self._user32 is not None:  # pragma: no cover - nur Windows
            try:
                m = self._user32.GetSystemMetrics
                vx, vy, vw, vh = m(76), m(77), m(78), m(79)  # virtueller Bildschirm
                return vx <= x < vx + vw - 40 and vy <= y < vy + vh - 20
            except Exception:
                pass
        return 0 <= x < self.root.winfo_screenwidth() - 40 and 0 <= y < self.root.winfo_screenheight() - 20

    # ------------------------------------------------------------------- Maus
    def _press(self, e):
        self._off = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def _drag(self, e):
        self.root.geometry(f"+{e.x_root - self._off[0]}+{e.y_root - self._off[1]}")

    def _release(self, _e):
        self.core.store.state.setdefault("fenster", {}).update(
            x=self.root.winfo_x(), y=self.root.winfo_y())
        self.core.store.save()

    # ------------------------------------------------------------------- Menu
    def _menu(self, e):
        st = self.core.store.state
        fen = st.setdefault("fenster", {})
        vorne = tk.BooleanVar(self.root, bool(fen.get("vorne")))
        auto = tk.BooleanVar(self.root, _autostart_get_safe())
        self._vars = [vorne, auto]
        k = self.core.summary(0.0, self.scope())

        m = tk.Menu(self.root, tearoff=False)
        m.add_command(label=f"uptime: today {nf(k['std_tag'], 1)} h · total {nf(k['std_gesamt'], 1)} h "
                            f"· avg {nf(k['std_pro_tag'], 1)} h/day "
                            f"({k['tage']} measured day{'s' if k['tage'] != 1 else ''})",
                      state="disabled")
        m.add_command(label=f"cost: today {nf(k['kosten_tag'], 2)} € · total {nf(k['kosten_gesamt'], 2)} € "
                            f"({nf(k['ct_kwh'], 2)} ct/kWh)", state="disabled")
        if self.core.cfg.tarif_name:
            m.add_command(label=f"tariff: {self.core.cfg.tarif_name}", state="disabled")
        m.add_separator()

        aus = st.setdefault("aus", {"typ": [], "geraet": []})
        sichtbar = (ids[0] if (ids and len(ids) == 1)
                    else (self.core.measurable()[:1] or [None])[0])
        for typ, label in (self.core.stummquellen(sichtbar) if sichtbar else []):
            m.add_checkbutton(label=f"{label} ({typ})",
                              variable=tk.BooleanVar(self.root, typ not in aus["typ"]),
                              command=lambda t=typ: self._umschalten("typ", t))
        if not sichtbar:
            m.add_command(label="sources: no live metering here", state="disabled")
        m.add_separator()

        def set_vorne():
            fen["vorne"] = vorne.get()
            self.core.store.save()
            self._apply_layer()

        def set_geraet(gid):
            fen["geraet"] = gid or ""
            self.core.store.save()
            self.render()

        def _umschalten(bereich, schluessel):
            an = schluessel in self.core.store.stumme()[bereich]
            self.core.store.stumm_setzen(bereich, schluessel, not an)
            self.core._sync_stumm()
            self.core.detect()
            self.render()

        def set_auto():
            try:
                from .autostart import autostart_set

                autostart_set(auto.get())
            except OSError as ex:
                log_error(f"Autostart: {ex}")

        def offnen(pfad):
            try:
                if IS_WINDOWS:  # pragma: no cover
                    import os

                    os.startfile(str(pfad))
                else:
                    for prog in ("xdg-open", "open"):
                        try:
                            subprocess.Popen([prog, str(pfad)])
                            return
                        except OSError:
                            continue
                    log_error(f"kein Oeffnungsprogramm fuer {pfad}")
            except OSError as ex:
                log_error(f"oeffnen {pfad}: {ex}")

        unterm = tk.Menu(m, tearoff=False)
        unterm.add_command(label="all devices", command=lambda: set_geraet(""))
        alle = self.core.alle_geraete()
        if len(alle) <= 1:
            unterm.add_command(label=f"currently running – {self.core.kennung(alle[0])}"
                               if alle else "currently running",
                               command=lambda: set_geraet("__laufend__"))
        for did in alle:
            unterm.add_command(label=f"{did} – {self.core.kennung(did)}",
                               command=lambda g=did: set_geraet(g))
        m.add_cascade(label="view", menu=unterm)
        detail = tk.BooleanVar(self.root, fen.get("detail") is True)
        self._vars.append(detail)

        def set_detail():
            fen["detail"] = detail.get()
            fen.pop("kurz", None)
            self.core.store.save()
            self.render()
        m.add_checkbutton(label="detailed table", variable=detail, command=set_detail)
        m.add_checkbutton(label="always on top", variable=vorne, command=set_vorne)
        m.add_checkbutton(label="start at login", variable=auto, command=set_auto)
        m.add_command(label="open data folder", command=lambda: offnen(self.core.paths.data))
        m.add_command(label="config.json", command=lambda: offnen(self.core.paths.config))
        m.add_command(label="devices.json", command=lambda: offnen(self.core.paths.devices))
        schaltbar = self.core.device_ids()
        if len(schaltbar) > 1:
            for did in schaltbar:
                m.add_checkbutton(label=f"measure {did}",
                                  variable=tk.BooleanVar(self.root, did not in aus["geraet"]),
                                  command=lambda g=did: _umschalten("geraet", g))
        m.add_separator()
        m.add_command(label="quit", command=self.close)
        m.tk_popup(e.x_root, e.y_root)

    def close(self):
        self.quit = True
        self.root.destroy()


def _autostart_get_safe() -> bool:
    try:
        from .autostart import autostart_get

        return autostart_get()
    except Exception as e:
        log_error(f"Autostart-Status: {e}")
        return False


def run_widget(core) -> int:
    """Widget bauen und laufen lassen; stuerzt das Fenster ab, wird es neu aufgebaut."""
    first = True
    try:
        while True:
            try:
                ui = Widget(core)
                ui.root.mainloop()
                if ui.quit:
                    break
            except Exception:
                log_error(f"Widget-Fenster abgestuerzt:\n{traceback.format_exc()}")
                if first:
                    raise
            first = False
            time.sleep(3)  # Fenster von aussen zerstoesst (Explorer-Neustart) -> neu aufbauen
    finally:
        core.close()
    return 0
