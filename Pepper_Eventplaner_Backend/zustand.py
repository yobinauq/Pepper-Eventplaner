"""Zustandsraum und Statusberechnung des Pepper-Eventplaners.

Reiner Python-Code ohne ACT-Up und ohne GPT -> deterministisch und testbar.

  Zustand  (stand):  Werte, die ACT-Up erinnert (Datum, Personenzahl, Location ...)
  Status   (status): pro Aktion erledigt / verfügbar / blockiert, mit Grund
                     -> wird bei jeder Anfrage NEU aus dem Zustand berechnet, nie gespeichert

Rechenregel je Aktion (siehe Folie "Statusberechnung"):
  1. erledigt?             -> Prüfung gegen die AKTUELLEN Werte
  2. sonst Vorbedingungen  -> alle erledigt: verfügbar, sonst blockiert (mit Grund)

Weil "erledigt" immer neu geprüft wird, schlagen Änderungen automatisch durch:
Personenzahl 60 -> 90 macht Location (Kapazität 70), Catering (für 60) und
Gästeliste (60 Namen) wieder offen.
"""

import random
import re
from datetime import date, datetime

# ============================================================
# Katalog (fest hinterlegt, für alle Teilnehmenden gleich)
# Belegte Termine im Format TT.MM.JJJJ - für die Studie anpassen.
# ============================================================

LOCATIONS = {
    "Halle A":           {"standort": "Lübeck Innenstadt", "kapazitaet": 70,  "kosten": 1200,
                          "belegt": ["26.09.2026"]},
    "Halle B":           {"standort": "Lübeck Innenstadt", "kapazitaet": 120, "kosten": 2000,
                          "belegt": ["12.09.2026"]},
    "Gartenhaus am See": {"standort": "Ratzeburg",         "kapazitaet": 50,  "kosten": 800,
                          "belegt": []},
    "Festsaal am Hafen": {"standort": "Travemünde",        "kapazitaet": 200, "kosten": 3500,
                          "belegt": ["26.09.2026"]},
    "Scheune am Feldrand": {"standort": "Bad Schwartau",   "kapazitaet": 100, "kosten": 1600,
                            "belegt": ["03.10.2026"]},
    "Dachterrasse Altstadt": {"standort": "Lübeck Altstadt", "kapazitaet": 40, "kosten": 900,
                              "belegt": []},
}

# Euro pro Person
CATERING = {"Fingerfood": 15, "Buffet": 28, "Drei-Gänge-Menü": 45}

DAUER_OPTIONEN = ["halber Tag", "ganzer Tag", "Abend"]

# "Kontakte" der Person für die Gästeliste (Dummy-Daten): 100 Vornamen x 100 Nachnamen
# -> bis zu 10.000 verschiedene Gäste. Die Liste wird nicht vorgelesen, Pepper nennt nur Beispiele.
VORNAMEN = [
    "Anna", "Ben", "Clara", "David", "Emma", "Felix", "Greta", "Hannes", "Ida", "Jonas",
    "Lea", "Leon", "Mia", "Noah", "Lina", "Paul", "Marie", "Luca", "Sophie", "Elias",
    "Emilia", "Finn", "Hanna", "Henry", "Lena", "Anton", "Laura", "Max", "Lara", "Moritz",
    "Sarah", "Jakob", "Julia", "Theo", "Leonie", "Oskar", "Nele", "Karl", "Frieda", "Emil",
    "Ella", "Matteo", "Charlotte", "Liam", "Amelie", "Samuel", "Johanna", "Julian", "Paula", "Tim",
    "Luisa", "Niklas", "Maja", "Jan", "Lisa", "Tom", "Katharina", "Lukas", "Helena", "Fabian",
    "Mila", "Simon", "Carla", "Vincent", "Alina", "Philipp", "Nina", "Erik", "Merle", "Konstantin",
    "Isabel", "Robert", "Jana", "Valentin", "Elena", "Benedikt", "Romy", "Florian", "Svenja", "Malte",
    "Annika", "Till", "Pia", "Daniel", "Hedda", "Sebastian", "Tabea", "Markus", "Ronja", "Stefan",
    "Ute", "Klaus", "Birgit", "Jürgen", "Sabine", "Thomas", "Monika", "Peter", "Petra", "Uwe",
]
NACHNAMEN = [
    "Müller", "Schmidt", "Schneider", "Fischer", "Weber", "Meyer", "Wagner", "Becker", "Schulz", "Hoffmann",
    "Schäfer", "Koch", "Bauer", "Richter", "Klein", "Wolf", "Schröder", "Neumann", "Schwarz", "Zimmermann",
    "Braun", "Krüger", "Hofmann", "Hartmann", "Lange", "Schmitt", "Werner", "Schmitz", "Krause", "Meier",
    "Lehmann", "Schmid", "Schulze", "Maier", "Köhler", "Herrmann", "König", "Walter", "Mayer", "Huber",
    "Kaiser", "Fuchs", "Peters", "Lang", "Scholz", "Möller", "Weiß", "Jung", "Hahn", "Schubert",
    "Vogel", "Friedrich", "Keller", "Günther", "Frank", "Berger", "Winkler", "Roth", "Beck", "Lorenz",
    "Baumann", "Franke", "Albrecht", "Schuster", "Simon", "Ludwig", "Böhm", "Winter", "Kraus", "Martin",
    "Schumacher", "Krämer", "Vogt", "Stein", "Jäger", "Otto", "Sommer", "Groß", "Seidel", "Heinrich",
    "Brandt", "Haas", "Schreiber", "Graf", "Schulte", "Dietrich", "Ziegler", "Kuhn", "Kühn", "Pohl",
    "Engel", "Horn", "Busch", "Bergmann", "Thomas", "Voigt", "Sauer", "Arnold", "Wolff", "Pfeiffer",
]


