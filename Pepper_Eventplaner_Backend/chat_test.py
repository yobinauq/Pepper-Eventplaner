"""Gespräch mit dem Backend im Terminal simulieren - ohne Pepper, ohne Kamera, MIT echtem GPT.

1. Terminal:  python -m uvicorn main:app --host 0.0.0.0 --port 8000
2. Terminal:  python chat_test.py            (Test-Person "lea")
              python chat_test.py max        (andere Test-Person -> eigenes Gedächtnis)

Befehle im Chat:
  /block         GEDÄCHTNIS-Block, den GPT bekommt, ein-/ausblenden
  /person NAME   zu einer anderen Test-Person wechseln (neuer Name = leeres Gedächtnis)
  /ende          beenden
"""
import json
import os
import sys
import urllib.request

URL = os.getenv("BACKEND_URL", "http://127.0.0.1:8000") + "/chat"

GRAU, GRUEN, ROT, GELB, BLAU, ENDE = "\033[90m", "\033[92m", "\033[91m", "\033[93m", "\033[94m", "\033[0m"


def sende(text, person):
    body = json.dumps({"text": text, "test_person_id": person}).encode("utf-8")
    req = urllib.request.Request(URL, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    person = sys.argv[1] if len(sys.argv) > 1 else "lea"
    zeige_block = False
    print(f"Chat mit Pepper als Test-Person '{person}'. Befehle: /block, /person NAME, /ende\n")

    while True:
        try:
            text = input(f"{BLAU}Du:{ENDE} ").strip()
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

        try:
            r = sende(text, person)
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
        print(f"{GRAU}  Status:    {' | '.join(r.get('erinnerte_themen', []))}{ENDE}")
        if zeige_block and d.get("block"):
            print(f"{GRAU}{d['block']}{ENDE}")
        print()


if __name__ == "__main__":
    main()
