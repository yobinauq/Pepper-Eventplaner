"""Pepper-Eventplaner: Chat-Backend fuer die Studie mit 4 Modi (2x2-Design).

             | mit Gedaechtnis (ACT-Up) | ohne Gedaechtnis
  -----------+--------------------------+-----------------
  mit Zust.  | Modus 1   /chat/1        | Modus 2  /chat/2
  ohne Zust. | Modus 3   /chat/3        | Modus 4  /chat/4

  Zustandsraum = Regeln: Status je Aktion, Blockieren, "wieder offen", naechster Schritt
  Gedaechtnis  = Person: Gesichtserkennung, ACT-Up (mit Vergessen), Name, Wuensche,
                 Wiederbegruessung "Seit dem letzten Mal ..."
  In ALLEN Modi gleich: Modell, Sprechregeln, Katalog, Chatverlauf (Arbeitsspeicher).

Ablauf pro Anfrage (je nach Modus werden Schritte uebersprungen):
  1. Person erkennen (nur mit Gedaechtnis) bzw. Teilnehmer-ID als Schluessel
  2. GPT extrahiert, was sich laut Aussage aendern soll        (nicht in Modus 4)
  3. mit Zustandsraum: Regelpruefung (zustand.wende_an), sonst alles uebernehmen
  4. Status / Aenderungen berechnen
  5. GPT antwortet mit Grundprompt + Modus-Bausteinen + Block
  6. Handlungsraum speichern + Log (mit modus, teilnehmer_id, antwortzeit)

Start:
  python -m uvicorn main:app --host 0.0.0.0 --port 8000
  Daten liegen in DATEN_ORDNER (Standard "daten"), getrennt pro Modus.
  Zum Testen: DATEN_ORDNER=test_daten python -m uvicorn main:app --host 0.0.0.0 --port 8000
"""
import json
import os
import re
import time
from datetime import date

import openai
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from openai import OpenAI
from pydantic import BaseModel

import zustand as z
from personen_gedaechtnis import PersonenErinnerung
from sitzungs_speicher import SitzungsSpeicher

MODELL = "gpt-4.1-mini"
MAX_ANTWORT_ZEICHEN = 350

# Die 4 Studien-Modi
MODI = {
    1: {"zustandsraum": True,  "gedaechtnis": True},
    2: {"zustandsraum": True,  "gedaechtnis": False},
    3: {"zustandsraum": False, "gedaechtnis": True},
    4: {"zustandsraum": False, "gedaechtnis": False},
}

# Chatverlauf = Arbeitsspeicher: bleibt im RAM (pro Modus + Teilnehmer), kein Zeitlimit.
# Geloescht wird er erst durch POST /sitzung/reset oder einen Neustart des Backends.
VERLAUF_MAX_EINTRAEGE = 50

# Kommt eine Person nach so langer Pause wieder, gilt das als neue Interaktion
# -> (nur mit Gedaechtnis) Wiederbegruessung "Seit dem letzten Mal ..."
NEUE_INTERAKTION_NACH_MINUTEN = 5

FEHLER_ANTWORT = (
    "Entschuldigung, ich kann gerade nicht richtig nachdenken. "
    "Versuch es bitte gleich noch einmal."
)

# Grundprompt: in ALLEN Modi gleich
PROMPT_GRUND = """
Du bist Pepper, ein humanoider Roboter und Assistent für Eventplanung (Partys, Hochzeiten, Feiern).
Du hilfst der Person, ihr Event Schritt für Schritt zu planen.

Planung:
- Locations und Catering gibt es NUR aus dem KATALOG. Erfinde keine anderen.
- Die Gäste kommen aus den Kontakten der Person (Beispieldaten). Nenne Gästenamen nur, wenn sie
  unten als Beispiele stehen - höchstens zwei, lies nie eine ganze Liste vor.
- Frage immer nur EINE Sache pro Antwort.
- Lies keine Listen vor (nicht den ganzen Katalog) - nenne höchstens zwei Optionen.

Sprechen:
- Höchstens 2-3 kurze Sätze, keine Listen, keine Emojis, kein Markdown.
- Zahlen ausschreiben, wie man sie spricht ("sechstausend Euro").
- Du bist kein ChatGPT und erwähnst keine Modellnamen.
""".strip()