def gaesteliste_erzeugen(person_id, anzahl) -> list[str]:
    """Zufällige Gästeliste aus den Kontakten - pro Person immer dieselbe Reihenfolge.
    Wächst die Liste (300 -> 330), bleiben die ersten 300 Namen gleich."""
    alle = [f"{v} {n}" for v in VORNAMEN for n in NACHNAMEN]
    random.Random(f"gaesteliste-{person_id}").shuffle(alle)
    return alle[:max(0, anzahl)]

# ============================================================
# Zustandsraum: alle Einzelwerte, die ACT-Up speichert
# ============================================================

EINZELWERTE = [
    "event_typ",
    # fundamental
    "datum", "dauer", "personenzahl", "budget",
    # aufbauend
    "gaesteliste",                        # Anzahl Namen auf der Liste
    "location",                           # Name aus LOCATIONS
    "catering", "catering_personen",      # Option aus CATERING + für wie viele bestellt
    "programm",                           # kurze Beschreibung
    "einladungen_datum", "einladungen_anzahl",  # mit welchem Datum / an wie viele verschickt
]

LABEL = {
    "event_typ": "Event", "datum": "Datum", "dauer": "Dauer", "personenzahl": "Personenzahl",
    "budget": "Budget (Euro)", "gaesteliste": "Gästeliste (Namen)", "location": "Location",
    "catering": "Catering", "catering_personen": "Catering bestellt für",
    "programm": "Programm", "einladungen_datum": "Einladungen verschickt für Datum",
    "einladungen_anzahl": "Einladungen verschickt an",
}

# ============================================================
# Aktionen mit Vorbedingungen (Folie "Statusberechnung")
# Reihenfolge wichtig: Vorbedingungen stehen immer VOR der Aktion.
# ============================================================

AKTIONEN = {
    "datum":        {"titel": "Datum festlegen",         "typ": "fundamental", "vor": []},
    "dauer":        {"titel": "Dauer festlegen",         "typ": "fundamental", "vor": []},
    "personenzahl": {"titel": "Personenzahl festlegen",  "typ": "fundamental", "vor": []},
    "budget":       {"titel": "Budget festlegen",        "typ": "fundamental", "vor": []},
    "gaesteliste":  {"titel": "Gästeliste erstellen",    "typ": "aufbauend", "vor": ["personenzahl"]},
    "location":     {"titel": "Location buchen",         "typ": "aufbauend",
                     "vor": ["datum", "personenzahl", "budget"]},
    "catering":     {"titel": "Catering bestellen",      "typ": "aufbauend", "vor": ["personenzahl", "budget"]},
    "programm":     {"titel": "Programm planen",         "typ": "aufbauend", "vor": ["dauer", "location"]},
    "einladungen":  {"titel": "Einladungen verschicken", "typ": "aufbauend",
                     "vor": ["gaesteliste", "location", "datum"]},
}

# Wie eine fehlende Vorbedingung im Grund "blockiert - erst ..." heißt
NOETIG = {
    "datum": "Datum festlegen", "dauer": "Dauer festlegen",
    "personenzahl": "Personenzahl festlegen", "budget": "Budget festlegen",
    "gaesteliste": "Gästeliste vervollständigen", "location": "Location buchen",
    "catering": "Catering bestellen", "programm": "Programm planen",
}


# ============================================================
# Hilfsfunktionen
# ============================================================

