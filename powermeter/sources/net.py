"""Netzwerk-Steckdosen / -Meter per HTTP. Damit lassen sich mehrere Geraete
sammeln, ohne dass auf jedem Rechner ein Logger laeuft.

Beispiele (URL + JSON-Pfad in devices.json):

  Shelly 1PM/Plus (Gen1)   http://shelly1/                      pfad: meters.0.power
  Shelly (Gen2/Gen3, RPC)  http://shelly2/rpc/Status.Power?id=0  pfad: power
  Tasmota                   http://tasmota1/cm?cmnd=Status%2011  pfad: StatusSNS.ENERGY.Power
  irgendein JSON            http://host/meter                   pfad: watt
  Textantwort (Prometheus) http://host/metrics                  regex: power_watts\\s+([0-9.]+)

Keine Abhaengigkeiten: urllib + json aus der Standardbibliothek.
"""
from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.request

from ..util import log_error
from .base import Source, SourceError


def dig(obj, path: str):
    """'meters.0.power' durch einen JSON-Baum folgen; None wenn nicht vorhanden."""
    cur = obj
    for part in path.split("."):
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            if part in cur:
                cur = cur[part]
            else:
                return None
        else:
            return None
    return cur


class HttpPower(Source):
    kind, key = "http", "http"
    #: Netz-Meter werden hoechstens alle read_interval_s abgefragt (Gerat schont,
    #: Messwert bleibt bis dahin stehen)
    read_interval = 30.0

    def __init__(self, spec=None):
        v = getattr(spec, "value", {}) if spec else {}
        self.url = str(v.get("url") or "")
        if not self.url.startswith(("http://", "https://")):
            raise SourceError(f"http-Quelle braucht 'url' mit http(s)://, ist: {self.url!r}")
        self.pfad = str(v.get("pfad") or "")
        self.regex = str(v.get("regex") or "")
        if not self.pfad and not self.regex:
            raise SourceError("http-Quelle braucht 'pfad' (JSON) oder 'regex' (Text)")
        self.faktor = float(v.get("faktor", 1.0))
        self.timeout = float(v.get("timeout_s", 3.0))
        self.read_interval = float(v.get("intervall_s", self.read_interval))
        self.key = str(v.get("key") or self.key)
        self.label = (spec.bezeichnung if spec and spec.bezeichnung else self.key)
        self.headers = {"User-Agent": "powermeter"}
        auth = str(v.get("auth") or "")
        if auth:
            token = base64.b64encode(auth.encode()).decode()
            self.headers["Authorization"] = f"Basic {token}"
        for k, val in (v.get("header") or {}).items():
            self.headers[str(k)] = str(val)

    def read(self):
        req = urllib.request.Request(self.url, headers=self.headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read(200_000).decode("utf-8", "replace")
        except (urllib.error.URLError, OSError, ValueError) as e:
            log_error(f"http-Quelle {self.url}: {e}")
            return None
        if self.regex:
            m = re.search(self.regex, raw)
            if not m:
                log_error(f"http-Quelle {self.url}: regex kein Treffer")
                return None
            val = m.group(1) if m.groups() else m.group(0)
        else:
            try:
                data = json.loads(raw)
            except ValueError as e:
                log_error(f"http-Quelle {self.url}: kein JSON ({e})")
                return None
            val = dig(data, self.pfad)
        try:
            w = float(val)
        except (TypeError, ValueError):
            log_error(f"http-Quelle {self.url}: Wert keine Zahl: {val!r}")
            return None
        return w * self.faktor
