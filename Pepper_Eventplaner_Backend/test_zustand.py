"""Testet die Statusberechnung mit den Interaktionen aus der Studienplanung.
Start im Backend-Ordner:  python test_zustand.py   (kein OpenAI, keine Kamera nötig)"""
import zustand as z


def zeige(titel, stand, alt=None):
    status = z.berechne_status(stand)
    print(f"\n===== {titel} =====")
    print("\n".join(z.status_text(status)))
    diff = z.vergleiche(alt, stand, status)
    if diff:
        print("SEIT DEM LETZTEN MAL:\n  " + "\n  ".join(diff))
    print("Nächster Schritt:", z.naechster_schritt(status))
    return {"stand": dict(stand), "status": status}


def anwenden(stand, daten):
    lernen, abgelehnt = z.wende_an(stand, daten)
    stand.update(lernen)
    for aktion, grund in abgelehnt:
        print(f"  ABGELEHNT {aktion}: {grund}")
    return stand, abgelehnt


# Interaktion 1: Startinformationen + Planung bis kurz vor die Einladungen
s = {}
anwenden(s, {"datum": "12.09.2026", "personenzahl": 60, "budget": 6000})
_, abgelehnt = anwenden(s, {"einladungen_verschicken": True})          # zu früh
assert abgelehnt and abgelehnt[0][0] == "einladungen"
anwenden(s, {"dauer": "Abend", "location": "Halle A", "catering": "Buffet",
             "gaesteliste_vorlage": True})
snap = zeige("Interaktion 1", s)
st = snap["status"]
assert st["location"]["status"] == st["catering"]["status"] == st["gaesteliste"]["status"] == "erledigt"
assert st["einladungen"]["status"] == "verfügbar"
anwenden(s, {"einladungen_verschicken": True})
assert s["einladungen_datum"] == "12.09.2026" and s["einladungen_anzahl"] == 60
snap = zeige("Interaktion 1 – Einladungen verschickt", s, snap)

# Interaktion 2: Personenzahl 60 -> 90
anwenden(s, {"personenzahl": 90})
snap = zeige("Interaktion 2: Personenzahl 60 -> 90", s, snap)
st = snap["status"]
assert st["location"]["grund"] == "Halle A fasst nur 70 (90 benötigt)"
assert st["catering"]["grund"] == "Für 60 bestellt, 90 benötigt"
assert st["gaesteliste"]["grund"] == "30 Personen fehlen"
assert st["einladungen"]["grund"] == "30 zusätzliche Gäste einladen"
assert st["personenzahl"]["status"] == "erledigt"

# Lösen: größere Location, Catering neu, Liste ergänzen, nachladen
_, abgelehnt = anwenden(s, {"location": "Halle A"})
assert abgelehnt
anwenden(s, {"location": "Halle B", "catering": "Buffet", "gaesteliste": 90})
assert s["location"] == "Halle A"   # Halle B ist am 12.09. belegt -> abgelehnt
anwenden(s, {"location": "Festsaal am Hafen", "einladungen_verschicken": True})
snap = zeige("Interaktion 2 – gelöst", s, snap)
assert all(e["status"] == "erledigt" for a, e in snap["status"].items() if a != "programm")

# Interaktion 3: Datum 2 Wochen nach hinten (26.09.2026: Festsaal belegt)
anwenden(s, {"datum": "26.09.2026"})
snap = zeige("Interaktion 3: Datum +2 Wochen", s, snap)
st = snap["status"]
assert st["location"]["grund"] == "Festsaal am Hafen ist am 26.09.2026 belegt"
assert "neues Datum ist der 26.09.2026" in st["einladungen"]["grund"]

print("\nAlle Prüfungen bestanden.")