def parse_datum(text) -> date | None:
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(text).strip(), fmt).date()
        except ValueError:
            pass
    return None


def normalisiere_datum(text) -> str | None:
    d = parse_datum(text)
    return d.strftime("%d.%m.%Y") if d else None


def normalisiere_dauer(text) -> str | None:
    """Kategorie (halber Tag / ganzer Tag / Abend) oder Stundenangabe ("5 Stunden")."""
    t = str(text).lower().strip()
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:stunden?|std\.?|h)\b", t) or re.fullmatch(r"(\d+(?:[.,]\d+)?)", t)
    if m:
        zahl = m.group(1).replace(".", ",")
        return f"{zahl} Stunde" if zahl == "1" else f"{zahl} Stunden"
    if "halb" in t:
        return "halber Tag"
    if "ganz" in t or "ganztag" in t:
        return "ganzer Tag"
    if "abend" in t:
        return "Abend"
    return None


def finde_im_katalog(text, katalog) -> str | None:
    """Name aus dem Katalog in exakter Schreibweise (Groß/klein egal)."""
    t = str(text).strip().lower()
    for name in katalog:
        if name.lower() == t:
            return name
    return None


def location_frei(name, datum) -> bool:
    return datum not in LOCATIONS[name]["belegt"]


def kosten(stand) -> dict:
    """Bisher gebuchte Kosten und Restbudget."""
    k_loc = LOCATIONS[stand["location"]]["kosten"] if stand.get("location") in LOCATIONS else 0
    k_cat = 0
    if stand.get("catering") in CATERING and stand.get("catering_personen"):
        k_cat = CATERING[stand["catering"]] * stand["catering_personen"]
    gesamt = k_loc + k_cat
    rest = stand["budget"] - gesamt if isinstance(stand.get("budget"), int) else None
    return {"location": k_loc, "catering": k_cat, "gesamt": gesamt, "rest": rest}


def _und(teile):
    return teile[0] if len(teile) == 1 else ", ".join(teile[:-1]) + " und " + teile[-1]


# ============================================================
# Statusberechnung
# ============================================================

def _pruefe(aktion, s) -> tuple[bool, str | None, str | None]:
    """-> (erledigt, grund_warum_nicht_erledigt, fortschritt)"""
    p = s.get("personenzahl")

    if AKTIONEN[aktion]["typ"] == "fundamental":
        return s.get(aktion) is not None, None, None

    if aktion == "gaesteliste":
        n = s.get("gaesteliste") or 0
        fortschritt = f"{n} von {p} Namen" if p else f"{n} Namen"
        if p and n >= p:
            return True, None, fortschritt
        grund = f"{p - n} Personen fehlen" if p and n > 0 else None
        return False, grund, fortschritt

    if aktion == "location":
        name = s.get("location")
        if not name:
            return False, None, None
        info = LOCATIONS[name]
        if p and info["kapazitaet"] < p:
            return False, f"{name} fasst nur {info['kapazitaet']} ({p} benötigt)", None
        if s.get("datum") and not location_frei(name, s["datum"]):
            return False, f"{name} ist am {s['datum']} belegt", None
        return True, None, None

    if aktion == "catering":
        if not s.get("catering"):
            return False, None, None
        bestellt = s.get("catering_personen") or 0
        if p and bestellt < p:
            return False, f"Für {bestellt} bestellt, {p} benötigt", None
        return True, None, None

    if aktion == "programm":
        return s.get("programm") is not None, None, None

    if aktion == "einladungen":
        e_datum = s.get("einladungen_datum")
        if not e_datum:
            return False, None, None
        if s.get("datum") and e_datum != s["datum"]:
            return False, (f"Einladungen nennen noch den {e_datum}, "
                           f"neues Datum ist der {s['datum']}"), None
        anzahl = s.get("einladungen_anzahl") or 0
        if p and anzahl < p:
            return False, f"{p - anzahl} zusätzliche Gäste einladen", None
        return True, None, None

    raise KeyError(aktion)


def berechne_status(stand: dict) -> dict:
    """Status jeder Aktion:
    {aktion: {"status": "erledigt"|"verfügbar"|"blockiert",
              "grund": warum nicht erledigt (z.B. Kapazität), oder None,
              "blockiert_durch": [Vorbedingungen, die fehlen],
              "fortschritt": z.B. "60 von 90 Namen", oder None,
              "gesetzt": ob schon ein Wert existiert (dann ist es "wieder offen")}}"""
    status = {}
    for aktion, info in AKTIONEN.items():
        erledigt, grund, fortschritt = _pruefe(aktion, stand)
        fehlend = [v for v in info["vor"] if status[v]["status"] != "erledigt"]
        if erledigt:
            zustand = "erledigt"
        elif fehlend:
            zustand = "blockiert"
        else:
            zustand = "verfügbar"
        status[aktion] = {
            "status": zustand,
            "grund": grund,
            "blockiert_durch": fehlend if not erledigt else [],
            "fortschritt": fortschritt,
            "gesetzt": grund is not None,
        }
    return status


