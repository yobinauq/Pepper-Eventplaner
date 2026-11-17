"""Personen-Gedächtnis für den Pepper-Eventplaner.

Ablauf: Bild -> vorderstes Gesicht (UniFace) -> person_id (PersonenRegister)
        -> Planungsstand der person_id (ACT-Up)

Jeder Chunk hat dieselbe Form:
    {"person": "person_ab12", "attribut": "gaeste", "wert": 80}
Einzelwerte (FAKTEN): der zuletzt gelernte aktive Chunk gilt.
Listen (wunsch, thema): alle aktiven Chunks gelten; "wunsch_entfernt" streicht einen Wunsch.

Gespeichert wird nach jeder Änderung in ZWEI JSON-Dateien, die beim Start geladen werden:
  ACT-Up-Datei     (z.B. actup_gedaechtnis.json): alle Chunks = Erinnerungen, lesbar,
                   ohne biometrische Daten -> für Auswertung/Studie
  Personen-Datei   (z.B. personen_register.json): Gesichts-Embeddings + Namen -> vertraulich
"""

import base64
import json
import math
import os
import threading
import time
import uuid

import cv2
import numpy as np
import pyactup
from uniface import FaceAnalyzer

analyzer = FaceAnalyzer()

# Mindestgröße der Bounding Box. 10000 passt bei Pepper für 1-1.5 m Abstand,
# Personen ab ~1.5 m werden ignoriert (12.10.26)
MIN_GESICHTSFLAECHE = 10000

# Ab dieser Kosinus-Ähnlichkeit gilt ein Gesicht als dieselbe Person
AEHNLICHKEITS_SCHWELLWERT = 0.40

# Nach so vielen Minuten ohne Wiederholung fällt ein einmal gelernter Chunk unter den Threshold
VERGESSEN_NACH_MINUTEN = 20160

# Personen, die so lange nicht gesehen wurden, werden aus dem Register gelöscht
PERSON_LOESCHEN_NACH_MINUTEN = 20160

# Chunk-Tabelle nach jeder gemerkten Änderung ausgeben
CHUNK_AUSGABE = True

# Einzelwerte des Planungsstands (es gilt immer der neueste)
FAKTEN = ["event_typ", "datum", "ort", "gaeste", "budget"]


