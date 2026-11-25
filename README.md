# Pepper-Eventplaner

Pepper Eventplaner für die Studie von Alexander Werk und Anika Halina Manthey.

Der Roboter Pepper hilft als Assistenzsystem beim Planen von Events wie Partys oder Hochzeiten. Die Studie vergleicht in vier Modi, was zwei Bausteine zum Gespräch beitragen:

- **Zustandsraum:** feste Planungsregeln mit Voraussetzungen, Blockaden und Gründen
- **ACT-Up-Gedächtnis:** Erinnerung an Person und Planung, mit Vergessen über die Zeit

| Modus | Bedingung (Tablet) | Zustandsraum | Gedächtnis | Endpunkt |
| --- | --- | --- | --- | --- |
| 1 | A | ja | ja | `POST /chat/1` |
| 2 | B | ja | nein | `POST /chat/2` |
| 3 | C | nein | ja | `POST /chat/3` |
| 4 | D | nein | nein | `POST /chat/4` |

## Aufbau

```
Pepper_Eventplaner_Client/     Android-App für Pepper (QiSDK)
Pepper_Eventplaner_Backend/
├── main.py                    FastAPI-Server: Endpunkte, Prompts, Ablauf pro Modus
├── zustand.py                 Regelschicht: Aktionen, Status, Katalog, Gästeliste
├── personen_gedaechtnis.py    Gesichtserkennung + ACT-Up-Gedächtnis (Modus 1, 3)
├── sitzungs_speicher.py       Planungsspeicher ohne Gedächtnis (Modus 2)
├── requirements.txt
└── werkzeuge/
    ├── chat_test.py           Gespräch im Terminal simulieren (echtes GPT, ohne Pepper)
    └── test_zustand.py        Regeltest ohne GPT und Kamera
```

- **Pepper-App:** Sie übernimmt Spracherkennung (de-DE), Sprachausgabe und das Kamerabild für die Gesichtserkennung. Optional nimmt sie über ein externes Handy-Mikrofon auf (Port 8080). Auf dem Tablet werden Teilnehmer-ID und Bedingung gewählt.
- **Backend:** Es erkennt das Gesicht, extrahiert die Planungsdaten, prüft die Regeln, speichert und loggt.
- **OpenAI `gpt-4.1-mini`:** Es gibt einen Aufruf für die Extraktion und einen für die Antwort. Modus 4 braucht nur den zweiten.

## Ablauf einer Anfrage

1. Die App sendet die Aussage, das Kamerabild und die Teilnehmer-ID an `POST /chat/{modus}`.
2. Das Backend bestimmt den Schlüssel: in Modus 1 und 3 die Person über das Gesicht (`person_…`), in Modus 2 die Sitzung (`sitzung_<Teilnehmer-ID>`).
3. GPT-Aufruf 1 extrahiert die Planungsdaten aus Aussage, aktuellem Stand und Peppers letzter Frage.
4. In Modus 1 und 2 prüft `zustand.wende_an` die Werte: Sie werden übernommen oder mit Grund abgelehnt. Modus 3 übernimmt alles ohne Prüfung.
5. Die Werte werden gespeichert, der Status neu berechnet und mit dem vorherigen Stand verglichen.
6. Das Backend baut einen Kontextblock aus Stand, Status, Änderungen, nächstem Schritt und Katalog.
7. GPT-Aufruf 2 erzeugt die Antwort aus Systemprompt des Modus, Chatverlauf und Kontextblock.
8. Das Backend schreibt eine Zeile ins Log, Pepper spricht die Antwort.

Modus 4 springt von Schritt 1 direkt zu Schritt 6 und bekommt dort nur den Katalog.

## Zustandsraum (`zustand.py`)

Jede Aktion ist **erledigt** (✓), **verfügbar** (○) oder **blockiert** (⊘), jeweils mit Grund. Der Status wird bei jeder Anfrage gegen die aktuellen Werte neu berechnet. Ändert sich z. B. die Personenzahl, sind Gästeliste und Catering wieder offen.

**Fundamentale Aktionen** haben keine Voraussetzungen: Datum, Dauer, Personenzahl, Budget.

**Aufbauende Aktionen:**

| Aktion | Voraussetzungen | Erledigt, wenn … |
| --- | --- | --- |
| Gästeliste | Personenzahl | genug Namen für die Personenzahl |
| Location | Datum, Personenzahl, Budget | Kapazität reicht und die Location ist am Datum frei |
| Catering | Personenzahl, Budget | für die aktuelle Personenzahl bestellt |
| Programm | Dauer, Location | ein konkreter Programmpunkt ist gesetzt |
| Einladungen | Gästeliste, Location, Datum | Einladungsdatum = Eventdatum und alle Gäste eingeladen |

**Katalog** (fest im Code):
- Locations: sechs Einträge mit Standort, Kapazität, Kosten und belegten Tagen
- Catering: Fingerfood 15 €, Buffet 28 €, Drei-Gänge-Menü 45 € pro Person
- Gästeliste: Beispielnamen aus 100 Vornamen × 100 Nachnamen, bis zur Personenzahl gefüllt

## Gedächtnis (`personen_gedaechtnis.py`)