def blockiert_text(eintrag) -> str:
    return "erst " + _und([NOETIG[v] for v in eintrag["blockiert_durch"]])


def naechster_schritt(status: dict) -> str | None:
    """Feste Priorität: 1. wieder offene Punkte, 2. fundamentale, 3. aufbauende Aktionen."""
    verfuegbar = [a for a, e in status.items() if e["status"] == "verfügbar"]
    for bedingung in (lambda a: status[a]["gesetzt"],
                      lambda a: AKTIONEN[a]["typ"] == "fundamental",
                      lambda a: True):
        for a in verfuegbar:
            if bedingung(a):
                return a
    return None


# ============================================================
# Änderungen aus einer Aussage anwenden (mit Prüfung)
# ============================================================

def wende_an(stand: dict, daten: dict) -> tuple[dict, list[tuple[str, str]]]:
    """Prüft die extrahierten Änderungen gegen die Regeln.

    Fundamentale Werte werden immer übernommen. Aufbauende Aktionen nur, wenn
    sie gerade möglich sind - sonst landen sie mit Grund in 'abgelehnt'.
    -> (lernen: {attribut: wert} für ACT-Up, abgelehnt: [(aktion, grund)])"""
    s = dict(stand)
    lernen, abgelehnt = {}, []

    def setze(attribut, wert):
        if s.get(attribut) != wert:
            s[attribut] = wert
            lernen[attribut] = wert

    def gesperrt(aktion):
        e = berechne_status(s)[aktion]
        return blockiert_text(e) if e["status"] == "blockiert" else None

    # 1. Fundamentale Werte + Event-Typ: immer übernehmen
    for k in ("event_typ", "datum", "dauer", "personenzahl", "budget"):
        if daten.get(k) is not None:
            setze(k, daten[k])

    # 2. Gästeliste (Anzahl oder aus den Kontakten auffüllen)
    if daten.get("gaesteliste") is not None or daten.get("gaesteliste_vorlage"):
        grund = gesperrt("gaesteliste")
        if grund:
            abgelehnt.append(("gaesteliste", grund))
        elif daten.get("gaesteliste_vorlage"):
            # so viele Kontakte eintragen, wie Personen kommen
            setze("gaesteliste", min(s["personenzahl"], len(VORNAMEN) * len(NACHNAMEN)))
        else:
            setze("gaesteliste", daten["gaesteliste"])

    # 3. Location buchen
    if daten.get("location"):
        name = daten["location"]
        grund = gesperrt("location")
        if grund:
            abgelehnt.append(("location", grund))
        elif LOCATIONS[name]["kapazitaet"] < s["personenzahl"]:
            abgelehnt.append(("location", f"{name} fasst nur {LOCATIONS[name]['kapazitaet']} "
                                          f"({s['personenzahl']} benötigt)"))
        elif not location_frei(name, s["datum"]):
            abgelehnt.append(("location", f"{name} ist am {s['datum']} belegt"))
        else:
            setze("location", name)

    # 4. Catering bestellen (immer für die aktuelle Personenzahl)
    if daten.get("catering"):
        grund = gesperrt("catering")
        if grund:
            abgelehnt.append(("catering", grund))
        else:
            setze("catering", daten["catering"])
            setze("catering_personen", s["personenzahl"])

    # 5. Programm
    if daten.get("programm"):
        grund = gesperrt("programm")
        if grund:
            abgelehnt.append(("programm", grund))
        else:
            setze("programm", daten["programm"])

    # 6. Einladungen verschicken - mit dem vereinbarten Datum
    if daten.get("einladungen_verschicken"):
        grund = gesperrt("einladungen")
        if grund:
            abgelehnt.append(("einladungen", grund))
        else:
            setze("einladungen_datum", s["datum"])
            setze("einladungen_anzahl", s["gaesteliste"])

    return lernen, abgelehnt


# ============================================================
# Vergleich mit dem letzten Handlungsraum
# ============================================================

