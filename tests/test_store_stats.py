"""Datenspeicher und Hochrechnung: alt gegen neu, Wirkungsgrad, Preise, Grundpreis."""
import json

import pytest

from powermeter.config import Config, Device, load_devices, resolve_paths
from powermeter.stats import aggregate, kosten, mess_tage, summary
from powermeter.store import HEAD, Row, Store, read_rows

V1 = ("zeit,sekunden,cpu_w,nvidia_w,amd_w,rest_w,netzteil_wirkungsgrad,gesamt_w\n"
      "2026-11-07 17:16,20.4,30.5,32.4,1.1,30,0.9,104.4\n"
      "2026-11-07 17:17,54.1,29.6,32.4,1.0,30,0.9,103.3\n")


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setenv("POWERMETER_DATA", str(tmp_path / "daten"))
    return resolve_paths(str(tmp_path / "daten"))


def devs(*specs):
    from powermeter.config import _parse_device

    return [_parse_device(s) for s in specs]


def pc(eta=0.9, preis=None):
    d = {"id": "pc", "name": "PC", "wirkungsgrad": eta, "quellen": [{"typ": "auto"}]}
    if preis is not None:
        d["preis_ct_kwh"] = preis
    return d


def test_v2_roundtrip_und_summen(paths):
    st = Store(paths)
    st.add("pc", {"cpu": 100.0, "rest": 20.0}, 30.0, now=1_700_000_000)
    st.add("pc", {"cpu": 60.0, "rest": 20.0}, 30.0, now=1_700_000_030)
    st.flush()
    rows = list(read_rows(paths.data / "2023-11.csv", "pc"))
    assert [r.quelle for r in rows] == ["cpu", "rest"]
    assert rows[0].sekunden == 60.0 and rows[0].watt == pytest.approx(80.0)   # (3000+1800)/60
    assert st.state["geraete"]["pc"]["quellen"]["cpu"] == pytest.approx((100 * 30 + 60 * 30) / 3600)
    # state.json neu geladen == dieselben Summen
    wieder = Store(paths)
    assert wieder.state["geraete"]["pc"]["quellen"] == st.state["geraete"]["pc"]["quellen"]


def test_v1_altdaten_werden_als_lange_zeilen_gelesen(paths):
    (paths.data).mkdir(parents=True, exist_ok=True)
    (paths.data / "2026-11.csv").write_text(V1, encoding="utf-8")
    rows = list(read_rows(paths.data / "2026-11.csv", "pc"))
    assert {(r.geraet, r.quelle) for r in rows} == {("pc", "cpu"), ("pc", "nvidia"),
                                                   ("pc", "amd"), ("pc", "rest")}
    assert sum(r.watt for r in rows if r.quelle == "cpu") == pytest.approx(60.1)


def test_aggregag_rechnet_wirkungsgrad_und_preise():
    store_rows = [Row("2026-11-21 10:00", "pc", "cpu", 3600.0, 100.0),
                  Row("2026-11-21 10:00", "kuhl", "dose", 3600.0, 150.0)]

    class FakeStore:
        def rows(self, months=None):
            return iter(store_rows)

        def unflushed(self):
            return {}

        def minute(self):
            return None

    d = devs(pc(eta=0.8), {"id": "kuhl", "name": "Kühlschrank", "wirkungsgrad": 1.0,
                           "preis_ct_kwh": 20.0, "quellen": [{"typ": "http"}]})
    cfg = Config(strompreis_kwh_ct=30.0)
    t = aggregate(FakeStore(), d, tag="2026-11-21")
    assert t.je_geraet["pc"].wh_steckdose == pytest.approx(125.0)   # 100 Wh / 0.8
    assert t.je_geraet["kuhl"].wh_steckdose == pytest.approx(150.0)
    k = kosten(t, d, cfg)
    assert k["eur"] == pytest.approx(0.125 * 0.30 + 0.150 * 0.20)
    assert k["ct_kwh"] == pytest.approx((k["eur"] * 1000 / t.wh_steckdose) * 100)


def test_summary_heute_gesamt_pro_tag(tmp_path, monkeypatch):
    monkeypatch.setenv("POWERMETER_DATA", str(tmp_path / "daten"))
    paths = resolve_paths(str(tmp_path / "daten"))
    cfgdir = paths.config.parent
    cfgdir.mkdir(parents=True, exist_ok=True)
    (cfgdir / "devices.json").write_text(json.dumps(
        {"geraete": [pc(), {"id": "pc2", "name": "Zweitgeraet", "host": "anderer",
                            "quellen": [{"typ": "fest", "wert_watt": 50}]}]},
        indent=1), encoding="utf-8")
    st = Store(paths)
    gestern = "2026-11-20 12:00"
    st.add("pc", {"cpu": 200.0}, 3600.0, now=1_763_000_000)   # 2025-11-13 UTC, Datum lokal egal
    st.flush()
    with (paths.data / "2026-11.csv").open("a", encoding="utf-8") as fh:
        fh.write(",".join(HEAD) + "\n")
        fh.write("\n".join([",".join([gestern, "pc", "cpu", "3600.0", "100.0"]),
                            ",".join([gestern, "pc2", "dose", "3600.0", "50.0"])]) + "\n")
    st.state = st._rebuild_state()
    d = load_devices(paths).devices
    s = summary(st, d, Config(strompreis_kwh_ct=30.0))
    assert s["tage"] >= 2
    assert s["wh_gesamt"] > s["wh_tag"]
    assert set(s["je_geraet"]) == {"pc", "pc2"}
    assert mess_tage(st) >= {"2026-11-20"}


def test_grundpreis_nur_wenn_angehabt():
    class FakeStore:
        def rows(self, months=None):
            return [Row("2026-11-21 10:00", "pc", "cpu", 86400.0, 100.0)]

        def unflushed(self):
            return {}

        def minute(self):
            return None

    d = devs(pc())
    st = FakeStore()
    t = aggregate(st, d)
    aus = kosten(t, d, Config(grundpreis_einrechnen=False))
    an = kosten(t, d, Config(grundpreis_einrechnen=True, grundpreis_eur_tag=0.50))
    assert an["grundpreis"] == pytest.approx(0.50)
    assert aus["grundpreis"] == 0.0
    assert an["eur"] - aus["eur"] == pytest.approx(0.50)


def test_unbekannte_geraete_aus_fremden_messreihen_zaehlen_mit(tmp_path, monkeypatch):
    """Geraete, die nur in uebernommenen Reihen anderer Rechner vorkommen, nicht verstecken."""
    monkeypatch.setenv("POWERMETER_DATA", str(tmp_path / "daten"))
    paths = resolve_paths(str(tmp_path / "daten"))
    paths.config.parent.mkdir(parents=True, exist_ok=True)
    (paths.config.parent / "devices.json").write_text(json.dumps(
        {"geraete": [pc()]}, indent=1), encoding="utf-8")
    st = Store(paths)
    st.add("pc", {"cpu": 100.0}, 3600.0, now=1_763_000_000)
    st.flush()
    with (paths.data / "2026-12.csv").open("a", encoding="utf-8") as fh:
        fh.write(",".join(HEAD) + "\n")
        fh.write("2026-12-01 10:00,otherbox,rest,3600.0,80.0\n")
    st.state = st._rebuild_state()
    s = summary(st, load_devices(paths).devices, Config(strompreis_kwh_ct=30.0))
    assert {"pc", "otherbox"} <= set(s["je_geraet"])
    assert s["wh_gesamt"] > 100 / 0.9 + 80 - 1        # beide Geraete gerechnet
