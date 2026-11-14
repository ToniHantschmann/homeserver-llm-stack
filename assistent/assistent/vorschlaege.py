"""Vorschläge für die Weekly Note aus dem Kalender.

Ablauf (Sonntag vor der neuen Woche, ``dienste.woche_vorbereiten``):

1. Python wählt Kandidaten: Termine, vor denen etwas zu tun ist (Prüfung, Frist,
   Vortrag, Geschenk, Wettkampf, Reise), erkannt an Schlüsselwörtern im Titel.
   Routine (gleicher Titel ≥ 3× in fünf Wochen, z. B. "Basistraining") fällt raus.
2. Das LLM formuliert pro Kandidat eine kurze Aufgabe – oder ``null``, wenn eine
   bestehende Aufgabe das schon abdeckt. Jede Antwort wird geprüft; was die Prüfung
   nicht besteht oder fehlt, bekommt den festen Vorlagentext der Kategorie.
3. Die Zeilen landen im Abschnitt "Vorschläge (Assistent)" der Note. Übernehmen =
   Zeile verschieben, ablehnen = löschen. Ein Zustand merkt sich jeden gemachten
   Vorschlag, damit ein gelöschter nicht beim nächsten Lauf wiederkommt.

"Python rechnet, das LLM formuliert": welche Termine und welches Fälligkeitsdatum
entscheidet allein dieser Code.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from .kalender import Termin
from .planung import gleiche_aufgabe
from .vault import _normal

log = logging.getLogger("assistent.vorschlaege")

TAGE_KURZ = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
MAX_VORSCHLAEGE = 8
ROUTINE_AB = 3  # gleicher Titel so oft im Fenster [Montag-14, Montag+21) → Routine


@dataclass(frozen=True)
class Kategorie:
    name: str
    beschreibung: str  # für den Prompt
    woerter: tuple[str, ...]  # Teilstrings im kleingeschriebenen Titel (deutsche Komposita!)
    vorlauf: int  # so viele Tage vor dem Termin wird die Aufgabe relevant
    faellig_vorher: int  # Fälligkeit = Termin minus so viele Tage
    vorlage: str  # Fallback-Text ohne LLM, {titel} = Termintitel
    ausser: tuple[str, ...] = ()  # Titel mit diesen Wörtern passen nicht


# Reihenfolge = Priorität: der erste Treffer zählt. Wörter bewusst lang genug, damit
# sie nicht zufällig in anderen Wörtern stecken ("zug" träfe "Umzug").
KATEGORIEN = (
    Kategorie("pruefung", "Prüfung – lernen", ("klausur", "prüfung", "pruefung", "exam", "kolloquium"),
              10, 1, "Für {titel} lernen"),
    Kategorie("frist", "Frist – rechtzeitig erledigen",
              ("abgabe", "deadline", "frist", "einreichung", "themenwahl", "anmeldeschluss"),
              3, 0, "Erledigen vor Frist: {titel}"),
    Kategorie("vortrag", "Vortrag – vorbereiten",
              ("präsentation", "praesentation", "vortrag", "referat", "pitch", "verteidigung"),
              3, 1, "{titel} vorbereiten"),
    Kategorie("gespraech", "Gespräch – vorbereiten",
              ("bewerbungsgespräch", "vorstellungsgespräch", "bewerbungsgespraech", "interview"),
              2, 1, "{titel} vorbereiten"),
    Kategorie("geschenk", "Feier – Geschenk besorgen",
              ("geburtstag", "einweihung", "hochzeit", "jubiläum", "jubilaeum", "taufe"),
              4, 1, "Geschenk für {titel} besorgen"),
    Kategorie("wettkampf", "Wettkampf – Ausrüstung vorbereiten",
              ("meisterschaft", "liga", "turnier", "wettkampf"),
              2, 1, "Ausrüstung für {titel} vorbereiten", ausser=("training",)),
    Kategorie("reise", "Reise – packen und Tickets prüfen",
              ("flug", "flughafen", "reise", "urlaub", "abfahrt"),
              2, 1, "Packen für {titel}"),
)


def kategorie_fuer(titel: str) -> Kategorie | None:
    t = titel.lower()
    for k in KATEGORIEN:
        if any(w in t for w in k.woerter) and not any(a in t for a in k.ausser):
            return k
    return None


@dataclass
class Kandidat:
    nr: int
    termin: Termin
    kategorie: Kategorie
    faellig: date

    @property
    def datum(self) -> date:
        return self.termin.beginn.date()

    @property
    def schluessel(self) -> str:
        """Gleicher Termin = gleicher Titel am gleichen Tag (auch über mehrere Läufe)."""
        return f"{_normal(self.termin.titel)}|{self.datum.isoformat()}"

    def vorlage(self) -> str:
        return self.kategorie.vorlage.format(titel=self.termin.titel)


def routine_titel(termine: list[Termin]) -> set[str]:
    zaehler = Counter(_normal(t.titel) for t in termine)
    return {t for t, n in zaehler.items() if n >= ROUTINE_AB}


def finde_kandidaten(
    termine: list[Termin],
    montag: date,
    heute: date,
    bestehend: list[str],
    schon_vorgeschlagen: set[str] = frozenset(),
) -> tuple[list[Kandidat], list[str]]:
    """Termine, vor denen in der Woche ab ``montag`` etwas zu tun ist.

    ``termine`` sollte [montag-14, montag+21) abdecken (Routine-Erkennung).
    ``bestehend``: Texte vorhandener Aufgaben/Vorschläge – ein Termin, dessen Titel
    darin vollständig vorkommt, ist schon abgedeckt.
    Gibt (Kandidaten, Gründe für Übersprungenes) zurück.
    """
    routine = routine_titel(termine)
    bestehend_n = [_normal(b) for b in bestehend]
    woche_ende = montag + timedelta(days=7)
    aus: list[Kandidat] = []
    weg: list[str] = []
    gesehen: set[str] = set()
    for t in sorted(termine, key=lambda x: x.beginn):
        d = t.beginn.date()
        k = kategorie_fuer(t.titel)
        if k is None or d < montag:
            continue
        if d - timedelta(days=k.vorlauf) >= woche_ende:
            continue  # wird erst in einer späteren Woche relevant
        n = _normal(t.titel)
        kand = Kandidat(0, t, k, max(d - timedelta(days=k.faellig_vorher), heute))
        if n in routine:
            weg.append(f"{t.titel}: Routine")
        elif kand.schluessel in schon_vorgeschlagen:
            weg.append(f"{t.titel}: schon einmal vorgeschlagen")
        elif kand.schluessel in gesehen:
            continue
        elif any(f" {n} " in f" {b} " for b in bestehend_n):
            weg.append(f"{t.titel}: steht schon in der Note")
        else:
            gesehen.add(kand.schluessel)
            aus.append(kand)
    for i, kand in enumerate(aus[:MAX_VORSCHLAEGE], 1):
        kand.nr = i
    weg += [f"{k.termin.titel}: mehr als {MAX_VORSCHLAEGE} Vorschläge" for k in aus[MAX_VORSCHLAEGE:]]
    return aus[:MAX_VORSCHLAEGE], weg


# ---------------------------------------------------------------- LLM-Antwort prüfen

_ZEIT = re.compile(r"\d{1,2}[:.]\d{2}|\d+\s*uhr", re.I)
_DATUM = re.compile(r"\d{1,2}\.\d{1,2}\.|\d{4}-\d{2}-\d{2}")


def pruefe_text(text: object, titel: str) -> str | None:
    """Der LLM-Text, falls er die Prüfung besteht, sonst ``None`` (→ Vorlage).

    Kurz, einzeilig, keine Uhrzeit/kein Datum (die setzt Python), kein Markdown, und
    mindestens ein Wort aus dem Termintitel kommt vor – sonst ist er nicht mehr
    erkennbar an den Termin gebunden.
    """
    if not isinstance(text, str):
        return None
    s = " ".join(text.split()).strip(" .!-–")
    if not (3 <= len(s) <= 90) or "\n" in text:
        return None
    if _ZEIT.search(s) or _DATUM.search(s) or any(c in s for c in "#*[]`📅"):
        return None
    woerter_titel = {w for w in _normal(titel).split() if len(w) >= 3}
    woerter_text = set(_normal(s).split())
    if woerter_titel and not woerter_titel & woerter_text:
        # Komposita: "GenAI-Klausur" ↔ "Klausur GenAI" ist über _normal schon abgedeckt;
        # "Geburtstagsfeier" ↔ "Geburtstag" über Teilstrings
        if not any(wt in wx or wx in wt for wt in woerter_titel for wx in woerter_text if len(wx) >= 4):
            return None
    return s[0].upper() + s[1:]


@dataclass
class Vorschlag:
    kandidat: Kandidat
    text: str
    quelle: str  # "llm" oder "vorlage"

    def zeile(self) -> str:
        t = self.kandidat.termin
        d = self.kandidat.datum
        return (f"- [ ] {self.text} (Kalender: {TAGE_KURZ[d.weekday()]} {d:%d.%m.} {t.titel}) "
                f"📅 {self.kandidat.faellig.isoformat()}")


def entscheide(
    kandidaten: list[Kandidat], antwort: dict | None, bestehend: list[str],
) -> tuple[list[Vorschlag], list[str]]:
    """LLM-Antwort (``{"1": "Text" | null, …}`` oder ``None`` bei Ausfall) → Vorschläge.

    ``null`` heißt "schon abgedeckt" und wird nur geglaubt, wenn das LLM überhaupt
    geantwortet hat. Ein ungültiger Text fällt auf die Vorlage zurück.
    """
    bestehend_n = [_normal(b) for b in bestehend]
    aus: list[Vorschlag] = []
    weg: list[str] = []
    for k in kandidaten:
        roh = antwort.get(str(k.nr), "fehlt") if antwort is not None else "fehlt"
        if antwort is not None and roh is None:
            weg.append(f"{k.termin.titel}: laut LLM schon abgedeckt")
            continue
        text = pruefe_text(roh, k.termin.titel)
        v = Vorschlag(k, text, "llm") if text else Vorschlag(k, k.vorlage(), "vorlage")
        if any(gleiche_aufgabe(_normal(v.text), b) for b in bestehend_n):
            weg.append(f"{k.termin.titel}: gleiche Aufgabe steht schon in der Note")
            continue
        aus.append(v)
    return aus, weg


def lies_antwort(roh: str) -> dict | None:
    """JSON aus der LLM-Antwort; tolerant gegenüber ```json-Zäunen und Vor-/Nachtext."""
    m = re.search(r"\{.*\}", roh or "", re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return {str(k): v for k, v in d.items()} if isinstance(d, dict) else None


# ---------------------------------------------------------------- Zustand


@dataclass
class Zustand:
    """Welche Termine schon einmal vorgeschlagen wurden (``Kandidat.schluessel``).

    Liegt außerhalb des Vaults (``ASSISTENT_ZUSTAND``, im Container ein Docker-Volume),
    weil jede Datei im Vault auf alle Geräte ginge. Ohne Pfad (Host, Tests) wird nichts
    gespeichert. Geht die Datei verloren, kann höchstens ein gelöschter Vorschlag
    einmal wiederkommen.
    """

    pfad: Path | None
    eintraege: dict[str, dict] = field(default_factory=dict)

    @classmethod
    def lade(cls, verzeichnis: Path | None) -> "Zustand":
        if verzeichnis is None:
            return cls(None)
        pfad = verzeichnis / "vorschlaege.json"
        try:
            daten = json.loads(pfad.read_text(encoding="utf-8"))
            return cls(pfad, daten if isinstance(daten, dict) else {})
        except FileNotFoundError:
            return cls(pfad)
        except (OSError, json.JSONDecodeError) as e:
            log.warning("Zustand %s unlesbar (%s) – starte leer", pfad, e)
            return cls(pfad)

    def schluessel(self) -> set[str]:
        return set(self.eintraege)

    def merke(self, vorschlaege: list[Vorschlag], jetzt: datetime) -> None:
        for v in vorschlaege:
            self.eintraege[v.kandidat.schluessel] = {
                "termin": v.kandidat.datum.isoformat(), "text": v.text, "am": jetzt.isoformat(),
            }

    def speichere(self, heute: date) -> None:
        if self.pfad is None:
            return
        grenze = (heute - timedelta(days=90)).isoformat()
        self.eintraege = {k: v for k, v in self.eintraege.items() if v.get("termin", "") >= grenze}
        tmp = self.pfad.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.eintraege, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.pfad)