# Baustein Zustandsraum (Modus 1 + 2)
PROMPT_ZUSTANDSRAUM = """
Planungsstand (vom System berechnet):
- Unter ZUSTAND und STATUS DER AKTIONEN steht der aktuelle Planungsstand. Nur diese Fakten sind
  sicher. Der Status wird vom System berechnet - übernimm ihn, rechne ihn nicht selbst neu.
- Schlage im KATALOG nur Einträge vor, die nicht "zu klein" oder "belegt" markiert sind.
- Steht etwas unter GERADE GEÄNDERT, hat die Person das eben selbst gesagt. Bestätige es direkt
  in der Gegenwart und nenne die wichtigste Folge, z.B.: "Alles klar, dann planen wir jetzt mit
  hundertfünfundzwanzig Gästen. Dafür ist Halle B allerdings zu klein."
  NICHT so, als wäre es schon früher passiert ("ist bereits geändert", "wurde schon").
- Nenne bei Änderungen höchstens die zwei wichtigsten Folgen.
- Steht etwas unter ABGELEHNT, behaupte NIE, es sei erledigt. Erkläre kurz den Grund
  und biete an, zuerst die fehlende Voraussetzung zu klären.
- Eine blockierte Aktion darfst du nicht ausführen; nenne, was vorher fehlt.
- Schlage am Ende genau EINEN nächsten Schritt vor: den unter NÄCHSTER SCHRITT.
- Ist die Gästeliste der nächste Schritt, biete an, Kontakte einzutragen. Wurde sie gerade gefüllt,
  sag z.B.: "Okay, ich trage jetzt als Beispiel dreihundert Personen aus deinen Kontakten ein,
  zum Beispiel Anna Becker und Jonas Weber."
""".strip()

# Baustein Gedaechtnis (Modus 1 + 3)
PROMPT_GEDAECHTNIS = """
Gedächtnis (du erkennst die Person wieder):
- Unter GEDÄCHTNIS steht, was du über diese Person weißt (Name, Wünsche, Themen). Nutze es
  persönlich, aber erfinde nichts dazu. Sprich die Person gelegentlich mit Namen an.
- Steht dort "Neue Interaktion", kommt die Person nach einer Pause wieder: Begrüße sie kurz
  persönlich ("Schön, dass du wieder da bist, Lea!").
- Steht etwas unter SEIT DEM LETZTEN MAL, fasse es nach der Begrüßung kurz zusammen, z.B.:
  "Seit dem letzten Mal hat sich die Personenzahl von sechzig auf neunzig geändert."
""".strip()

# Baustein: Gedaechtnis OHNE Zustandsraum (Modus 3)
PROMPT_NUR_FAKTEN = """
Planung ohne Systemprüfung:
- Unter GEMERKTE PLANUNGSDATEN steht, was du dir zur Planung gemerkt hast. Es gibt keine
  automatische Prüfung - überlege selbst, was als Nächstes sinnvoll ist.
- Steht etwas unter GERADE GEÄNDERT, hat die Person das eben gesagt: bestätige es direkt.
""".strip()

# Baustein: weder Zustandsraum noch Gedaechtnis (Modus 4)
PROMPT_NUR_VERLAUF = """
Planung nur aus dem Gespräch:
- Du hast kein zusätzliches Gedächtnis. Orientiere dich am bisherigen Gesprächsverlauf und
  überlege selbst, was als Nächstes sinnvoll ist.
""".strip()


def system_prompt(modus: int) -> str:
    cfg = MODI[modus]
    teile = [PROMPT_GRUND]
    if cfg["zustandsraum"]:
        teile.append(PROMPT_ZUSTANDSRAUM)
    if cfg["gedaechtnis"]:
        teile.append(PROMPT_GEDAECHTNIS)
    if cfg["gedaechtnis"] and not cfg["zustandsraum"]:
        teile.append(PROMPT_NUR_FAKTEN)
    if not cfg["gedaechtnis"] and not cfg["zustandsraum"]:
        teile.append(PROMPT_NUR_VERLAUF)
    return "\n\n".join(teile)


