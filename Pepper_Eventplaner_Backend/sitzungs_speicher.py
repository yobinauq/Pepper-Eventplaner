"""Einfacher Planungsspeicher OHNE Gedächtnis (Modus 2: nur Zustandsraum).

Merkt sich die Planungswerte pro Sitzung (Schlüssel = Teilnehmer-ID), aber:
  - keine Gesichtserkennung, kein Name, keine Wünsche/Themen
  - kein Vergessen (kein ACT-Up-Decay)
Dieselben Methoden wie PersonenErinnerung (stand, merke_aenderungen, lock),
damit main.py beide Speicher gleich benutzen kann.
"""
import json
import os
import threading
import time

from zustand import EINZELWERTE


class SitzungsSpeicher:
    def __init__(self, datei=None):
        self.datei = datei
        self.lock = threading.RLock()
        self.sitzungen = {}  # schluessel -> {"werte": {...}, "vorher": {...}, "zuletzt": ts}
        if datei and os.path.exists(datei):
            with open(datei, encoding="utf-8") as f:
                self.sitzungen = json.load(f)
            print(f"[Sitzung] {len(self.sitzungen)} Sitzungen geladen aus {datei}")

    def stand(self, schluessel) -> dict:
        """Planungsstand wie bei PersonenErinnerung.stand(), inkl. '<wert>_vorher'."""
        with self.lock:
            s = self.sitzungen.get(schluessel, {})
            stand = dict(s.get("werte", {}))
            for k, v in s.get("vorher", {}).items():
                if k in stand:
                    stand[k + "_vorher"] = v
            return stand

    def merke_aenderungen(self, schluessel, daten: dict):
        with self.lock:
            s = self.sitzungen.setdefault(schluessel, {"werte": {}, "vorher": {}})
            for k in EINZELWERTE:
                if daten.get(k) is None or s["werte"].get(k) == daten[k]:
                    continue
                if k in s["werte"]:
                    s["vorher"][k] = s["werte"][k]
                s["werte"][k] = daten[k]
            s["zuletzt"] = time.time()
            self.speichern()
        print(f"[Sitzung] {schluessel}: {self.stand(schluessel)}")

    def speichern(self):
        if not self.datei:
            return
        with self.lock:
            tmp = self.datei + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.sitzungen, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.datei)
