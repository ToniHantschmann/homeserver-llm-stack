# Assistent – Phase 4 (Kern)

Tages- und Wochenzusammenfassung aus Obsidian-Weekly-Note und Google Calendar.
Python rechnet, das LLM (über LiteLLM, Alias `assistent`) formuliert nur.

Liegt als eigenes Git-Repo in `~/docker/llm-stack/assistent` (User `homeserver`), damit
Phase 5 den Container direkt per `build: ./assistent` aus dem Stack bauen kann. Der
ursprünglich geplante Umweg über ein Repo des Users `dev` entfällt (Entscheidung
2026-11-13). **Backup:** die nächtliche Kopie von `~/docker` nimmt nur `*.yml`/`*.yaml`/`.env`
mit, also keinen Python-Code – der Git-Remote ist das Backup.

```
assistent/
  config.py    Umgebungsvariablen (siehe .env.example)
  vault.py     Weekly Note finden/anlegen, Tasks parsen, Zeile einfügen, abhaken
  kalender.py  iCal laden, Serientermine auflösen, Europe/Berlin
  planung.py   Datumsauflösung, Konflikte, freie Zeit, überfällig, Faktenblöcke
  llm.py       Prompts, Halluzinations-/Vollständigkeitsprüfung, Fallback
  vorschlaege.py  Vorbereitungsaufgaben aus dem Kalender (Kandidaten, Prüfung, Zustand)
  dienste.py   gemeinsame Abläufe für CLI und API (Bericht, einfügen, abhaken, vorbereiten)
  api.py       FastAPI für Home Assistant (Phase 5)
  cli.py       assistent tag|woche|tasks|vergleich|neue-woche|hinzufuegen|erledigt|vorbereiten|api
tests/         pytest, ohne Netz und ohne LLM (147 Tests)
Dockerfile     Image für compose-Service "assistent" (uid 1993)
```

## Benutzen

`uv` gibt es auf dem Host nicht systemweit: `nix shell nixpkgs#python3 nixpkgs#uv`.

```bash
uv sync --python "$(command -v python3)"
uv run pytest -q

cp .env.example .env              # LITELLM_API_KEY, ASSISTENT_ICAL eintragen
set -a; . ./.env; set +a
uv run assistent tag --dry-run      # Faktenblock + LLM-Text + Dauer/Tokens
uv run assistent woche --dry-run
uv run assistent tag --datum 2026-09-14 --ohne-llm   # nur Fakten, beliebiger Tag
uv run assistent vergleich tag      # derselbe Faktenblock an assistent und assistent-cloud
uv run assistent tasks --alle
```

Schreibende Befehle (wirken über livesync-bridge **auf allen Geräten**), jeweils mit `--dry-run`:

```bash
uv run assistent neue-woche                       # Note der aktuellen KW anlegen
uv run assistent hinzufuegen "Steuer abgeben" --wann "bis Freitag"
uv run assistent erledigt "Bremsbeläge bestellen"  # Fuzzy-Match, Rückfrage bei Mehrdeutigkeit
```

Der Vault gehört uid 1993 und ist für alle anderen nur lesbar. Auf dem Host (als
`homeserver`) funktionieren deshalb alle lesenden Befehle, die schreibenden scheitern mit
`PermissionError` – gewollt für die Testwoche. Schreiben wird erst im Container
(Phase 5, `user: 1993:1993`) scharf.

## Entscheidungen

- **Nie abschneiden, nie Temp-Dateien.** Einfügen schreibt ab der Einfügestelle weiter
  (Datei wächst nur), Abhaken ist ein 1-Byte-Write `[ ]`→`[x]`. Eine Temp-Datei im Vault
  würde die Bridge auf alle Geräte verteilen; ein `write_text` hätte kurz eine leere Datei.
- **Einfügestelle:** hinter dem letzten echten Task des Abschnitts, vor dem leeren
  Platzhalter `- [ ] ` und dem Trenner `- - - ` – so wie die Notes von Hand geführt werden.
  Datum in der aktuellen Woche → Tagesabschnitt; spätere Woche → deren Note, falls es sie
  gibt, sonst `# ToDo` mit `📅 YYYY-MM-DD` (Tasks-Plugin-Format, wird auch gelesen).
- **Dateinamen ohne Jahr.** `Wochenplanung KW 01.md` von 2026 würde 2027 wieder gefunden.
  Eine Note gilt als veraltet, wenn ihre mtime > 60 Tage vor dem Montag der KW liegt
  (LiveSync überträgt die Original-mtime); sie wird dann weder gelesen noch
  überschrieben, `neue-woche` bricht ab. **Ab KW 01/2027 kollidiert das real** (KW 01–08
  von 2026 existieren) – vorher über ein Namensschema mit Jahr entscheiden, das ist eine
  Zeile: `ASSISTENT_WEEKLY_MUSTER="03 - notes/Wochenplanung {jahr} KW {kw:02d}.md"`.