SYSTEM_EXTRAKTION = """
Du extrahierst Planungsdaten aus einer Aussage an den Eventplanungs-Roboter Pepper.
Du bekommst HEUTE, den AKTUELLEN STAND, was PEPPER ZULETZT gesagt hat, und eine neue AUSSAGE.
Antworte nur mit JSON. Gib nur Felder zurück, die in der Aussage neu genannt, geändert
oder ausdrücklich beauftragt werden. Enthält die Aussage nichts davon, antworte mit {{}}.

Felder:
- "name": eigener Vorname der Person
- "event_typ": z.B. "Hochzeit", "Geburtstagsparty"
- "datum": immer als "TT.MM.JJJJ". Relative Angaben vom STAND oder von HEUTE aus
  ausrechnen ("zwei Wochen später" bei 12.09.2026 -> "26.09.2026"). Fehlt das Jahr,
  nimm den nächsten passenden Termin in der Zukunft.
- "dauer": einer von {dauer} ODER eine Stundenangabe als "<Zahl> Stunden" (z.B. "5 Stunden")
- "personenzahl": Zahl (int). Relative Angaben ausrechnen: "30 mehr" bei 60 -> 90
- "budget": Zahl in Euro (int), "6 Tausend" -> 6000
- "gaesteliste": Anzahl Namen auf der Gästeliste (int), relative Angaben ausrechnen
- "gaesteliste_vorlage": true, wenn Gäste aus den Kontakten / der vorbereiteten Liste eingetragen
  werden sollen (auch als Zustimmung, wenn Pepper das angeboten hat)
- "location": NUR wenn die Person eine Location bucht oder sich festlegt - exakt einer von {locations}
- "catering": NUR wenn die Person Catering bestellt oder sich festlegt - exakt einer von {catering}
- "programm": nur konkrete Programmpunkte, die die Person festlegt (z.B. "DJ und Tanz",
  "Rede und Spiele"). Keine Stimmungen oder Adjektive ("locker", "schön") -> dann weglassen.
- "einladungen_verschicken": true, wenn die Person sagt, die Einladungen sollen raus
- "wuensche_neu": Liste neuer Wünsche, kurz, z.B. ["vegetarisch"]
- "wuensche_entfernt": Liste von Wünschen aus dem Stand (exakt wie dort geschrieben), die verworfen werden
- "thema": sonstiges Gesprächsthema in 2-5 Wörtern, sonst weglassen

Wichtig:
- Fragen sind keine Entscheidungen: "Welche Location passt?" -> kein "location".
- Setze nur, was die Person will - ob es erlaubt ist, prüft das System.
- Kurze Zustimmungen ("ja", "ja bitte", "mach das", "klingt gut", "passt") beziehen sich auf
  PEPPER ZULETZT: Hat Pepper dort einen konkreten Vorschlag gemacht oder eine Aktion angeboten,
  extrahiere genau diesen Vorschlag. Lehnt die Person ab ("nein", "lieber nicht") -> {{}}.
  Hat Pepper nur eine offene Frage gestellt (ohne Vorschlag), ergibt "ja" allein nichts -> {{}}.

Regeln für "name":
- Nur wenn die Person ausdrücklich IHREN EIGENEN Vornamen nennt ("Ich heiße Lea").
- Nicht: Namen anderer Personen ("mein Verlobter Tom"), Eigenschaften ("ich bin müde").

Beispiele:
STAND: {{}}
AUSSAGE: "Ich bin Lea und plane eine Hochzeit am 12. September 2026 mit 60 Gästen, Budget sechstausend Euro."
-> {{"name": "Lea", "event_typ": "Hochzeit", "datum": "12.09.2026", "personenzahl": 60, "budget": 6000}}

STAND: {{"personenzahl": 60}}
AUSSAGE: "Es kommen doch 30 Leute mehr."
-> {{"personenzahl": 90}}

STAND: {{"datum": "12.09.2026"}}
AUSSAGE: "Wir müssen das Ganze zwei Wochen nach hinten verschieben."
-> {{"datum": "26.09.2026"}}

STAND: {{"personenzahl": 60}}
AUSSAGE: "Nimm meine vorbereitete Liste und schick dann die Einladungen raus."
-> {{"gaesteliste_vorlage": true, "einladungen_verschicken": true}}

STAND: {{"datum": "12.09.2026"}}
AUSSAGE: "Dauer soll 5 Stunden betragen."
-> {{"dauer": "5 Stunden"}}

STAND: {{"datum": "12.09.2026", "personenzahl": 90, "budget": 6000}}
PEPPER ZULETZT: "Für neunzig Gäste passt der Festsaal am Hafen. Soll ich ihn buchen?"
AUSSAGE: "Ja bitte."
-> {{"location": "Festsaal am Hafen"}}

STAND: {{"personenzahl": 300}}
PEPPER ZULETZT: "Soll ich dreihundert Personen aus deinen Kontakten auf die Gästeliste setzen?"
AUSSAGE: "Ja, mach das."
-> {{"gaesteliste_vorlage": true}}

STAND: {{"personenzahl": 60}}
PEPPER ZULETZT: "Soll ich das Buffet für sechzig Personen bestellen?"
AUSSAGE: "Nein, lieber Fingerfood."
-> {{"catering": "Fingerfood"}}
""".strip()


api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
if not api_key:
    raise RuntimeError("OPENAI_API_KEY fehlt oder ist leer")

# timeout 15 s, keine Retries: Pepper sagt lieber sofort Bescheid als lange zu warten
client = OpenAI(api_key=api_key, timeout=15.0, max_retries=0)
# Alle Daten getrennt pro Modus - wer Modus 1 und danach Modus 3 macht, wird in Modus 3
# NICHT schon wiedererkannt.
DATEN_ORDNER = os.getenv("DATEN_ORDNER", "daten")
os.makedirs(DATEN_ORDNER, exist_ok=True)