def vergleiche(alt: dict | None, stand: dict, status: dict) -> list[str]:
    """'Seit dem letzten Mal hat sich ... geändert, dadurch ist ... wieder offen'.
    alt = {"stand": ..., "status": ...} vom Ende der letzten Interaktion."""
    if not alt:
        return []
    zeilen = []
    for k in EINZELWERTE:
        vorher, jetzt = alt["stand"].get(k), stand.get(k)
        if vorher != jetzt and jetzt is not None:
            zeilen.append(f"{LABEL[k]}: {vorher if vorher is not None else 'unbekannt'} → {jetzt}")

    for aktion, e in status.items():
        a = alt["status"].get(aktion, {}).get("status")
        titel = AKTIONEN[aktion]["titel"]
        if a == e["status"]:
            continue
        if a == "erledigt":
            grund = e["grund"] or (blockiert_text(e) if e["blockiert_durch"] else "")
            zeilen.append(f"{titel}: wieder offen" + (f" – {grund}" if grund else ""))
        elif e["status"] == "erledigt":
            zeilen.append(f"{titel}: jetzt erledigt")
        elif e["status"] == "verfügbar":
            zeilen.append(f"{titel}: jetzt möglich")
        else:
            zeilen.append(f"{titel}: jetzt blockiert – {blockiert_text(e)}")
    return zeilen


# ============================================================
# Text für GPT
# ============================================================

SYMBOL = {"erledigt": "✓", "verfügbar": "○", "blockiert": "⊘"}


def zustand_text(stand: dict, status: dict) -> list[str]:
    z = ["ZUSTAND:"]
    for k in ("event_typ", "datum", "dauer", "personenzahl", "budget"):
        vorher = f" (vorher {stand[k + '_vorher']})" if k + "_vorher" in stand else ""
        z.append(f"- {LABEL[k]}: {stand.get(k, 'unbekannt')}{vorher}")
    z.append(f"- Gästeliste: {status['gaesteliste']['fortschritt'] or 'leer'}")
    if stand.get("location"):
        info = LOCATIONS[stand["location"]]
        z.append(f"- Location: {stand['location']} in {info['standort']} "
                 f"(Kapazität {info['kapazitaet']}, {info['kosten']} Euro)")
    else:
        z.append("- Location: unbekannt")
    if stand.get("catering"):
        z.append(f"- Catering: {stand['catering']} für {stand.get('catering_personen')} Personen")
    else:
        z.append("- Catering: unbekannt")
    z.append(f"- Programm: {stand.get('programm', 'unbekannt')}")
    if stand.get("einladungen_datum"):
        z.append(f"- Einladungen: verschickt an {stand.get('einladungen_anzahl')} Gäste "
                 f"für den {stand['einladungen_datum']}")
    else:
        z.append("- Einladungen: nicht verschickt")

    k = kosten(stand)
    if k["rest"] is not None:
        z.append(f"- Kosten bisher: {k['gesamt']} Euro, Restbudget: {k['rest']} Euro"
                 + ("  ACHTUNG: Budget überschritten!" if k["rest"] < 0 else ""))
    if isinstance(stand.get("personenzahl"), int) and isinstance(stand.get("budget"), int):
        z.append(f"- Budget pro Person: ca. {stand['budget'] // stand['personenzahl']} Euro")
    return z


def status_text(status: dict) -> list[str]:
    z = ["STATUS DER AKTIONEN:"]
    for aktion, e in status.items():
        titel = AKTIONEN[aktion]["titel"]
        zeile = f"{SYMBOL[e['status']]} {titel}: {e['status']}"
        if e["status"] == "blockiert":
            zeile += f" – {blockiert_text(e)}"
        details = [x for x in (e["fortschritt"], e["grund"]) if x and e["status"] != "erledigt"]
        if details:
            zeile += f" ({'; '.join(details)})"
        z.append(zeile)
    return z


def katalog_text(stand: dict) -> list[str]:
    datum, p = stand.get("datum"), stand.get("personenzahl")
    z = ["KATALOG LOCATIONS (nur diese gibt es):"]
    for name, info in LOCATIONS.items():
        hinweise = []
        if p and info["kapazitaet"] < p:
            hinweise.append("zu klein")
        if datum and not location_frei(name, datum):
            hinweise.append(f"am {datum} belegt")
        z.append(f"- {name}, {info['standort']}: bis {info['kapazitaet']} Personen, "
                 f"{info['kosten']} Euro" + (f" [{', '.join(hinweise)}]" if hinweise else ""))
    z.append("KATALOG CATERING (Preis pro Person): "
             + ", ".join(f"{n} {preis} Euro" for n, preis in CATERING.items()))
    z.append(f"Dauer-Optionen: {', '.join(DAUER_OPTIONEN)} oder in Stunden. "
             f"Gästeliste: Pepper kann Gäste aus den Kontakten der Person eintragen "
             f"(so viele wie die Personenzahl).")
    return z