- **Weitergeschobene Aufgaben.** Dieselbe Aufgabe steht oft Mittwoch offen und Donnerstag
  erledigt. Sie gilt als erledigt, wenn irgendwo in der Note eine gleiche Aufgabe abgehakt
  ist („Fahrrad anschauen“ ≙ „Alex Fahrrad anschauen“: ganze Wörter, ≥ 2 Wörter, ≥ 60 %
  Länge) oder alle ihre Unterpunkte abgehakt sind. Überfällig = offen an einem vergangenen
  Tag der Woche und nicht auf heute/später verschoben, oder `📅` vor heute.
- **Schutz gegen Erfundenes.** Jede Uhrzeit im LLM-Text muss im Faktenblock stehen, und
  jeder Termin aus dem Faktenblock muss im Text vorkommen. Sonst ein Neuversuch mit
  Korrekturhinweis, danach Fallback auf den Faktenblock selbst (steht in `--dry-run`).
- **Ohne Aufgaben-Daten ein eigener Wochen-Prompt.** Im Test hat `gemma4:e4b` bei
  fehlender Note auf die Bitte um „zwei bis drei Beispiele“ hin „Dokument A, Thema B,
  Bericht C“ als erledigt erfunden. Der Faktenblock sagt dann explizit „Keine Aufgaben
  erfasst“, und der Prompt fragt nicht nach Beispielen.
- **Wochenbericht Sonntag 19:00:** Termine zwischen Bericht und Montag (Sonntagabend)
  stehen getrennt unter „Noch vor Wochenbeginn“ – vorher hat das Modell einen Nachtzug
  am Sonntagabend als „in KW 46 absolviert“ gemeldet.
- **Kalender-Fehler sind Fakten.** Eine nicht ladbare Quelle erscheint als Hinweis,
  sonst sähe ein leerer Tag aus wie ein freier. Die iCal-URL wird nie ausgegeben.

## Stand Phase 4 (2026-11-13)

- [x] vault/kalender/planung mit pytest (Serientermine inkl. EXDATE/verschobenem
      Einzeltermin, UTC/Sommerzeit, ganztägig, Sonntag→Montag, KW 53/Jahreswechsel,
      fehlende und veraltete Note, Platzhalter, Unterpunkte, CRLF)
- [x] Prompts für Tag und Woche, `--dry-run` zeigt Fakten, Text, Dauer, Tokens, Fallback
- [ ] **Eine Woche gegen echte Daten prüfen** – braucht die iCal-Adresse und eine
      aktuelle Weekly Note (letzte ist KW 38). Bisher geprüft gegen KW 35/38 und einen
      Test-Kalender: 4,5–15 s pro Bericht, nach den drei Korrekturen oben nichts erfunden.
- [ ] Vergleich lokal vs. Cloud – `assistent vergleich` ist fertig, `assistent-cloud`
      antwortet aber mit 401, solange kein `ANTHROPIC_API_KEY` gesetzt ist (bewusst so
      entschieden). Bis dahin fällt der Cloud-Zweig sauber auf den Faktenblock zurück.

## Phase 5: Zustellung (2026-11-13)

`dienste.py` hält die Abläufe, die CLI und API gleich ausführen (Bericht bauen, Aufgabe
einfügen/abhaken); `cli.py` druckt, `api.py` übersetzt in HTTP. Container-Service
`assistent` in `~/docker/llm-stack/compose.yml`, gebaut aus diesem Verzeichnis
(`Dockerfile`, `.dockerignore` lässt nur `pyproject.toml`, `uv.lock`, `assistent/` durch –
`.env` kommt nie ins Image).

| Methode | Pfad | Zweck |
|---|---|---|
| GET | `/gesund` | Healthcheck, ohne Auth |
| GET | `/zusammenfassung/tag` · `/woche` | `{titel, text, fallback, modell, sekunden}`; `?datum=`, `?ohne_llm=true` |
| GET | `/tasks?offen=true` | Aufgaben der aktuellen Note; 404 `note_fehlt` |
| POST | `/tasks` | `{text, wann?\|faellig?, anlegen=true, dry_run=false}` → `meldung` |
| POST | `/tasks/erledigt` | `{text, dry_run=false}`; 409 `mehrdeutig` mit `kandidaten`, 404 `kein_treffer` |
| POST | `/woche/vorbereiten` | `{datum?, ohne_llm=false, dry_run=false}` → Note nächste Woche + Vorschläge, `meldung`; 409 `note_veraltet` |