def _datei(name: str) -> str:
    return os.path.join(DATEN_ORDNER, name)


# Speicher: ACT-Up (Modus 1, 3), einfacher Sitzungsspeicher (Modus 2), keiner (Modus 4)
speicher = {
    1: PersonenErinnerung(_datei("modus1_actup_gedaechtnis.json"), _datei("modus1_personen_register.json")),
    2: SitzungsSpeicher(_datei("modus2_planung.json")),
    3: PersonenErinnerung(_datei("modus3_actup_gedaechtnis.json"), _datei("modus3_personen_register.json")),
    4: None,
}
LOG_DATEI = _datei("interaktionen_log.jsonl")
app = FastAPI()

# Chatverlauf (Arbeitsspeicher): "m<modus>:<teilnehmer>" -> [{"role", "content", "ts"}]
history: dict[str, list[dict]] = {}


class ChatRequest(BaseModel):
    text: str
    conversation_id: str | None = None
    # Nur fuer die lokale Gesichtserkennung - geht NICHT an OpenAI
    face_image_base64: str | None = None
    # Von der Versuchsleitung am Tablet eingegeben (z.B. "P07") - Schluessel fuer Verlauf,
    # Sitzungsspeicher (Modus 2) und Log
    teilnehmer_id: str | None = None
    # Nur zum Testen ohne Kamera (chat_test.py): feste Test-Person statt Gesichtserkennung
    test_person_id: str | None = None


def antwort(text, *, cid=None, mode="chat", modus=None, person_id=None, person_neu=None,
            themen=None, person_name=None) -> dict:
    return {
        "text": text,
        "conversation_id": cid,
        "mode": mode,
        "modus": modus,
        "gedaechtnis": MODI[modus]["gedaechtnis"] if modus else None,
        "person_id": person_id,
        "person_neu": person_neu,
        "person_name": person_name,
        "erinnerte_themen": themen or [],   # zeigt die App in der Gedaechtnis-Zeile an
    }


@app.exception_handler(openai.APIError)
async def openai_fehler(request, exc):
    grund = {
        openai.APITimeoutError: "Zeitueberschreitung",
        openai.APIConnectionError: "keine Verbindung zu OpenAI (Internet?)",
        openai.RateLimitError: "Rate-Limit oder kein Guthaben",
        openai.AuthenticationError: "API-Key ungueltig",
    }.get(type(exc), type(exc).__name__)
    print(f"[OpenAI] Fehler: {grund} -> {exc}")
    # Status 200, damit Pepper den Satz einfach vorliest
    return JSONResponse(status_code=200, content=antwort(FEHLER_ANTWORT, mode="error"))


# ---------- Hilfsfunktionen ----------

def fuer_sprache_kuerzen(text: str, max_zeichen: int = MAX_ANTWORT_ZEICHEN) -> str:
    """Markdown entfernen und an einer Satzgrenze kuerzen."""
    t = (text or "").strip()
    t = re.sub(r"[*_#`>]+", "", t)
    t = re.sub(r"^\s*(?:[-•]|\d+[.)])\s+", "", t, flags=re.MULTILINE)
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) <= max_zeichen:
        return t
    ergebnis = ""
    for s in re.split(r"(?<=[.!?])\s+", t):
        if len(ergebnis) + len(s) + 1 > max_zeichen:
            break
        ergebnis = f"{ergebnis} {s}".strip()
    return ergebnis or t[:max_zeichen].rsplit(" ", 1)[0] + "."


