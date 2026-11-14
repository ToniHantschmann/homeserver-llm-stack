import os
from datetime import date, datetime
from pathlib import Path

import pytest

from assistent.config import STANDARD_WEEKLY_MUSTER

DATEN = Path(__file__).parent / "daten"

# Aufbau wie die echte "Wochenplanung KW 35.md": ToDo-Liste, Aufgaben werden von Tag
# zu Tag weitergeschoben, Unterpunkte mit Tab eingerückt, leere Platzhalter.
KW35 = """# ToDo
- [ ] Jonas antworten
- [x] Socken kaufen
- [ ] 1 Bewerbung abschicken
- [ ] Alex Fahrrad anschauen
---
# Tage:
## Montag:
- [ ] 
- - - 
## Dienstag:
- [ ] 17:45 Uhr: Mama Flughafen
- - - 
## Mittwoch:
- [x] 30 Min. GenAI lernen
- [ ] Jonas antworten
- [ ] Blumen besorgen
- [ ] 
- - - 
## Donnerstag:
- [x] Jonas antworten
- [ ] Sachen packen
\t- [x] Anzug + Hose
\t- [ ] Fliege
- [ ] Bewerbung vorbereiten
- [ ] 
- - - 
## Freitag:
- [ ] Blumen besorgen
- [ ] 
- - - 
## Samstag:
- [ ] 
- - - 
## Sonntag:
- [ ] """  # wie im Original: letzte Zeile ohne Zeilenende


def setze_mtime(pfad: Path, wann: datetime) -> None:
    ts = wann.timestamp()
    os.utime(pfad, (ts, ts))


@pytest.fixture
def vault_dir(tmp_path: Path) -> Path:
    (tmp_path / "03 - notes").mkdir()
    return tmp_path


def lege_note(vault: Path, kw: int, text: str, mtime: datetime, name: str | None = None) -> Path:
    p = vault / (name or STANDARD_WEEKLY_MUSTER.format(kw=kw))
    p.write_text(text, encoding="utf-8")
    setze_mtime(p, mtime)
    return p


MUSTER = STANDARD_WEEKLY_MUSTER
# 2026-08-27 ist der Donnerstag der KW 35/2026
KW35_DONNERSTAG = date(2026, 8, 27)
