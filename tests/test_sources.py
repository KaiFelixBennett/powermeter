"""Quellen: JSON-Pfade, HTTP, Datei, hwmon/RAPL mit gefaeckelten sysfs-Baeumen."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from powermeter.config import SourceSpec
from powermeter.sources import make_sources
from powermeter.sources.base import SourceError
from powermeter.sources.fixed import FilePower, FixedSource
from powermeter.sources.net import HttpPower, dig


def spec(typ, **kw):
    bez = kw.pop("bezeichnung", "")
    return SourceSpec(typ=typ, bezeichnung=bez, value=kw)


def test_dig_folgt_punkten_und_listen():
    data = {"meters": [{"power": 12.5}], "a": {"b": [0, {"c": "x"}]}}
    assert dig(data, "meters.0.power") == 12.5
    assert dig(data, "a.b.1.c") == "x"
    assert dig(data, "meters.5.power") is None
    assert dig(data, "fehlt") is None


def test_http_quelle_shelly_pfad_und_regex():
    antwort = {"meters": [{"power": 118.0}]}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = (json.dumps(antwort) if self.path == "/status"
                    else "metric其他 0\npower_watts 42.5\n").encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        q = HttpPower(spec("http", url=f"http://127.0.0.1:{port}/status", pfad="meters.0.power"))
        assert q.read() == pytest.approx(118.0)
        r = HttpPower(spec("http", url=f"http://127.0.0.1:{port}/text", regex=r"power_watts\s+([\d.]+)"))
        assert r.read() == pytest.approx(42.5)
        kaputt = HttpPower(spec("http", url=f"http://127.0.0.1:{port}/status", pfad="meters.9.power"))
        assert kaputt.read() is None
        faktor = HttpPower(spec("http", url=f"http://127.0.0.1:{port}/status",
                                pfad="meters.0.power", faktor=0.5))
        assert faktor.read() == pytest.approx(59.0)
    finally:
        srv.shutdown()


def test_http_quelle_ohne_url_oder_pfad():
    with pytest.raises(SourceError):
        HttpPower(spec("http", url="ftp://host/x", pfad="power"))
    with pytest.raises(SourceError):
        HttpPower(spec("http", url="http://host/x"))


def test_fest_und_datei(tmp_path):
    f = FixedSource(spec("fest", wert_watt=37, bezeichnung="Monitor"))
    assert f.read() == 37 and f.label == "Monitor"
    with pytest.raises(SourceError):
        FixedSource(spec("fest", wert_watt="zwoelf"))

    p = tmp_path / "watt.txt"
    p.write_text("  250.5  # gemittelt vom anderen Rechner\n", encoding="utf-8")
    d = FilePower(spec("datei", pfad=str(p), bezeichnung="Serverbox", intervall_s=0))
    assert d.read() == pytest.approx(250.5)
    p.write_text("keine zahl\n", encoding="utf-8")
    assert d.read() is None
    p.touch()
    alt = FilePower(spec("datei", pfad=str(p), max_alter_s=-1))
    assert alt.read() is None


# ------------------------------------------------------------------- Linux sysfs
@pytest.fixture
def fake_sysfs(tmp_path):
    """Gefaelschter /sys-Baum. powercap-Verzeichnisse enthalten Doppelpunkte und
    sind damit auf Windows nicht anlegbar – die RAPL-Tests laufen nur auf Linux."""
    from powermeter.util import IS_WINDOWS

    root = tmp_path / "sys"
    if not IS_WINDOWS:
        dom = root / "class" / "powercap" / "intel-rapl:0"
        dom.mkdir(parents=True)
        (dom / "energy_uj").write_text("1000000", encoding="utf-8")
        (dom / "max_energy_range_uj").write_text("2000000", encoding="utf-8")
        (dom / "label").write_text("package-0", encoding="utf-8")
        sub = root / "class" / "powercap" / "intel-rapl:0:0"
        sub.mkdir()
        (sub / "energy_uj").write_text("500", encoding="utf-8")
    hw = root / "class" / "hwmon" / "hwmon2"
    hw.mkdir(parents=True)
    (hw / "name").write_text("nvidia", encoding="utf-8")
    (hw / "power1_input").write_text("75000", encoding="utf-8")   # mW -> 75 W
    batt = root / "class" / "power_supply" / "BAT0"
    batt.mkdir(parents=True)
    (batt / "type").write_text("Battery", encoding="utf-8")
    (batt / "status").write_text("Discharging", encoding="utf-8")
    (batt / "power_now").write_text("12500000", encoding="utf-8")  # µW -> 12,5 W
    return root


@pytest.mark.skipif(__import__("powermeter.util", fromlist=["x"]).IS_WINDOWS,
                   reason="sysfs-Pfade mit Doppelpunkt sind auf Windows nicht anlegbar")
def test_rapl_mit_gefaelschter_sysfs_ablage(fake_sysfs, monkeypatch):
    """Watt aus Zaehlerdifferenz / Zeitspanne - mit **gesetzter Uhr**.

    Der Test schob frueher `prev_t` um 1 s zurueck und verglich dann gegen die echte
    Laufzeit der Funktion: auf langsamen CI-Runnern waren daraus 1,008 s geworden,
    0,595 W statt 0,6 W - der Test war wanduhrabhaengig und flaky.
    """
    from powermeter.sources import linux
    from powermeter.sources.linux import PowercapRapl

    uhr = [1000.0]
    monkeypatch.setattr(linux.time, "time", lambda: uhr[0])

    q = PowercapRapl(spec("rapl", sysfs=str(fake_sysfs)))
    assert "package-0" in q.label
    assert q.read() is None                      # erster Durchgang: noch kein Delta
    (fake_sysfs / "class" / "powercap" / "intel-rapl:0" / "energy_uj").write_text("1600000",
                                                                                 encoding="utf-8")
    q2 = PowercapRapl(spec("rapl", sysfs=str(fake_sysfs)))
    assert q2.domains[0][2].prev_v is None          # Unterdomain intel-rapl:0:0 nicht doppelt
    assert len(q.domains) == 1
    uhr[0] += 1.0                               # genau 1 s spaeter, Zaehler +0,6 J
    p = q.read()
    assert p == pytest.approx(0.6, abs=1e-9)
    uhr[0] += 0.5                               # 0,5 s spaeter, keine Zaehlerbewegung
    assert q.read() == pytest.approx(0.0, abs=1e-9)


def test_rapl_ueberlauf_zaehler(fake_sysfs):
    from powermeter.sources.linux import _Delta

    d = _Delta(1e-6)
    assert d.power(100.0, 200.0, 0.0) is None
    assert d.power(50.0, 200.0, 1.0) == pytest.approx(150e-6)  # Ueberlauf ueber max


def test_hwmon_und_akku(fake_sysfs):
    from powermeter.sources.linux import BatteryPower, HwmonPower

    h = HwmonPower(spec("hwmon", sysfs=str(fake_sysfs), name="nvidia"))
    assert h.read() == pytest.approx(75.0)
    with pytest.raises(SourceError):
        HwmonPower(spec("hwmon", sysfs=str(fake_sysfs), name="amdgpu"))
    b = BatteryPower(spec("batterie", sysfs=str(fake_sysfs)))
    assert b.read() == pytest.approx(12.5)


def test_make_sources_nutzt_nur_eigene_rechner():
    from powermeter.config import _parse_device

    dev = _parse_device({"id": "box", "name": "Box", "host": "andrekiste",
                         "quellen": [{"typ": "fest", "wert_watt": 10}]})
    assert make_sources(dev, "workstation") == []
    dev2 = _parse_device({"id": "box", "name": "Box", "quellen": [{"typ": "fest", "wert_watt": 10}]})
    out = make_sources(dev2, "workstation")
    assert [s.key for s in out] == ["rest"] and out[0].read() == 10


def test_quellen_keys_eindeutig():
    from powermeter.config import _parse_device

    dev = _parse_device({"id": "d", "name": "D", "quellen": [
        {"typ": "fest", "wert_watt": 5, "key": "rest"},
        {"typ": "fest", "wert_watt": 7, "key": "rest"},
        {"typ": "datei", "pfad": "/tmp/x", "key": "rest"}]})
    out = make_sources(dev, "host")
    assert [s.key for s in out] == ["rest", "rest2", "rest3"]