Fehler sind immer `{fehler, meldung}`; `meldung` ist ein fertiger Satz für Phase 6.

```bash
cd ~/docker/llm-stack && docker compose up -d --build assistent
docker logs -f assistent                          # pro Bericht: Dauer, Tokens, FALLBACK
. assistent/.env; curl -s -H "Authorization: Bearer $ASSISTENT_API_TOKEN" \
  http://127.0.0.1:8100/zusammenfassung/tag | jq
```

Entscheidungen:

- **Nur `127.0.0.1:8100`, nicht im Tailnet.** Einziger Client ist Home Assistant, das mit
  `network_mode: host` läuft. Bearer-Token `ASSISTENT_API_TOKEN` in `.env` und in HA
  `secrets.yaml` (`assistent_api_auth`); ohne Token (< 24 Zeichen) startet die API nicht.
  Keine `/docs`/`/openapi.json`.
- **`user: 1993:1993`** – nur so darf er in den Vault schreiben. Root-FS read-only,
  `cap_drop: ALL`, nur `llm-network` (LiteLLM) plus Internet für iCal, kein Zugang zur CouchDB.
- **LLM-Ausfall ist kein HTTP-Fehler.** Dann kommt `200` mit dem Faktenblock und
  `fallback` gesetzt – der Push kommt trotzdem, HA hängt „(Fakten)“ an den Titel.
- **Ein Lock für Schreibzugriffe, einer für LLM-Berichte.** Zwei gleichzeitige Einfügungen
  würden sich den Datei-Offset verschieben; Ollama rechnet ohnehin seriell.
- **`POST /tasks` legt eine fehlende Note standardmäßig an** (`anlegen=true`) – „Füge X
  hinzu“ per Sprache soll nicht an einer fehlenden KW-Note scheitern. Eine *veraltete*
  Note (Vorjahr) wird nie überschrieben → 409 `note_veraltet`.
- **Zusammenfassung als Notiz in den Vault (Plan: optional) bewusst nicht gebaut** –
  jede neue Datei dort geht an alle Geräte; der Push reicht.

Home Assistant (`~/docker/homeassistant/config`): `rest_command.assistent_bericht`
(`configuration.yaml`, Timeout 300 s) und Automation `assistent_bericht`
(`automations.yaml`): 06:30 täglich `tag`, So 19:00 `woche`, bei Fehler nach 2 min ein
zweiter Versuch, danach Fehler-Push.

**Fertig, wenn** (Plan) der Push drei Tage in Folge pünktlich und korrekt ankommt.

## Kalender-Vorschläge für die Weekly Note (2026-11-13)

Sonntags 18:45 ruft HA `POST /woche/vorbereiten` auf (Automation
`assistent_woche_vorbereiten`): fehlt die Note der nächsten Woche, legt der Assistent sie
an; dann trägt er Vorbereitungsaufgaben aus dem Kalender in einen eigenen Abschnitt ein.
Der Wochenbericht um 19:00 nennt sie. Uni-Notizen als Quelle sind bewusst **noch nicht**
gebaut (Entscheidung des Operators: erst den Kalender).

```markdown
# Vorschläge (Assistent)
_Aus dem Kalender. Übernehmen: Zeile nach ToDo oder an einen Tag verschieben. Ablehnen: Zeile löschen – sie kommt nicht wieder._
- [ ] Seminar Themenwahl festlegen (Kalender: Di 17.11. Seminar Themenwahl) 📅 2026-11-17
```

```bash
uv run assistent vorbereiten --dry-run                      # was würde eingetragen (Datum = heute)
uv run assistent vorbereiten --dry-run --datum 2026-11-29   # beliebiger Sonntag
uv run assistent vorbereiten --dry-run --ohne-llm           # nur Vorlagentexte
```

Entscheidungen (`vorschlaege.py`):

- **Vorschläge sind keine Aufgaben.** Alles unter „Vorschläge (Assistent)“ steht in
  `WeeklyNote.vorschlaege`, nicht in `.tasks`: kein „überfällig“, kein Abhaken per Sprache,
  nicht in `/tasks`. Erst wenn du die Zeile verschiebst, zählt sie – das `📅` reist mit.
  Die Quellenangabe steht *vor* dem `📅`, damit das Tasks-Plugin das Datum erkennt.
