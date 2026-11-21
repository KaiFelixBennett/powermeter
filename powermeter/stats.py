"""Hochrechnen und Ausgeben: was wurde wann verbraucht, und was kostet das.

Reihenfolge immer gleich:
  1. Quellen-Energie pro Geraet aufaddieren (gemessene + geschaetzte Quellen)
  2. durch Geraet-Wirkungsgrad teilen  -> Energie an der Steckdose
  3. mit Arbeitspreis multiplizieren   -> Kosten
Schritt 2 und 3 passieren bei der Abfrage, nicht beim Speichern. Aenderungen in
devices.json/config.json wirken damit rueckwirkend auf alle Summen.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from .store import Store


@dataclass
class Totals:
    sekunden: float = 0.0
    wh_steckdose: float = 0.0
    je_quelle_wh: dict = field(default_factory=dict)
    je_geraet: dict = field(default_factory=dict)   # geraet -> Totals (ohne je_geraet)

    @property
    def kwh(self) -> float:
        return self.wh_steckdose / 1000.0

    @property
    def std(self) -> float:
        return self.sekunden / 3600.0

    def watt_mittel(self) -> float:
        return self.wh_steckdose / self.sekunden * 3600 if self.sekunden else 0.0


def _eff(device) -> float:
    return device.wirkungsgrad if device else 0.9


def aggregate(store: Store, devices, geraete: list[str] | None = None,
              tag: str | None = None, inkl_puffer: bool = True) -> Totals:
    """tag=None: alles seit der ersten Messung; tag='YYYY-MM-DD': ein Kalendertag."""
    wanted = set(geraete) if geraete else None
    eff = {d.id: _eff(d) for d in devices}
    out, partial = Totals(), {}
    for row in store.rows([tag[:7]] if tag else None):
        if wanted and row.geraet not in wanted:
            continue
        if tag and row.tag != tag:
            continue
        e = eff.get(row.geraet, 0.9)
        wh_wall = row.wh / e if e else 0.0
        out.sekunden += row.sekunden
        out.wh_steckdose += wh_wall
        out.je_quelle_wh[row.quelle] = out.je_quelle_wh.get(row.quelle, 0.0) + wh_wall
        dev = out.je_geraet.get(row.geraet)
        if dev is None:
            dev = out.je_geraet[row.geraet] = Totals()
        dev.sekunden += row.sekunden
        dev.wh_steckdose += wh_wall
        dev.je_quelle_wh[row.quelle] = dev.je_quelle_wh.get(row.quelle, 0.0) + wh_wall
    if inkl_puffer:
        for geraet, quellen in store.unflushed().items():
            if wanted and geraet not in wanted:
                continue
            if tag and (store.minute() or "")[:10] != tag:
                continue
            e = eff.get(geraet, 0.9)
            ws = sum(v[0] for v in quellen.values())
            s = sum(v[1] for v in quellen.values())
            wh_wall = ws / 3600.0 / e if e else 0.0
            partial[geraet] = (s, wh_wall, {q: v[0] / 3600.0 / e for q, v in quellen.items()})
            out.sekunden += s
            out.wh_steckdose += wh_wall
            for q, v in partial[geraet][2].items():
                out.je_quelle_wh[q] = out.je_quelle_wh.get(q, 0.0) + v
            dev = out.je_geraet.setdefault(geraet, Totals())
            dev.sekunden += s
            dev.wh_steckdose += wh_wall
            for q, v in partial[geraet][2].items():
                dev.je_quelle_wh[q] = dev.je_quelle_wh.get(q, 0.0) + v
    return out


def mess_tage(store: Store, geraete: list[str] | None = None) -> set[str]:
    wanted = set(geraete) if geraete else None
    days = set()
    for row in store.rows():
        if row.sekunden > 0 and (not wanted or row.geraet in wanted):
            days.add(row.tag)
    return days


def kosten(t: Totals, devices, cfg, geraete: list[str] | None = None) -> dict:
    """Kosten zu einer Totals-Gruppe. Grundpreis nur, wenn configured."""
    price = {d.id: cfg.price_eur_kwh(d.preis_ct_kwh) for d in devices}
    eur = 0.0
    for did, dev in t.je_geraet.items():
        eur += dev.wh_steckdose / 1000.0 * price.get(did, cfg.price_eur_kwh())
    gp = 0.0
    if cfg.grundpreis_einrechnen:
        gp = cfg.grundpreis_eur_tag * max(0.0, t.sekunden / 86400.0)
    effective = eur * 1000.0 / t.wh_steckdose * 100.0 if t.wh_steckdose else cfg.strompreis_kwh_ct
    return {"eur": eur + gp, "grundpreis": gp, "ct_kwh": effective}


def summary(store: Store, devices, cfg, geraete: list[str] | None = None) -> dict:
    """Kennzahlen fuer Widget und Report: heute / gesamt / pro Messtag.

    Ohne ausdrueckliche Auswahlzaehlen alle Geraete, die irgendwo auftauchen –
    konfigurierte und solche, die nur in uebernommenen Messreihen anderer Rechner
    vorkommen (unbekannte Geraete rechnen mit Standard-Wirkungsgrad und -Preis).
    """
    ids = geraete or sorted({d.id for d in devices} | set(store.geraete_im_daten()))
    heute = dt.date.today().isoformat()
    t_day = aggregate(store, devices, ids, heute)
    t_all = aggregate(store, devices, ids)
    days = mess_tage(store, ids)
    if t_day.sekunden > 0:
        days.add(heute)
    n = max(1, len(days))
    k_day, k_all = kosten(t_day, devices, cfg, ids), kosten(t_all, devices, cfg, ids)
    return {
        "wh_tag": t_day.wh_steckdose, "wh_gesamt": t_all.wh_steckdose,
        "std_tag": t_day.std, "std_gesamt": t_all.std,
        "kosten_tag": k_day["eur"], "kosten_gesamt": k_all["eur"],
        "tage": n,
        "kwh_pro_tag": t_all.kwh / n, "kosten_pro_tag": k_all["eur"] / n,
        "std_pro_tag": t_all.std / n, "w_tag": t_day.watt_mittel(), "w_gesamt": t_all.watt_mittel(),
        "je_geraet": {gid: {"wh_gesamt": d.wh_steckdose, "std_gesamt": d.std,
                            "wh_tag": t_day.je_geraet.get(gid, Totals()).wh_steckdose}
                      for gid, d in t_all.je_geraet.items()},
        "ct_kwh": k_all["ct_kwh"], "tarif": cfg.tarif_name,
    }