def parse_json_safely(raw: str) -> dict | None:
    txt = re.sub(r"^```(?:json)?\s*|\s*```$", "", (raw or "").strip(), flags=re.IGNORECASE)
    m = re.search(r"\{.*\}", txt, flags=re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def verlauf_merken(cid: str, role: str, text: str):
    eintraege = history.setdefault(cid, [])
    eintraege.append({"role": role, "content": text, "ts": time.time()})
    history[cid] = eintraege[-VERLAUF_MAX_EINTRAEGE:]


def verlauf_holen(cid: str) -> list[dict]:
    return [{"role": e["role"], "content": e["content"]} for e in history.get(cid, [])]


def _zahl(v, maximum: int) -> int | None:
    """80, 80.0, "80", "8.000 €" -> int; Unplausibles -> None."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        n = int(v)
    else:
        ziffern = re.sub(r"\D", "", str(v))
        if not ziffern:
            return None
        n = int(ziffern)
    return n if 0 < n <= maximum else None


def _zeitspanne(sekunden: float) -> str:
    minuten = sekunden / 60
    if minuten < 60:
        return f"{max(1, round(minuten))} Minuten"
    if minuten < 48 * 60:
        return f"{round(minuten / 60)} Stunden"
    return f"{round(minuten / 1440)} Tagen"


# ---------- Handlungsraum (letzter Stand pro Person) + Log ----------

def _handlungsraum_datei(modus: int) -> str:
    return _datei(f"modus{modus}_handlungsraum.json")


def _lade_handlungsraum(modus: int) -> dict:
    if os.path.exists(_handlungsraum_datei(modus)):
        with open(_handlungsraum_datei(modus), encoding="utf-8") as f:
            return json.load(f)
    return {}


# Letzter Handlungsraum pro Modus und Schluessel (person_id bzw. Sitzung)
handlungsraum: dict[int, dict[str, dict]] = {m: _lade_handlungsraum(m) for m in MODI}


def speichere_handlungsraum(modus: int, schluessel: str, stand: dict, status: dict | None):
    """Letzten Handlungsraum merken - Grundlage fuer 'Gerade geaendert' / 'Seit dem letzten Mal'."""
    handlungsraum[modus][schluessel] = {
        "zeit": time.strftime("%Y-%m-%d %H:%M:%S"),
        "stand": {k: stand.get(k) for k in z.EINZELWERTE if stand.get(k) is not None},
        "status": status,
    }
    tmp = _handlungsraum_datei(modus) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(handlungsraum[modus], f, ensure_ascii=False, indent=2)
    os.replace(tmp, _handlungsraum_datei(modus))


def logge(eintrag: dict):
    """Eine Zeile pro Interaktion - fuer die Auswertung der Studie (Vorher/Nachher)."""
    with open(LOG_DATEI, "a", encoding="utf-8") as f:
        f.write(json.dumps(eintrag, ensure_ascii=False) + "\n")


# ---------- Gedaechtnis -> Prompt ----------

def kontext_block(modus: int, stand: dict, status: dict | None, aenderungen: list[str],
                  neue_interaktion: bool, abgelehnt: list[tuple[str, str]],
                  zuletzt_gesehen: float | None, beispiel_gaeste: list[str] | None = None) -> str:
    """Baut den Block, der bei jeder Anfrage an GPT geht - je nach Modus mit anderen Teilen."""
    cfg = MODI[modus]
    zeilen = []

    if cfg["gedaechtnis"]:
        zeilen.append("GEDÄCHTNIS (nur diese Fakten sind sicher):")
        if stand.get("name"):
            zeilen.append(f"Name: {stand['name']} (selbst genannt)")
        if neue_interaktion and zuletzt_gesehen:
            zeilen.append(f"Neue Interaktion: ihr habt zuletzt vor "
                          f"{_zeitspanne(time.time() - zuletzt_gesehen)} gesprochen.")
        if stand.get("wuensche"):
            zeilen.append("Wünsche: " + ", ".join(stand["wuensche"]))
        if stand.get("themen"):
            zeilen.append("Sonstige Gesprächsthemen: " + ", ".join(stand["themen"]))
        if not any(stand.get(k) is not None for k in z.EINZELWERTE):
            zeilen.append("Noch nichts geplant - das ist ein neues Gespräch.")
        zeilen.append("")

    if cfg["zustandsraum"]:
        zeilen.append("PLANUNGSSTAND (vom System berechnet):")
        zeilen += z.zustand_text(stand, status)
    elif cfg["gedaechtnis"]:
        zeilen += z.fakten_text(stand)
    if beispiel_gaeste:
        zeilen.append("- Beispiele von der Gästeliste: " + ", ".join(beispiel_gaeste))
    if cfg["zustandsraum"]:
        zeilen += [""] + z.status_text(status)

    if aenderungen:
        titel = ("SEIT DEM LETZTEN MAL (neue Interaktion):" if neue_interaktion
                 else "GERADE GEÄNDERT (durch diese Nachricht):")
        zeilen += ["", titel] + [f"- {x}" for x in aenderungen]
    if abgelehnt:
        zeilen += ["", "ABGELEHNT IN DIESER NACHRICHT (nicht ausgeführt!):"]
        zeilen += [f"- {z.AKTIONEN[a]['titel']}: {grund}" for a, grund in abgelehnt]

    if cfg["zustandsraum"]:
        schritt = z.naechster_schritt(status)
        zeilen += ["", "NÄCHSTER SCHRITT: " + (z.AKTIONEN[schritt]["titel"] if schritt
                                             else "alles erledigt - Planung abgeschlossen")]
    zeilen += [""] + z.katalog_text(stand, mit_hinweisen=cfg["zustandsraum"])
    return "\n".join(zeilen).strip()


def status_kurz(stand: dict, status: dict) -> list[str]:
    """Kurzfassung fuer die Gedaechtnis-Zeile in der App: z.B. 'Location ⊘'."""
    namen = {"datum": "Datum", "dauer": "Dauer", "personenzahl": "Personen", "budget": "Budget",
             "gaesteliste": "Gästeliste", "location": "Location", "catering": "Catering",
             "programm": "Programm", "einladungen": "Einladungen"}
    teile = []
    for a, e in status.items():
        wert = stand.get(a) if a in ("datum", "personenzahl", "budget", "location") else None
        teile.append(f"{namen[a]}{' ' + str(wert) if wert is not None else ''} {z.SYMBOL[e['status']]}")
    return teile


# ---------- Aussage -> gewuenschte Aenderungen ----------

def analysiere_aussage(aussage: str, stand: dict, pepper_zuletzt: str | None = None) -> tuple[dict, dict]:
    """GPT extrahiert, was die Person aendern/beauftragen will; danach wird alles bereinigt.
    Ob es ERLAUBT ist, entscheidet erst zustand.wende_an().
    pepper_zuletzt: letzte Antwort von Pepper, damit "ja bitte" einem Vorschlag zugeordnet wird.
    -> (bereinigt, roh)  roh = unveraenderte GPT-Ausgabe, fuers Log"""
    bekannt = {k: v for k, v in stand.items()
               if v not in (None, [], "") and not k.endswith("_vorher")}
    system = SYSTEM_EXTRAKTION.format(
        dauer=json.dumps(z.DAUER_OPTIONEN, ensure_ascii=False),
        locations=json.dumps(list(z.LOCATIONS), ensure_ascii=False),
        catering=json.dumps(list(z.CATERING), ensure_ascii=False),
    )
    nachricht = (f"HEUTE: {date.today().strftime('%d.%m.%Y')}\n"
                 f"STAND: {json.dumps(bekannt, ensure_ascii=False)}\n"
                 + (f"PEPPER ZULETZT: \"{pepper_zuletzt}\"\n" if pepper_zuletzt else "")
                 + f"AUSSAGE: {aussage}")
    resp = client.responses.create(
        model=MODELL,
        input=[{"role": "system", "content": system}, {"role": "user", "content": nachricht}],
        max_output_tokens=200,
    )
    roh = parse_json_safely(resp.output_text) or {}
    d = {}

    for k in ("event_typ", "programm", "thema"):
        if roh.get(k):
            d[k] = str(roh[k]).strip().strip('"').strip(".")[:60]
    if roh.get("datum"):
        d["datum"] = z.normalisiere_datum(roh["datum"])
    if roh.get("dauer"):
        d["dauer"] = z.normalisiere_dauer(roh["dauer"])
    if roh.get("personenzahl") is not None:
        d["personenzahl"] = _zahl(roh["personenzahl"], 2000)
    if roh.get("budget") is not None:
        d["budget"] = _zahl(roh["budget"], 1_000_000)
    if roh.get("gaesteliste") is not None:
        d["gaesteliste"] = _zahl(roh["gaesteliste"], 2000)
    if roh.get("location"):
        d["location"] = z.finde_im_katalog(roh["location"], z.LOCATIONS)
    if roh.get("catering"):
        d["catering"] = z.finde_im_katalog(roh["catering"], z.CATERING)
    for k in ("gaesteliste_vorlage", "einladungen_verschicken"):
        if roh.get(k) is True:
            d[k] = True

    d["wuensche_neu"] = [str(w).strip()[:60] for w in roh.get("wuensche_neu") or [] if str(w).strip()]
    vorhanden = {w.lower(): w for w in stand.get("wuensche", [])}
    d["wuensche_entfernt"] = [vorhanden[str(w).lower()] for w in roh.get("wuensche_entfernt") or []
                              if str(w).lower() in vorhanden]
    for t in stand.get("themen", []):
        if d.get("thema", "").lower() == t.lower():
            d["thema"] = t

    name = str(roh.get("name") or "").strip().strip('"').strip(".")
    if name and len(name) <= 20 and " " not in name and name[0].isalpha():
        d["name"] = name[0].upper() + name[1:]

    bereinigt = {k: v for k, v in d.items() if v not in (None, [], "")}
    verworfen = [k for k in roh if k in d and k not in bereinigt]
    if verworfen:
        print(f"[Extraktion] verworfen (ungueltiges Format): { {k: roh[k] for k in verworfen} }")
    return bereinigt, roh


def verarbeite_aussage(modus: int, sp, schluessel: str, aussage: str, stand: dict,
                       pepper_zuletzt: str | None):
    """Extraktion -> (mit Zustandsraum: Regelpruefung) -> Speicher.
    -> (wunsch, roh, gelernt, abgelehnt)"""
    cfg = MODI[modus]
    wunsch, roh = analysiere_aussage(aussage, stand, pepper_zuletzt)
    name = wunsch.get("name") if cfg["gedaechtnis"] else None
    with sp.lock:
        aktuell = sp.stand(schluessel)       # frisch lesen, falls parallel etwas kam
        if cfg["zustandsraum"]:
            gelernt, abgelehnt = z.wende_an(aktuell, wunsch)
        else:
            gelernt, abgelehnt = z.uebernehme_ohne_regeln(aktuell, wunsch), []
        if cfg["gedaechtnis"]:               # Persoenliches nur mit Gedaechtnis
            for k in ("wuensche_neu", "wuensche_entfernt", "thema"):
                if k in wunsch:
                    gelernt[k] = wunsch[k]
            if name:
                sp.setze_name(schluessel, name)
        if gelernt:
            sp.merke_aenderungen(schluessel, gelernt)
    print(f"[Modus {modus}] {schluessel} -> neu gemerkt: "
          f"{({'name': name} if name else {}) | gelernt or '(nichts Neues)'}"
          + (f" | abgelehnt: {abgelehnt}" if abgelehnt else ""))
    return wunsch, roh, gelernt, abgelehnt


def letzte_pepper_antwort(cid: str) -> str | None:
    """Letzte Antwort von Pepper aus dem Chatverlauf (fuer "ja bitte")."""
    for e in reversed(verlauf_holen(cid)):
        if e["role"] == "assistant":
            return e["content"]
    return None


# ---------- Endpunkte ----------

@app.post("/chat")
def chat_standard(request: ChatRequest):
    """Ohne Modusnummer = Modus 1 (damit aeltere App-Versionen weiter funktionieren)."""
    return chat(1, request)


@app.post("/chat/{modus}")
def chat(modus: int, request: ChatRequest):
    if modus not in MODI:
        raise HTTPException(status_code=404, detail="Modus muss 1, 2, 3 oder 4 sein.")
    prompt = (request.text or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Es fehlt 'text'.")
    t_start = time.time()
    cfg = MODI[modus]
    teilnehmer = (request.teilnehmer_id or request.test_person_id
                  or request.conversation_id or "unbekannt").strip()
    sp = speicher[modus]

    # 1. Wer ist da? Mit Gedaechtnis: Gesicht -> person_id. Sonst: Teilnehmer-ID als Sitzung.
    person_id, person_neu, zuletzt_gesehen, schluessel = None, None, None, None
    if cfg["gedaechtnis"]:
        if request.test_person_id:
            person_id, person_neu, zuletzt_gesehen = sp.test_person(f"test_{request.test_person_id}")
        elif request.face_image_base64:
            try:
                person_id, person_neu, zuletzt_gesehen = sp.erkenne(request.face_image_base64)
            except Exception as e:
                print(f"[Person] Gesichtserkennung fehlgeschlagen: {e}")
        schluessel = person_id
    elif sp is not None:
        schluessel = f"sitzung_{teilnehmer}"
    if schluessel is None:
        sp = None          # Modus 4, oder kein Gesicht erkannt -> kein Speicher in dieser Nachricht

    # 2.+3. Aussage auswerten und merken
    verlauf_key = f"m{modus}:{teilnehmer}"
    pepper_zuletzt = letzte_pepper_antwort(verlauf_key)
    wunsch, roh, gelernt, abgelehnt = {}, {}, {}, []
    stand_vorher = sp.stand(schluessel) if sp else {}
    if sp:
        try:
            wunsch, roh, gelernt, abgelehnt = verarbeite_aussage(
                modus, sp, schluessel, prompt, stand_vorher, pepper_zuletzt)
        except openai.APIError:
            raise
        except Exception as e:
            print(f"[Modus {modus}] Aussage konnte nicht verarbeitet werden: {e}")

    # 4. Neuer Stand, Status, Aenderungen
    stand = sp.stand(schluessel) if sp else {}
    status = z.berechne_status(stand) if cfg["zustandsraum"] else None
    letzter = handlungsraum[modus].get(schluessel) if sp else None
    if cfg["zustandsraum"]:
        aenderungen = z.vergleiche(letzter, stand, status)
    else:
        aenderungen = z.vergleiche_fakten(letzter["stand"] if letzter else None, stand)
    # Wiederbegruessung nur mit Gedaechtnis: Person kommt nach einer Pause wieder
    neue_interaktion = cfg["gedaechtnis"] and bool(letzter) and (
        zuletzt_gesehen is None or time.time() - zuletzt_gesehen >= NEUE_INTERAKTION_NACH_MINUTEN * 60)

    # 5. Antwort
    verlauf_merken(verlauf_key, "user", prompt)
    beispiele = (z.gaesteliste_erzeugen(schluessel, stand["gaesteliste"])[:3]
                 if sp and stand.get("gaesteliste") else None)
    block = kontext_block(modus, stand, status, aenderungen, neue_interaktion, abgelehnt,
                          zuletzt_gesehen, beispiele)
    resp = client.responses.create(
        model=MODELL,
        input=[{"role": "system", "content": system_prompt(modus)},
               {"role": "system", "content": block},
               *verlauf_holen(verlauf_key)],
        max_output_tokens=200,
    )
    text = fuer_sprache_kuerzen(resp.output_text)
    verlauf_merken(verlauf_key, "assistant", text)
    antwortzeit = round(time.time() - t_start, 2)

    # 6. Handlungsraum + Log
    if sp:
        speichere_handlungsraum(modus, schluessel, stand, status)
    logge({
        "zeit": time.strftime("%Y-%m-%d %H:%M:%S"), "modus": modus, "teilnehmer_id": teilnehmer,
        "person_id": person_id, "name": stand.get("name"), "aussage": prompt,
        "pepper_zuletzt": pepper_zuletzt, "extrahiert_roh": roh, "extrahiert": wunsch,
        "gelernt": gelernt, "abgelehnt": abgelehnt,
        "stand_vorher": {k: stand_vorher.get(k) for k in z.EINZELWERTE if stand_vorher.get(k) is not None},
        "stand_nachher": {k: stand.get(k) for k in z.EINZELWERTE if stand.get(k) is not None},
        "status": {a: e["status"] for a, e in status.items()} if status else None,
        "aenderungen": aenderungen, "neue_interaktion": neue_interaktion,
        "naechster_schritt": z.naechster_schritt(status) if status else None,
        "antwort": text, "antwortzeit_s": antwortzeit,
    })

    if cfg["zustandsraum"]:
        kurz = status_kurz(stand, status)
    else:
        kurz = z.fakten_kurz(stand)
    ergebnis = antwort(text, cid=verlauf_key, modus=modus, person_id=person_id, person_neu=person_neu,
                       themen=kurz, person_name=stand.get("name"))
    if request.test_person_id:
        # Nur im Testmodus: zeigen, was intern passiert ist (fuer chat_test.py)
        ergebnis["debug"] = {"extrahiert": wunsch, "gelernt": gelernt, "abgelehnt": abgelehnt,
                             "seit_letztem_mal": aenderungen, "neue_interaktion": neue_interaktion,
                             "block": block, "antwortzeit_s": antwortzeit}
    return ergebnis


class ResetRequest(BaseModel):
    teilnehmer_id: str
    modus: int | None = None   # None = alle Modi


@app.post("/sitzung/reset")
def sitzung_reset(req: ResetRequest):
    """Chatverlauf (Arbeitsspeicher) einer Teilnehmerin / eines Teilnehmers loeschen.
    Gespeicherte Planung und ACT-Up-Gedaechtnis bleiben erhalten."""
    modi = [req.modus] if req.modus else list(MODI)
    geloescht = [k for k in (f"m{m}:{req.teilnehmer_id}" for m in modi) if history.pop(k, None)]
    print(f"[Sitzung] Verlauf geloescht: {geloescht or '(nichts)'}")
    return {"ok": True, "geloescht": geloescht}


@app.get("/gaesteliste/{schluessel}")
def gaesteliste(schluessel: str, modus: int = 1):
    """Komplette Gaesteliste ansehen, z.B. /gaesteliste/test_lea?modus=1
    oder /gaesteliste/sitzung_P07?modus=2"""
    sp = speicher.get(modus)
    anzahl = (sp.stand(schluessel).get("gaesteliste") or 0) if sp else 0
    return {"modus": modus, "schluessel": schluessel, "anzahl": anzahl,
            "gaeste": z.gaesteliste_erzeugen(schluessel, anzahl)}


@app.get("/gedaechtnis")
def gedaechtnis(modus: int = 1):
    """ACT-Up-Chunk-Tabelle eines Modus mit Gedaechtnis in der Konsole ausgeben (1 oder 3)."""
    if not MODI.get(modus, {}).get("gedaechtnis"):
        raise HTTPException(status_code=400, detail="Nur Modus 1 und 3 haben ein ACT-Up-Gedaechtnis.")
    speicher[modus].chunk_ausgabe()
    return {"ok": True}


@app.get("/health")
def health():
    return {"ok": True}