- **Python wählt, das LLM formuliert.** Kandidaten sind Termine mit einem Schlüsselwort
  einer Kategorie (Prüfung, Frist, Vortrag, Gespräch, Geschenk, Wettkampf, Reise; Teilstrings
  wegen Komposita, „Ligatraining“ ausgenommen). Jede Kategorie hat einen Vorlauf (Klausur
  10 Tage, Geschenk 4, Frist 3 …) – daher kann eine Klausur aus der übernächsten Woche schon
  auftauchen – und eine Fälligkeit (Frist: am Tag selbst, sonst Vortag, nie vor heute).
  Erweitern = Eintrag in `KATEGORIEN`.
- **Routine fällt raus:** gleicher Titel ≥ 3× in fünf Wochen (wöchentliche Seminare,
  Trainings). Absichtlich über die Häufigkeit statt über RRULE – jährliche Geburtstage
  sind auch Serien und sollen bleiben.
- **LLM-Antwort wird geprüft:** JSON `{"1": "Text" | null}`. Text muss kurz und einzeilig
  sein, ohne Uhrzeit/Datum/Markdown, und ein Wort aus dem Termintitel enthalten – sonst
  Vorlagentext. `null` = „deckt eine bestehende Aufgabe schon ab“ (bekommt die offenen
  Aufgaben der Ziel- und der laufenden Note mit). Fällt das LLM aus, kommen nur Vorlagen.
- **Höchstens 8 pro Woche.** Echtdaten Okt./Nov. 2026: 0–2 pro Woche, 3–10 s LLM.
- **Gelöscht bleibt gelöscht:** `ASSISTENT_ZUSTAND` (`/zustand`, Named Volume
  `assistent-zustand`) merkt sich „Titel|Datum“ jedes Vorschlags, 90 Tage. Ohne Zustand
  (Host-CLI) wird nur gegen den Inhalt der Note dedupliziert. Das Volume ist bewusst nicht
  im Backup: geht es verloren, kommt höchstens ein gelöschter Vorschlag einmal wieder.
- **Schreibweg wie gehabt:** neue Note per `open(…, "x")`, Vorschläge per Einfügen
  (Datei wächst nur). Fehlt der Abschnitt, wird er am Dateiende angehängt.

## Wochenbericht mit Thinking (2026-11-14)

Nur der Wochenbericht (Batch, So 19:00) läuft über das LiteLLM-Alias
`assistent-gruendlich` = gemma4:e4b mit `think: true` – gleiches Modell, kein Umladen.
Tagesbericht, Vorschläge und Sprache bleiben auf `assistent` (ohne Thinking).

| Variable (compose) | Wert | Warum |
|---|---|---|
| `ASSISTENT_MODELL_WOCHE` | `assistent-gruendlich` | leer = wie `ASSISTENT_MODELL` |
| `ASSISTENT_MAX_TOKENS_WOCHE` | `2500` | Denk-Tokens zählen mit (gemessen ~1200); Ollama-Kontext ist 4096 |
| `ASSISTENT_LLM_TIMEOUT_WOCHE` | `180` | gemessen ~60 s |

Messung KW 46/47, echter Faktenblock:

| Alias | Dauer | Übernahme-Vorschlag | Fehler |
|---|---|---|---|
| `assistent` | 24 s | fehlt | erwähnte „Vorschläge“, die es nicht gab |
| `assistent-gruendlich` | 58–62 s | ja | keine |
| qwen3.5:9b mit Thinking | > 600 s (Timeout) | – | verworfen |

- **Ablauf:** ein Versuch mit `assistent-gruendlich`; scheitert er (erfundene Uhrzeit,
  fehlender Termin, Timeout, `finish_reason == "length"`), formuliert `assistent` einmal
  neu, erst dann kommt der Faktenblock. Summe bleibt unter dem HA-Timeout (300 s).
  Im Log: `Rückfall auf assistent – erster Lauf: …`.
- **Denken kommt getrennt** (`reasoning_content`), nicht als `<think>` im Text – die
  Prüfungen sehen nur den Endtext.
- **Kein 9B-Alias.** Ollama rechnet einen vom Client abgebrochenen Request zu Ende: der
  9B-Timeout blockierte Ollama ~15 min für *alle* Aliase, auch den HA-Sprachagenten.
- **Vorschlags-Satz nur bei Bedarf.** Als feste Bedingung im Prompt hat das Modell ohne
  Thinking Kalender-Vorschläge erwähnt, die es nicht gab; jetzt kommt der Satz nur in den
  Prompt, wenn der Faktenblock welche enthält.
- Vergleich nach ein paar echten Wochen: `uv run assistent vergleich woche`
  (Standard: `assistent,assistent-gruendlich`).
