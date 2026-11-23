"""Pepper-Eventplaner: Chat-Backend mit ACT-Up-Gedaechtnis.

Ablauf pro Anfrage:
  Gesichtsbild (lokal) -> person_id -> Planungsstand aus ACT-Up
  -> GPT-Antwort mit GEDAECHTNIS-Block (wird IMMER mitgeschickt)
  -> im Hintergrund: Aenderungen (Gaeste, Budget, Wuensche ...) extrahieren und merken

Start (beide Dateien optional, das sind die Standardnamen):
  ACTUP_DATEI=actup_gedaechtnis.json PERSONEN_DATEI=personen_register.json \
      python -m uvicorn main:app --host 0.0.0.0 --port 8000
"""
import json
import os
import re
import time

import openai
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import JSONResponse
from openai import OpenAI
from pydantic import BaseModel

from personen_gedaechtnis import FAKTEN, PersonenErinnerung

MODELL = "gpt-4.1-mini"
MAX_ANTWORT_ZEICHEN = 350

# Kurzzeit-Verlauf: aeltere Nachrichten gehen nicht mehr an GPT, Laengeres
# kommt nur noch aus ACT-Up - Pepper "erinnert" nichts, was ACT-Up vergessen hat.
VERLAUF_NACH_MINUTEN = 5
VERLAUF_MAX_EINTRAEGE = 10

# Was fuer die Planung noch fehlen kann -> "Noch offen" im Gedaechtnis-Block
OFFENE_PUNKTE = [("event_typ", "Art des Events"), ("datum", "Datum"),
                 ("gaeste", "Gästeanzahl"), ("budget", "Budget"), ("ort", "Ort")]

FEHLER_ANTWORT = (
    "Entschuldigung, ich kann gerade nicht richtig nachdenken. "
    "Versuch es bitte gleich noch einmal."
)

SYSTEM_PEPPER = """
Du bist Pepper, ein humanoider Roboter und Assistent für Eventplanung (Partys, Hochzeiten, Feiern).
Du hilfst der Person, ihr Event zu planen und zu gestalten.

Gedächtnis:
- Im Abschnitt GEDÄCHTNIS steht, was du über diese Person und ihr Event weißt.
  Nutze diese Fakten aktiv und erfinde KEINE WEITEREN FAKTEN.
- Wenn sich ein Wert ändert (z.B. mehr Gäste), bestätige diese Änderung kurz ("Alles klar, dann planen wir jetzt mit 80 statt 50 Gästen.") und nenne,
  falls relevant, eine Konsequenz (z.B. für Budget pro Gast oder Raumgröße).
- Fehlt eine wichtige Angabe, frage danach - aber immer nur EINE Frage pro Antwort.

Planung:
- Mach konkrete, realistische Vorschläge, die zu Budget, Gästeanzahl und Location passen.
- Rechne einfach und nachvollziehbar (z.B. Budget pro Gast).
- Wenn das Budget nicht reicht, sag das freundlich und schlage Alternativen vor.

Sprechen:
- Höchstens 2-3 kurze Sätze, keine Listen und keine Emojis und kein Markdown.
- Zahlen ausschreiben, wie man sie spricht ("achttausend Euro").
- Du bist kein ChatGPT und erwähnst keine Modellnamen.
""".strip()

SYSTEM_EXTRAKTION = """
Du extrahierst Planungsdaten aus einer Aussage an den Eventplanungs-Roboter Pepper.
Du bekommst den AKTUELLEN STAND und eine neue AUSSAGE.
Antworte nur mit JSON. Gib nur Felder zurück, die in der Aussage neu genannt oder geändert werden. Alles andere weglassen.
Enthält die Aussage nichts Neues, antworte mit {}.

Felder:
- "name": eigener Vorname der Person
- "event_typ": z.B. "Hochzeit", "Geburtstagsparty"
- "datum": wie genannt, z.B. "24.04.2027"
- "gaeste": Zahl (int). Relative Angaben ausrechnen: "10 mehr" bei Stand 50 -> 60
- "budget": Zahl in Euro (int), "8 Tausend" -> 8000
- "ort": z.B. "Garten", "Lübeck"
- "wuensche_neu": Liste neuer Wünsche, kurz, z.B. ["vegetarisches Buffet"]
- "wuensche_entfernt": Liste von Wünschen aus dem Stand (exakt wie dort geschrieben), die verworfen werden.
- "thema": sonstiges Gesprächsthema in 2-5 Wörtern, sonst weglassen

Regeln für "name":
- Nur wenn die Person ausdrücklich IHREN EIGENEN Vornamen nennt ("Ich heiße Lea", "Mein Name ist Tim").
- Nicht: Namen anderer Personen ("mein Verlobter Tom"), Eigenschaften ("ich bin müde", "ich bin Studentin").

Beispiele:
STAND: {"gaeste": 50, "budget": 8000}
AUSSAGE: "Wir sind jetzt doch 20 Leute mehr, das Budget bleibt aber gleich."
-> {"gaeste": 70}

STAND: {}
AUSSAGE: "Hallo, ich bin Lea und plane die Hochzeit mit meinem Verlobten Tom."
-> {"name": "Lea", "event_typ": "Hochzeit"}

STAND: {"wuensche": ["Live-Band", "Gartenlocation"]}
AUSSAGE: "Die Band ist zu teuer, lass uns lieber einen DJ nehmen."
-> {"wuensche_entfernt": ["Live-Band"], "wuensche_neu": ["DJ"]}
""".strip()