- **Wiedererkennung:** UniFace-Embedding, Kosinus-Ähnlichkeit ab 0,40. Gesichter unter 10 000 Pixel werden ignoriert.
- **ACT-Up:** Jeder Wert ist ein Chunk `{person, attribut, wert}`. Es gilt Decay 0,5 ohne Rauschen, vergessen wird nach 20 160 Minuten (14 Tage).
- **Neuester Wert gewinnt:** Ist der neueste Chunk eines Attributs vergessen, gilt das ganze Attribut als vergessen. Der alte Wert bleibt als `<attribut>_vorher` erhalten, damit Pepper Änderungen benennen kann.
- **Neue Interaktion:** Ab 5 Minuten Pause begrüßt Pepper die Person wieder und nennt, was sich seit dem letzten Mal geändert hat.

## Installation und Start

### Backend (Python 3.12)

```bash
cd Pepper_Eventplaner_Backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt     # mit NVIDIA-GPU: uniface[gpu] statt uniface[cpu]

export OPENAI_API_KEY="sk-..."
python -m uvicorn main:app --host 0.0.0.0 --port 8000
```

Die IP des Rechners bekommst du unter macOS mit `ipconfig getifaddr en0`. Unter `http://<IP>:8000/health` prüfst du, ob das Backend im Netz erreichbar ist. Pepper und Rechner müssen im selben WLAN sein.

### Pepper-App (Android Studio)

1. Öffne den Ordner `Pepper_Eventplaner_Client`.
2. Passe `sdk.dir` in `local.properties` an den eigenen SDK-Pfad an.
3. Stelle das Gradle-JDK auf **JDK 11** (das Projekt nutzt Gradle 6.7.1, QiSDK 1.7.5 und compileSdk 28).
4. Verbinde Pepper mit `adb connect <TABLET-IP>:5555`, wähle Pepper als Gerät und starte mit **Run ▶**.

### Studie durchführen

1. Gib in der App die Backend-Adresse `http://<IP>:8000` ein.
2. Gib im Studien-Dialog die Teilnehmer-ID ein (z. B. `P01`), wähle eine Bedingung A–D und tippe auf **Starten**.
3. Zum Wechseln der Bedingung lange auf `A · P01` unten rechts drücken. Ein Neustart der App ist nicht nötig.
4. Variiere die Reihenfolge der Modi über die Teilnehmenden (lateinisches Quadrat).

## Endpunkte

| Endpunkt | Zweck |
| --- | --- |
| `POST /chat/{modus}` | Gespräch in Modus 1–4 |
| `POST /chat` | wie Modus 1 |
| `POST /sitzung/reset` | Chatverlauf einer Teilnehmer-ID löschen (optional nur ein Modus) |
| `GET /gaesteliste/{schluessel}?modus=1` | Gästeliste einer Person oder Sitzung |
| `GET /gedaechtnis?modus=1` | Inhalt des Gedächtnisses (Modus 1 oder 3) |
| `GET /health` | Erreichbarkeit prüfen |

## Gespeicherte Daten

Alle Studiendaten liegen in `Pepper_Eventplaner_Backend/daten/`. Ein anderer Ordner lässt sich mit `DATEN_ORDNER=...` wählen.

| Datei | Inhalt |
| --- | --- |
| `modus1_personen_register.json`, `modus3_…` | eine Zeile pro Person: Name, zuletzt gesehen, Gesichts-Embedding |
| `modus1_actup_gedaechtnis.json`, `modus3_…` | alle Chunks mit Lernzeit, Aktivierung, Status und ACT-Up-Parametern |
| `modus2_planung.json` | Planungswerte pro Sitzung, ohne Vergessen |
| `modus{1,2,3}_handlungsraum.json` | letzter Stand mit Status, für „seit dem letzten Mal“ |
| `interaktionen_log.jsonl` | eine Zeile pro Anfrage aus allen Modi: Aussage, Extraktion, Gelerntes, Abgelehntes, Stand vorher/nachher, Antwort, Antwortzeit |

Der Chatverlauf liegt nur im Arbeitsspeicher, höchstens 50 Einträge pro Teilnehmer und Modus. Nach einem Neustart des Backends ist er weg.

## Werkzeuge

```bash
cd Pepper_Eventplaner_Backend

# Regeltest (ohne GPT, ohne Kamera)
python werkzeuge/test_zustand.py

# Gespräch simulieren: Backend mit eigenem Datenordner starten …
DATEN_ORDNER=test_daten python -m uvicorn main:app --host 0.0.0.0 --port 8000
# … und im zweiten Terminal chatten (Test-Person, Modus)
python werkzeuge/chat_test.py lea 1
```

Befehle im Chat: `/modus N`, `/person NAME`, `/reset`, `/block` (Kontextblock anzeigen), `/ende`.

## Datenschutz

Register und Log enthalten Gesichts-Embeddings (biometrische Daten), Namen und Gesprächsinhalte. `daten/`, `daten_*/` und `test_daten/` stehen deshalb in der `.gitignore` und dürfen nicht ins Repository. Der OpenAI-Schlüssel wird nur über die Umgebungsvariable `OPENAI_API_KEY` übergeben.