def bild_aus_base64(b64):
    """Base64-String von Pepper -> OpenCV-Bild (None, wenn ungültig)."""
    arr = np.frombuffer(base64.b64decode(b64), dtype=np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


def ahnlichkeit(e1, e2):
    return np.dot(e1, e2) / (np.linalg.norm(e1) * np.linalg.norm(e2))


def bbox_flaeche(bbox):
    x1, y1, x2, y2 = bbox
    return (x2 - x1) * (y2 - y1)


def groesstes_gesicht(bild, min_flaeche=MIN_GESICHTSFLAECHE):
    """Größtes (= vorderstes) Gesicht oder None."""
    gesichter = list(analyzer.analyze(bild))
    if not gesichter:
        print("[Person] kein Gesicht im Bild gefunden")
        return None
    groesstes = max(gesichter, key=lambda g: bbox_flaeche(g.bbox))
    flaeche = bbox_flaeche(groesstes.bbox)
    if flaeche < min_flaeche:
        print(f"[Person] Gesicht zu klein: flaeche={flaeche:.0f} "
              f"Minimum {min_flaeche}, {len(gesichter)} Gesichter im Bild")
        return None
    return groesstes


class PersonenRegister:
    """Ordnet Gesichts-Embeddings einer person_id zu."""

    def __init__(self, schwellwert=AEHNLICHKEITS_SCHWELLWERT):
        self.schwellwert = schwellwert
        self.personen = {}  # pid -> {"embedding", "zuletzt", "name"}

    def identifiziere(self, embedding):
        """-> (person_id, ähnlichkeit, neu, zuletzt_gesehen_vorher)"""
        bester_id, beste = None, -1.0
        for pid, daten in self.personen.items():
            s = ahnlichkeit(embedding, daten["embedding"])
            if s > beste:
                bester_id, beste = pid, s

        if bester_id is not None and beste >= self.schwellwert:
            vorher = self.personen[bester_id]["zuletzt"]
            self.personen[bester_id]["zuletzt"] = time.time()
            return bester_id, beste, False, vorher

        neue_id = f"person_{uuid.uuid4().hex[:6]}"
        self.personen[neue_id] = {"embedding": embedding, "zuletzt": time.time()}
        return neue_id, beste, True, None

    def setze_name(self, person_id, name):
        if person_id in self.personen:
            self.personen[person_id]["name"] = name

    def name_von(self, person_id):
        daten = self.personen.get(person_id)
        return daten.get("name") if daten else None

    def vergiss_alte(self, max_alter_sekunden):
        grenze = time.time() - max_alter_sekunden
        alte = [pid for pid, d in self.personen.items() if d["zuletzt"] < grenze]
        for pid in alte:
            del self.personen[pid]
        return alte


class PersonenGedachtnis:
    """ACT-Up-Gedächtnis mit Base-Level-Decay (Zeit in Minuten)."""

    def __init__(self, vergessen_nach_minuten=VERGESSEN_NACH_MINUTEN, noise=0):
        self.m = pyactup.Memory()
        self.m.noise = noise  # 0 = Vergessen ist vorhersehbar
        # Ein einmal gelernter Chunk fällt genau nach vergessen_nach_minuten unter den Threshold
        self.m.threshold = -self.m.decay * math.log(vergessen_nach_minuten)
        self.start = time.time()
        self.zeit_offset = 0.0  # für Tests: simuliertes Verstreichen von Zeit

    def zeit_synchronisieren(self):
        """Memory-Zeit auf die echte Uhr (Minuten) nachziehen, immer ein Schritt > 0."""
        jetzt = (time.time() - self.start) / 60 + self.zeit_offset
        self.m.advance(max(jetzt - self.m.time, 0.001))

    def zeit_verblassen(self, minuten):
        """Lässt Zeit vergehen - zum Testen des Decays."""
        self.zeit_offset += minuten
        self.zeit_synchronisieren()

    def aktivierung(self, chunk):
        """B = ln( Σ (t - t_j)^(-d) ), entspricht ACT-Up bei Noise = 0."""
        summe = sum(max(self.m.time - t_j, 1e-6) ** (-self.m.decay) for t_j in chunk.references)
        return math.log(summe)

    def merke_fakt(self, person_id, attribut, wert):
        self.zeit_synchronisieren()
        self.m.learn({"person": person_id, "attribut": attribut, "wert": wert})

    def _chunks(self, person_id, attribut):
        """Alle Chunks dieser Person/dieses Attributs, zuletzt gelernte zuerst."""
        self.zeit_synchronisieren()
        k = [c for c in self.m.chunks
             if c.get("person") == person_id and c.get("attribut") == attribut]
        k.sort(key=lambda c: max(c.references), reverse=True)
        return k

    def _ist_aktiv(self, chunk):
        return self.aktivierung(chunk) >= self.m.threshold

    def _aktive(self, person_id, attribut):
        """Nur die Chunks über dem Threshold, zuletzt gelernte zuerst."""
        return [c for c in self._chunks(person_id, attribut) if self._ist_aktiv(c)]

    def aktueller_wert(self, person_id, attribut):
        """Für Einzelwerte (Gäste, Budget ...) -> (neu, vorher).

        Es gilt immer der ZULETZT gelernte Wert - bewusst nicht m.retrieve(), denn
        das nimmt den aktivsten Chunk, und ein alter, oft wiederholter Wert kann
        aktiver sein als eine neue Änderung. Ist der neueste Wert vergessen, gilt das
        Attribut als vergessen; ein älterer Wert taucht dann NICHT wieder auf."""
        k = self._chunks(person_id, attribut)
        if not k or not self._ist_aktiv(k[0]):
            return None, None
        vorher = k[1]["wert"] if len(k) > 1 and self._ist_aktiv(k[1]) else None
        return k[0]["wert"], vorher

    def aktive_liste(self, person_id, attribut, max_anzahl=5):
        """Für Listen (Wünsche, Themen). Ein danach entfernter Eintrag zählt nicht mehr."""
        entfernt = {c["wert"]: max(c.references) for c in self.m.chunks
                    if c.get("person") == person_id and c.get("attribut") == attribut + "_entfernt"}
        return [c["wert"] for c in self._aktive(person_id, attribut)
                if max(c.references) > entfernt.get(c["wert"], -1)][:max_anzahl]


class PersonenErinnerung:
    """Register + Gedächtnis + Speicherung. Thread-sicher (FastAPI arbeitet parallel)."""

    def __init__(self, actup_datei=None, personen_datei=None):
        self.register = PersonenRegister()
        self.gedachtnis = PersonenGedachtnis()
        self.lock = threading.RLock()  # RLock: speichern() darf auch innerhalb des Locks laufen
        self.actup_datei = actup_datei
        self.personen_datei = personen_datei
        self.laden()

    # ---------- Erkennen ----------

    def erkenne(self, bild_b64):
        """Base64-Bild -> (person_id, neu, zuletzt_gesehen_vorher).
        (None, None, None), wenn niemand nah genug vorne steht."""
        bild = bild_aus_base64(bild_b64)
        if bild is None:
            return None, None, None

        gesicht = groesstes_gesicht(bild)
        if gesicht is None:
            print("[Person] niemand steht nah genug vor Pepper")
            return None, None, None

        with self.lock:
            self.register.vergiss_alte(PERSON_LOESCHEN_NACH_MINUTEN * 60)
            pid, sim, neu, vorher = self.register.identifiziere(gesicht.embedding)
            name = self.register.name_von(pid)
            self.speichern()

        print(f"[Person] {pid} name={name!r} neu={neu} aehnlichkeit={sim:.3f} "
              f"flaeche={bbox_flaeche(gesicht.bbox):.0f}")
        return pid, neu, vorher

    # ---------- Planungsstand ----------

    def stand(self, person_id):
        """Aktueller Planungsstand einer Person als Dict - das geht an GPT."""
        with self.lock:
            g = self.gedachtnis
            s = {"name": self.register.name_von(person_id)}
            for a in FAKTEN:
                neu, alt = g.aktueller_wert(person_id, a)
                if neu is not None:
                    s[a] = neu
                    if alt is not None:
                        s[a + "_vorher"] = alt
            s["wuensche"] = g.aktive_liste(person_id, "wunsch")
            s["themen"] = g.aktive_liste(person_id, "thema", 3)
        return s

    def merke_aenderungen(self, person_id, daten):
        """daten = bereinigtes Ergebnis von analysiere_aussage() (ohne name)."""
        with self.lock:
            g = self.gedachtnis
            for a in FAKTEN:
                if daten.get(a) is not None:
                    g.merke_fakt(person_id, a, daten[a])
            for w in daten.get("wuensche_neu", []):
                g.merke_fakt(person_id, "wunsch", w)
            for w in daten.get("wuensche_entfernt", []):
                g.merke_fakt(person_id, "wunsch_entfernt", w)
            if daten.get("thema"):
                g.merke_fakt(person_id, "thema", daten["thema"])
            self.speichern()
        if CHUNK_AUSGABE:
            self.chunk_ausgabe()

    def setze_name(self, person_id, name):
        with self.lock:
            self.register.setze_name(person_id, name)
            self.speichern()

    # ---------- Speichern / Laden ----------

    @staticmethod
    def _schreibe_text(pfad, text):
        tmp = pfad + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, pfad)  # erst fertig schreiben, dann ersetzen -> Datei nie halb kaputt

    def _schreibe_json(self, pfad, daten, einrueckung=None):
        self._schreibe_text(pfad, json.dumps(daten, ensure_ascii=False, indent=einrueckung))

    def _schreibe_register(self, pfad):
        """Eine Zeile pro Person: Name und Zeitpunkt vorne, Embedding hinten.
        Bleibt gültiges JSON, laden() liest es unverändert."""
        zeilen = []
        for pid, d in self.register.personen.items():
            eintrag = {
                "name": d.get("name"),
                "zuletzt_um": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(d["zuletzt"])),
                "zuletzt": d["zuletzt"],
                "embedding": [round(float(x), 6) for x in np.asarray(d["embedding"])],
            }
            zeilen.append(f"  {json.dumps(pid)}: {json.dumps(eintrag, ensure_ascii=False)}")
        self._schreibe_text(pfad, "{\n" + ",\n".join(zeilen) + "\n}\n")

    def _uhrzeit(self, memory_minuten):
        """Memory-Zeit (Minuten seit Start) -> lesbare Uhrzeit."""
        g = self.gedachtnis
        ts = g.start + (memory_minuten - g.zeit_offset) * 60
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))

    def speichern(self):
        """ACT-Up-Chunks und Personenregister in ihre Dateien schreiben."""
        with self.lock:
            g = self.gedachtnis
            g.zeit_synchronisieren()
            if self.actup_datei:
                self._schreibe_json(self.actup_datei, {
                    "info": {
                        "beschreibung": "ACT-Up-Gedächtnis des Pepper-Eventplaners",
                        "gespeichert_um": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "start": g.start,          # Echtzeit-Start der Memory-Uhr (Unix-Zeit)
                        "zeit_offset": g.zeit_offset,
                        "memory_time": g.m.time,   # Minuten seit start
                        "decay": g.m.decay,
                        "threshold": g.m.threshold,
                        "vergessen_nach_minuten": VERGESSEN_NACH_MINUTEN,
                    },
                    "chunks": [{
                        "person": c.get("person"),
                        "name": self.register.name_von(c.get("person")),
                        "attribut": c.get("attribut"),
                        "wert": c.get("wert"),
                        "gelernt_minuten": [float(r) for r in c.references],
                        "gelernt_um": [self._uhrzeit(r) for r in c.references],
                        "aktivierung": round(g.aktivierung(c), 3),
                        "status": "aktiv" if g._ist_aktiv(c) else "vergessen",
                    } for c in g.m.chunks],
                }, einrueckung=2)
            if self.personen_datei:
                self._schreibe_register(self.personen_datei)

    def laden(self):
        """Beide Dateien laden, falls vorhanden (sonst leeres Gedächtnis)."""
        with self.lock:
            if self.personen_datei and os.path.exists(self.personen_datei):
                with open(self.personen_datei, encoding="utf-8") as f:
                    personen = json.load(f)
                self.register.personen = {
                    pid: {"embedding": np.array(d["embedding"], dtype=np.float32),
                          "zuletzt": d["zuletzt"],
                          **({"name": d["name"]} if d.get("name") else {})}
                    for pid, d in personen.items()}
                print(f"[Gedächtnis] {len(personen)} Personen geladen aus {self.personen_datei}")

            if self.actup_datei and os.path.exists(self.actup_datei):
                with open(self.actup_datei, encoding="utf-8") as f:
                    daten = json.load(f)
                g = self.gedachtnis
                g.start = daten["info"]["start"]
                g.zeit_offset = daten["info"].get("zeit_offset", 0.0)
                # Alle Lernereignisse chronologisch neu einspielen -> identische references.
                # Aktivierung/Status in der Datei sind nur zum Lesen, ACT-Up rechnet sie neu.
                ereignisse = sorted(
                    (r, json.dumps({"person": c["person"], "attribut": c["attribut"],
                                    "wert": c["wert"]}, sort_keys=True))
                    for c in daten["chunks"] for r in c["gelernt_minuten"])
                for t, slots in ereignisse:
                    g.m.time = t
                    g.m.learn(json.loads(slots))
                g.m.time = daten["info"]["memory_time"]
                print(f"[Gedächtnis] {len(daten['chunks'])} Chunks geladen aus {self.actup_datei}")

    # ---------- Debug-Ausgabe ----------

    def chunk_ausgabe(self):
        """Tabelle aller Chunks + aktueller Planungsstand pro Person."""
        with self.lock:
            g = self.gedachtnis
            g.zeit_synchronisieren()
            zeilen = []
            for c in g.m.chunks:
                akt = g.aktivierung(c)
                zeilen.append((
                    f"Chunk{getattr(c, '_name', '????')}",
                    c.get("person"),
                    self.register.name_von(c.get("person")) or "-",
                    f'{c.get("attribut")}={c.get("wert")}',
                    len(c.references),
                    g.m.time - max(c.references),
                    akt,
                    "aktiv" if akt >= g.m.threshold else "vergessen",
                ))
            uebersicht = [(pid, self.stand(pid)) for pid in self.register.personen]

        print(f"\n[Gedächtnis] {len(zeilen)} Chunk(s), Zeit {g.m.time:.1f} min, "
              f"Threshold {g.m.threshold:.2f}")
        if not zeilen:
            print("  (leer)")
        else:
            print(f"  {'Chunk':<11}{'Person':<14}{'Name':<12}{'Attribut=Wert':<34}{'gelernt':>8}"
                  f"{'Alter':>9}{'Aktiv.':>8}  Status")
            for chunk, pid, name, eintrag, n, alter, akt, status in sorted(zeilen):
                print(f"  {chunk:<11}{pid:<14}{name[:11]:<12}{eintrag[:33]:<34}{n:>7}x"
                      f"{alter:>7.1f}m{akt:>8.2f}  {status}")

        print(f"\n[Personen] {len(uebersicht)} im Register")
        for pid, s in uebersicht:
            kurz = {k: v for k, v in s.items() if v not in (None, [], "")}
            print(f"  {pid:<14}{kurz or '(nichts Aktives)'}")
        print()