api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
if not api_key:
    raise RuntimeError("OPENAI_API_KEY fehlt oder ist leer")

# timeout 15 s, keine Retries: Pepper sagt lieber sofort Bescheid als lange zu warten
client = OpenAI(api_key=api_key, timeout=15.0, max_retries=0)
# ACT-Up-Chunks (Erinnerungen) und Gesichtsregister werden beim Start geladen
# und nach jeder Aenderung gespeichert.
personen = PersonenErinnerung(
    actup_datei=os.getenv("ACTUP_DATEI", "actup_gedaechtnis.json"),
    personen_datei=os.getenv("PERSONEN_DATEI", "personen_register.json"),
)
app = FastAPI()

# person_id/conversation_id -> [{"role", "content", "ts"}]
history: dict[str, list[dict]] = {}


class ChatRequest(BaseModel):
    text: str
    conversation_id: str | None = None
    # Nur fuer die lokale Gesichtserkennung - geht NICHT an OpenAI
    face_image_base64: str | None = None


def antwort(text, *, cid=None, mode="chat", person_id=None, person_neu=None,
            themen=None, person_name=None) -> dict:
    return {
        "text": text,
        "conversation_id": cid,
        "mode": mode,
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


def verlaeufe_aufraeumen():
    """Loescht bei ALLEN Personen Nachrichten, die aelter als VERLAUF_NACH_MINUTEN sind
    (auch bei Personen, die nicht wiederkommen)."""
    grenze = time.time() - VERLAUF_NACH_MINUTEN * 60
    for cid in list(history):  # list(): Eintraege werden waehrend der Schleife geloescht
        history[cid] = [e for e in history[cid] if e["ts"] >= grenze]
        if not history[cid]:
            del history[cid]


def verlauf_holen(cid: str) -> list[dict]:
    verlaeufe_aufraeumen()
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


# ---------- Gedaechtnis -> Prompt ----------

def erinnerungs_hinweis(stand: dict, zuletzt_gesehen: float | None = None) -> str:
    """Baut den GEDAECHTNIS-Block, der bei jeder Anfrage an GPT geht."""
    z = ["GEDÄCHTNIS (aktueller Stand, nur diese Fakten sind sicher):"]
    if stand.get("name"):
        z.append(f"Name: {stand['name']} (selbst genannt, gelegentlich damit ansprechen)")
    if zuletzt_gesehen and time.time() - zuletzt_gesehen > 30 * 60:
        z.append(f"Ihr habt zuletzt vor {_zeitspanne(time.time() - zuletzt_gesehen)} gesprochen.")

    for key, label in [("event_typ", "Event"), ("datum", "Datum"), ("ort", "Ort"),
                       ("gaeste", "Gäste"), ("budget", "Budget in Euro")]:
        if key in stand:
            vorher = f" (vorher {stand[key + '_vorher']})" if key + "_vorher" in stand else ""
            z.append(f"{label}: {stand[key]}{vorher}")
    if isinstance(stand.get("gaeste"), int) and isinstance(stand.get("budget"), int):
        z.append(f"Budget pro Gast: ca. {stand['budget'] // stand['gaeste']} Euro")
    if stand.get("wuensche"):
        z.append("Wünsche: " + ", ".join(stand["wuensche"]))
    if stand.get("themen"):
        z.append("Sonstige Gesprächsthemen: " + ", ".join(stand["themen"]))

    if len(z) == 1:
        z.append("Noch nichts bekannt - das ist ein neues Gespräch.")
    offen = [name for key, name in OFFENE_PUNKTE if key not in stand]
    if offen:
        z.append("Noch offen: " + ", ".join(offen))
    return "\n".join(z)


def stand_kurz(stand: dict) -> list[str]:
    """Kurzfassung fuer die Gedaechtnis-Zeile in der App."""
    teile = [f"{k}: {stand[k]}" for k in FAKTEN if k in stand]
    return teile + stand.get("wuensche", [])


# ---------- Aussage -> Aenderungen ----------

def analysiere_aussage(aussage: str, stand: dict) -> dict:
    """GPT extrahiert, was in der Aussage neu ist; danach wird alles geprueft."""
    bekannt = {k: v for k, v in stand.items() if v not in (None, [], "")}
    nachricht = f"STAND: {json.dumps(bekannt, ensure_ascii=False)}\nAUSSAGE: {aussage}"
    resp = client.responses.create(
        model=MODELL,
        input=[{"role": "system", "content": SYSTEM_EXTRAKTION},
               {"role": "user", "content": nachricht}],
        max_output_tokens=150,
    )
    roh = parse_json_safely(resp.output_text) or {}
    d = {}

    for k in ("event_typ", "datum", "ort", "thema"):
        if roh.get(k):
            d[k] = str(roh[k]).strip().strip('"').strip(".")[:60]
    if roh.get("gaeste") is not None:
        d["gaeste"] = _zahl(roh["gaeste"], 2000)
    if roh.get("budget") is not None:
        d["budget"] = _zahl(roh["budget"], 1_000_000)

    d["wuensche_neu"] = [str(w).strip()[:60] for w in roh.get("wuensche_neu") or [] if str(w).strip()]
    # Entfernen nur, wenn der Wunsch wirklich im Stand steht (gleiche Schreibweise erzwingen)
    vorhanden = {w.lower(): w for w in stand.get("wuensche", [])}
    d["wuensche_entfernt"] = [vorhanden[str(w).lower()] for w in roh.get("wuensche_entfernt") or []
                              if str(w).lower() in vorhanden]
    # gleiche Schreibweise wie ein bekanntes Thema
    for t in stand.get("themen", []):
        if d.get("thema", "").lower() == t.lower():
            d["thema"] = t

    name = str(roh.get("name") or "").strip().strip('"').strip(".")
    if name and len(name) <= 20 and " " not in name and name[0].isalpha():
        d["name"] = name[0].upper() + name[1:]

    return {k: v for k, v in d.items() if v not in (None, [], "")}


def merke_im_hintergrund(person_id: str, aussage: str):
    """Laeuft NACH dem Absenden der Antwort, damit Pepper nicht laenger wartet."""
    try:
        daten = analysiere_aussage(aussage, personen.stand(person_id))
        name = daten.pop("name", None)
        if name:
            personen.setze_name(person_id, name)
        if daten:
            personen.merke_aenderungen(person_id, daten)
        neu = ({"name": name} if name else {}) | daten
        print(f"[Person] {person_id} -> neu gemerkt: {neu or '(nichts Neues)'}")
    except Exception as e:
        print(f"[Person] Konnte nicht gemerkt werden: {e}")


# ---------- Endpunkte ----------

@app.post("/chat")
def chat(request: ChatRequest, background_tasks: BackgroundTasks):
    prompt = (request.text or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Es fehlt 'text'.")

    # Person erkennen (lokal)
    person_id, person_neu, zuletzt_gesehen = None, None, None
    if request.face_image_base64:
        try:
            person_id, person_neu, zuletzt_gesehen = personen.erkenne(request.face_image_base64)
        except Exception as e:
            print(f"[Person] Gesichtserkennung fehlgeschlagen: {e}")
    stand = personen.stand(person_id) if person_id else {}

    # Verlauf pro Person, damit sich Gespraeche verschiedener Personen nicht mischen
    cid = person_id or request.conversation_id or "default"
    verlauf_merken(cid, "user", prompt)

    gedaechtnis_block = erinnerungs_hinweis(stand, zuletzt_gesehen)
    resp = client.responses.create(
        model=MODELL,
        input=[{"role": "system", "content": SYSTEM_PEPPER},
               {"role": "system", "content": gedaechtnis_block},
               *verlauf_holen(cid)],
        max_output_tokens=200,
    )
    text = fuer_sprache_kuerzen(resp.output_text)
    verlauf_merken(cid, "assistant", text)

    if person_id:
        background_tasks.add_task(merke_im_hintergrund, person_id, prompt)

    return antwort(text, cid=cid, person_id=person_id, person_neu=person_neu,
                   themen=stand_kurz(stand), person_name=stand.get("name"))


@app.get("/gedaechtnis")
def gedaechtnis():
    """Chunk-Tabelle + Planungsstand in der Konsole ausgeben (fuer Demo/Debugging)."""
    personen.chunk_ausgabe()
    return {"ok": True}


@app.get("/health")
def health():
    return {"ok": True}
