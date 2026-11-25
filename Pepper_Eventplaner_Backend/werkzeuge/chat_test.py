"""Gespräch mit dem Backend im Terminal simulieren - ohne Pepper, ohne Kamera, MIT echtem GPT.

1. Terminal:  DATEN_ORDNER=test_daten python -m uvicorn main:app --host 0.0.0.0 --port 8000
2. Terminal:  python werkzeuge/chat_test.py        (Test-Person "lea", Modus 1)
              python werkzeuge/chat_test.py max 3  (Test-Person "max", Modus 3)

Modi:  1 = Zustandsraum + Gedächtnis   2 = nur Zustandsraum
       3 = nur Gedächtnis              4 = nur Prompt

Befehle im Chat:
  /modus N       zu Modus N (1-4) wechseln
  /person NAME   zu einer anderen Test-Person wechseln (neuer Name = leerer Stand)
  /reset         Chatverlauf (Arbeitsspeicher) dieser Person im aktuellen Modus löschen
  /block         Block, den GPT bekommt, ein-/ausblenden
  /ende          beenden
"""
import json
import os
import sys
import urllib.request

BASIS = os.getenv("BACKEND_URL", "http://127.0.0.1:8000")
MODUS_NAME = {1: "Zustandsraum + Gedächtnis", 2: "nur Zustandsraum", 3: "nur Gedächtnis", 4: "nur Prompt"}

GRAU, GRUEN, ROT, GELB, BLAU, ENDE = "\033[90m", "\033[92m", "\033[91m", "\033[93m", "\033[94m", "\033[0m"


def post(pfad, daten):
    body = json.dumps(daten).encode("utf-8")
    req = urllib.request.Request(BASIS + pfad, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    person = sys.argv[1] if len(sys.argv) > 1 else "lea"
    modus = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    zeige_block = False
    print(f"Chat als Test-Person '{person}' in Modus {modus} ({MODUS_NAME[modus]}).")
    print("Befehle: /modus N, /person NAME, /reset, /block, /ende\n")

    while True:
        try:
            text = input(f"{BLAU}[M{modus}] Du:{ENDE} ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text:
            continue
        if text == "/ende":
            break
        if text == "/block":
            zeige_block = not zeige_block
            print(f"{GRAU}(Block {'an' if zeige_block else 'aus'}){ENDE}")
            continue
        if text.startswith("/person"):
            teile = text.split(maxsplit=1)
            if len(teile) == 2:
                person = teile[1].strip().lower()
                print(f"{GRAU}(jetzt Test-Person '{person}'){ENDE}")
            continue
        if text.startswith("/modus"):
            teile = text.split()
            if len(teile) == 2 and teile[1] in ("1", "2", "3", "4"):
                modus = int(teile[1])
                print(f"{GRAU}(jetzt Modus {modus}: {MODUS_NAME[modus]}){ENDE}")
            else:
                print(f"{GRAU}(Benutzung: /modus 1 bis /modus 4){ENDE}")
            continue
        if text == "/reset":
            try:
                r = post("/sitzung/reset", {"teilnehmer_id": person, "modus": modus})
                print(f"{GRAU}(Chatverlauf gelöscht: {r['geloescht'] or 'war leer'}){ENDE}")
            except Exception as e:
                print(f"{ROT}Backend nicht erreichbar: {e}{ENDE}")
            continue

        try:
            r = post(f"/chat/{modus}", {"text": text, "test_person_id": person, "teilnehmer_id": person})
        except Exception as e:
            print(f"{ROT}Backend nicht erreichbar: {e}{ENDE}\n")
            continue

        print(f"{GRUEN}Pepper:{ENDE} {r['text']}")
        d = r.get("debug", {})
        if d.get("extrahiert"):
            print(f"{GRAU}  erkannt:   {d['extrahiert']}{ENDE}")
        for aktion, grund in d.get("abgelehnt", []):
            print(f"{ROT}  abgelehnt: {aktion} – {grund}{ENDE}")
        label = "seit letztem Mal" if d.get("neue_interaktion") else "geändert"
        for zeile in d.get("seit_letztem_mal", []):
            print(f"{GELB}  {label}:  {zeile}{ENDE}")
        if r.get("erinnerte_themen"):
            print(f"{GRAU}  Stand:     {' | '.join(r['erinnerte_themen'])}{ENDE}")
        print(f"{GRAU}  ({d.get('antwortzeit_s', '?')} s){ENDE}")
        if zeige_block and d.get("block"):
            print(f"{GRAU}{d['block']}{ENDE}")
        print()


if __name__ == "__main__":
    main()
