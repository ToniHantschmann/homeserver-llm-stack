"""Weekly Notes im Obsidian-Vault: finden, anlegen, Tasks parsen, anhängen, abhaken.

Der Vault-Ordner wird von livesync-bridge in beide Richtungen mit der CouchDB
gespiegelt. Jede Änderung hier landet auf allen Geräten, auch Löschungen und
halb geschriebene Dateien. Daraus folgen die Schreibregeln:

* Es wird nur eine Zeile eingefügt oder ein einzelnes Checkbox-Zeichen geändert,
  nie die ganze Datei neu geschrieben.
* Die Datei wird nie abgeschnitten (kein ``write_text``, kein ``O_TRUNC``): eine
  Einfügung schreibt ab der Einfügestelle weiter, Abhaken ist ein 1-Byte-Write.
* Keine Temp-Dateien im Vault. Die Bridge würde sie auf alle Geräte verteilen.
* Neue Notes werden mit ``open(..., "x")`` angelegt und überschreiben nie etwas.

Dateinamen haben kein Jahr ("Wochenplanung KW 38.md"). Eine Datei mit passender
KW kann also aus dem Vorjahr stammen. Sie gilt als veraltet, wenn ihre mtime
(LiveSync überträgt die Original-mtime) mehr als ``VERALTET_NACH`` vor dem Montag
der gesuchten Woche liegt. Eine veraltete Note wird weder gelesen noch überschrieben.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum
from pathlib import Path

TAGE = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
TODO = "todo"  # Abschnittsschlüssel für "# ToDo"; Wochentage sind 0..6
# Abschnitt "# Vorschläge (Assistent)": vom Assistenten eingetragene Vorschläge aus dem
# Kalender. Sie sind KEINE Aufgaben, bis der Nutzer sie nach ToDo oder an einen Tag
# verschiebt – deshalb stehen sie in WeeklyNote.vorschlaege, nicht in .tasks.
VORSCHLAG = "vorschlag"
VORSCHLAG_UEBERSCHRIFT = "# Vorschläge (Assistent)"
VORSCHLAG_HINWEIS = (
    "_Aus dem Kalender. Übernehmen: Zeile nach ToDo oder an einen Tag verschieben. "
    "Ablehnen: Zeile löschen – sie kommt nicht wieder._"
)
VERALTET_NACH = timedelta(days=60)

_TASK_RE = re.compile(r"^(?P<einzug>[\t ]*)- \[(?P<marke>[ xX])\][ \t]?(?P<text>.*)$")
_UEBERSCHRIFT_RE = re.compile(r"^(?P<ebene>#{1,6})\s*(?P<titel>.*?)\s*:?\s*$")
# "17:45", "9:05 Uhr", "14.30 Uhr" – aber nicht "15.11." (Datum): Punkt nur mit "Uhr"
_UHRZEIT_RE = re.compile(
    r"(?<![\d:.])([01]?\d|2[0-3])(?::([0-5]\d)(?!\d)|\.([0-5]\d)(?=\s*Uhr))"
)
_FAELLIG_RE = re.compile(r"📅\s*(\d{4}-\d{2}-\d{2})")


class VaultFehler(Exception):
    pass


class VeralteteNote(VaultFehler):
    """Unter dem Namen liegt eine Note aus einem früheren Jahr."""


class KeinTreffer(VaultFehler):
    pass


class Mehrdeutig(VaultFehler):
    def __init__(self, kandidaten: list["Task"]):
        self.kandidaten = kandidaten
        super().__init__("mehrdeutig: " + " | ".join(t.text for t in kandidaten))


@dataclass
class Task:
    text: str
    erledigt: bool
    abschnitt: str | int | None  # TODO, 0..6 (Wochentag) oder None (sonstiger Abschnitt)
    zeile: int  # 0-basierter Zeilenindex in der Datei
    ebene: int = 0  # 0 = oberste Ebene, 1 = Unterpunkt, ...
    eltern: str | None = None
    uhrzeit: str | None = None  # "HH:MM", falls im Text eine Uhrzeit steht
    faellig: date | None = None  # aus "📅 YYYY-MM-DD" (Tasks-Plugin-Format)
    unterpunkte: list["Task"] = field(default_factory=list)


def parse_tasks(text: str, mit_vorschlaegen: bool = False) -> list[Task]:
    """Alle Tasks einer Weekly Note, leere Platzhalter ("- [ ] ") ausgenommen.

    Zeilen im Abschnitt "Vorschläge (Assistent)" sind keine Aufgaben und fehlen, außer
    mit ``mit_vorschlaegen=True`` (dann mit ``abschnitt == VORSCHLAG``). So kann z. B.
    "hake X ab" nie einen bloßen Vorschlag treffen.
    """
    alle = _parse_alle(text)
    return alle if mit_vorschlaegen else [t for t in alle if t.abschnitt != VORSCHLAG]


def _parse_alle(text: str) -> list[Task]:
    tasks: list[Task] = []
    abschnitt: str | int | None = None
    stapel: list[Task] = []  # offene Eltern je Ebene
    for i, zeile in enumerate(text.splitlines()):
        u = _UEBERSCHRIFT_RE.match(zeile)
        if u:
            abschnitt = _abschnitt_von(u.group("titel"))
            stapel = []
            continue
        m = _TASK_RE.match(zeile)
        if not m:
            continue
        inhalt = m.group("text").strip()
        if not inhalt:
            continue
        ebene = len(m.group("einzug").expandtabs(4)) // 4
        stapel = stapel[:ebene]
        eltern = stapel[-1] if stapel else None
        t = Task(
            text=inhalt,
            erledigt=m.group("marke") in "xX",
            abschnitt=abschnitt,
            zeile=i,
            ebene=ebene,
            eltern=eltern.text if eltern else None,
            uhrzeit=_uhrzeit(inhalt),
            faellig=_faellig(inhalt),
        )
        if eltern:
            eltern.unterpunkte.append(t)
        tasks.append(t)
        stapel.append(t)
    return tasks


def _abschnitt_von(titel: str) -> str | int | None:
    t = titel.strip().rstrip(":").strip().lower()
    if t in ("todo", "to do", "to-do", "todos"):
        return TODO
    if t.startswith(("vorschläge", "vorschlaege")):
        return VORSCHLAG
    for i, tag in enumerate(TAGE):
        if t == tag.lower():
            return i
    return None


def _uhrzeit(text: str) -> str | None:
    m = _UHRZEIT_RE.search(text)
    return f"{int(m.group(1)):02d}:{m.group(2) or m.group(3)}" if m else None


def _faellig(text: str) -> date | None:
    m = _FAELLIG_RE.search(text)
    if not m:
        return None
    try:
        return date.fromisoformat(m.group(1))
    except ValueError:
        return None


# ---------------------------------------------------------------- finden / anlegen


def montag_der_kw(jahr: int, kw: int) -> date:
    return date.fromisocalendar(jahr, kw, 1)


def weekly_pfad(vault: Path, muster: str, jahr: int, kw: int) -> Path:
    return vault / muster.format(kw=kw, jahr=jahr)


def _finde_case_insensitiv(pfad: Path) -> Path | None:
    """Der Vault ist case-insensitiv ("wochenplanung kw 08.md" = "Wochenplanung KW 08.md")."""
    if not pfad.parent.is_dir():
        return None
    ziel = pfad.name.lower()
    treffer = [p for p in pfad.parent.iterdir() if p.name.lower() == ziel and p.is_file()]
    if not treffer:
        return None
    return max(treffer, key=lambda p: p.stat().st_mtime)


class NoteStatus(Enum):
    OK = "ok"
    FEHLT = "fehlt"
    VERALTET = "veraltet"


@dataclass
class WeeklyNote:
    jahr: int
    kw: int
    status: NoteStatus
    pfad: Path  # bei FEHLT: der Pfad, unter dem sie angelegt würde
    tasks: list[Task] = field(default_factory=list)
    geaendert: datetime | None = None
    vorschlaege: list[Task] = field(default_factory=list)  # Abschnitt "Vorschläge (Assistent)"

    @property
    def montag(self) -> date:
        return montag_der_kw(self.jahr, self.kw)

    def offen(self) -> list[Task]:
        return [t for t in self.tasks if not t.erledigt]


def lade_weekly(vault: Path, muster: str, jahr: int, kw: int) -> WeeklyNote:
    soll = weekly_pfad(vault, muster, jahr, kw)
    pfad = _finde_case_insensitiv(soll)
    if pfad is None:
        return WeeklyNote(jahr, kw, NoteStatus.FEHLT, soll)
    mtime = datetime.fromtimestamp(pfad.stat().st_mtime)
    if mtime.date() < montag_der_kw(jahr, kw) - VERALTET_NACH:
        return WeeklyNote(jahr, kw, NoteStatus.VERALTET, pfad, geaendert=mtime)
    alle = parse_tasks(pfad.read_text(encoding="utf-8"), mit_vorschlaegen=True)
    return WeeklyNote(
        jahr, kw, NoteStatus.OK, pfad,
        tasks=[t for t in alle if t.abschnitt != VORSCHLAG],
        geaendert=mtime,
        vorschlaege=[t for t in alle if t.abschnitt == VORSCHLAG],
    )


def weekly_vorlage() -> str:
    """Gleicher Aufbau wie die bestehenden Notes (KW 35/38)."""
    teile = ["# ToDo", "- [ ] ", "---", "# Tage:"]
    for i, tag in enumerate(TAGE):
        teile += [f"## {tag}:", "- [ ] "]
        if i < len(TAGE) - 1:
            teile.append("- - - ")
    return "\n".join(teile)


def lege_weekly_an(vault: Path, muster: str, jahr: int, kw: int) -> WeeklyNote:
    """Legt die Note an, falls sie fehlt. Überschreibt nie etwas."""
    note = lade_weekly(vault, muster, jahr, kw)
    if note.status is NoteStatus.OK:
        return note
    if note.status is NoteStatus.VERALTET:
        raise VeralteteNote(
            f"{note.pfad.name} stammt vom {note.geaendert:%d.%m.%Y} (Vorjahr?) – "
            "wird nicht überschrieben. Dateinamen brauchen ein Jahr, siehe README."
        )
    note.pfad.parent.mkdir(parents=True, exist_ok=True)
    with open(note.pfad, "x", encoding="utf-8") as f:  # "x": scheitert, falls es sie inzwischen gibt
        f.write(weekly_vorlage())
    return lade_weekly(vault, muster, jahr, kw)


# ---------------------------------------------------------------- schreiben


def _zeilen_mit_ende(daten: bytes) -> list[bytes]:
    return daten.splitlines(keepends=True)


def _einfuegestelle(zeilen: list[str], abschnitt: str | int) -> int | None:
    """Zeilenindex, vor dem eingefügt wird: hinter dem letzten echten Task des Abschnitts.

    Leere Platzhalter und der Trenner "- - -" bleiben dahinter stehen. Hat der Abschnitt
    keinen echten Task, wird direkt unter der Überschrift eingefügt.
    """
    start = None
    for i, z in enumerate(zeilen):
        u = _UEBERSCHRIFT_RE.match(z)
        if u and _abschnitt_von(u.group("titel")) == abschnitt:
            start = i
            break
    if start is None:
        return None
    ende = len(zeilen)
    for i in range(start + 1, len(zeilen)):
        if _UEBERSCHRIFT_RE.match(zeilen[i]):
            ende = i
            break
    letzte = start
    for i in range(start + 1, ende):
        m = _TASK_RE.match(zeilen[i])
        if m and m.group("text").strip():
            letzte = i
    if letzte == start and abschnitt == VORSCHLAG:
        # Alle Vorschläge gelöscht: neue hinter den Hinweistext, nicht davor
        for i in range(start + 1, ende):
            if zeilen[i].strip() and not zeilen[i].strip().startswith(("---", "- - -")):
                letzte = i
    return letzte + 1


def _fuege_ein(f, daten: bytes, idx: int | None, neue: list[str], kopf: list[str] | None = None) -> None:
    """Schreibt ``neue`` Zeilen vor Zeile ``idx`` der offenen Datei ``f`` (Inhalt ``daten``).

    ``idx is None``: ans Dateiende, davor eine Leerzeile und ``kopf`` (z. B. Überschrift).
    Die Datei wächst nur: ab der Einfügestelle wird der alte Rest dahinter neu
    geschrieben, nie abgeschnitten.
    """
    nl = b"\r\n" if b"\r\n" in daten else b"\n"
    roh = _zeilen_mit_ende(daten)
    if idx is None:
        block = ([""] + kopf if kopf and daten else (kopf or [])) + neue
        vor = nl if daten and not daten.endswith(b"\n") else b""
        f.seek(len(daten))
        f.write(vor + nl.join(z.encode("utf-8") for z in block) + nl)
        return
    offset = sum(len(z) for z in roh[:idx])
    rest = daten[offset:]
    neu = b"".join(z.encode("utf-8") + nl for z in neue)
    if offset == len(daten) and daten and not daten.endswith(b"\n"):
        neu = nl + neu[: -len(nl)]  # letzte Zeile hat kein Zeilenende
    f.seek(offset)
    f.write(neu + rest)


def haenge_vorschlaege_an(pfad: Path, zeilen: list[str]) -> None:
    """Trägt ``- [ ] …``-Zeilen in den Abschnitt "Vorschläge (Assistent)" ein.

    Gibt es den Abschnitt noch nicht, wird er am Dateiende angehängt (mit Hinweiszeile);
    sonst landen die Zeilen hinter dem letzten Vorschlag darin.
    """
    if not zeilen:
        return
    with open(pfad, "r+b") as f:
        daten = f.read()
        text = [z.decode("utf-8").rstrip("\r\n") for z in _zeilen_mit_ende(daten)]
        idx = _einfuegestelle(text, VORSCHLAG)
        _fuege_ein(f, daten, idx, zeilen, kopf=[VORSCHLAG_UEBERSCHRIFT, VORSCHLAG_HINWEIS])


def haenge_task_an(
    pfad: Path, text: str, tag: int | None = None, faellig: date | None = None
) -> str:
    """Fügt ``- [ ] text`` in den Abschnitt des Wochentags ``tag`` ein (sonst unter ToDo).

    ``faellig`` wird nur als "📅 YYYY-MM-DD" angehängt, wenn kein Wochentag-Abschnitt
    gewählt ist – innerhalb der Woche ist der Abschnitt selbst das Datum.
    Gibt die geschriebene Zeile zurück.
    """
    text = " ".join(text.split())
    if not text:
        raise VaultFehler("leerer Aufgabentext")
    zeile = f"- [ ] {text}"
    if faellig and tag is None:
        zeile += f" 📅 {faellig.isoformat()}"

    with open(pfad, "r+b") as f:
        daten = f.read()
        roh = _zeilen_mit_ende(daten)
        zeilen = [z.decode("utf-8").rstrip("\r\n") for z in roh]
        idx = _einfuegestelle(zeilen, TODO if tag is None else tag)
        if idx is None:
            idx = len(roh)  # Abschnitt fehlt: ans Dateiende
        _fuege_ein(f, daten, idx, [zeile])  # Datei wächst nur, wird nie abgeschnitten
    return zeile


def _normal(s: str) -> str:
    s = _FAELLIG_RE.sub("", s).lower()
    return " ".join(re.sub(r"[^\wäöüß ]", " ", s).split())


def finde_offene(tasks: list[Task], suchtext: str, schwelle: float = 0.6) -> Task:
    """Fuzzy-Match für "Hake {aufgabe} ab" (Whisper-Hörfehler, ungefähre Formulierung)."""
    such = _normal(suchtext)
    if not such:
        raise KeinTreffer("leerer Suchtext")
    bewertet: list[tuple[float, Task]] = []
    for t in tasks:
        if t.erledigt:
            continue
        kand = _normal(t.text)
        score = difflib.SequenceMatcher(None, such, kand).ratio()
        if such in kand or kand in such:
            score = max(score, 0.9 if such == kand else 0.8)
        if such == kand:
            score = 1.0
        bewertet.append((score, t))
    bewertet.sort(key=lambda x: x[0], reverse=True)
    if not bewertet or bewertet[0][0] < schwelle:
        raise KeinTreffer(f"keine offene Aufgabe passt zu „{suchtext}“")
    beste = bewertet[0][0]
    nah = [t for s, t in bewertet if s >= schwelle and beste - s < 0.05]
    # Gleicher Text mehrfach offen (z.B. Montag und Dienstag) ist keine Mehrdeutigkeit:
    # der früheste wird abgehakt.
    if len({_normal(t.text) for t in nah}) > 1:
        raise Mehrdeutig(nah)
    return min(nah, key=lambda t: t.zeile)


def hake_ab(pfad: Path, suchtext: str) -> Task:
    """Setzt genau ein Zeichen: ``[ ]`` → ``[x]`` in der passenden offenen Aufgabe."""
    with open(pfad, "r+b") as f:
        daten = f.read()
        text = daten.decode("utf-8")
        task = finde_offene(parse_tasks(text), suchtext)
        roh = _zeilen_mit_ende(daten)
        zeile = roh[task.zeile]
        pos = zeile.find(b"- [ ]")
        if pos < 0:
            raise VaultFehler("Checkbox nicht gefunden (Datei inzwischen geändert?)")
        offset = sum(len(z) for z in roh[: task.zeile]) + pos + 3
        if daten[offset : offset + 1] != b" ":
            raise VaultFehler("unerwarteter Inhalt an der Checkbox – nichts geschrieben")
        f.seek(offset)
        f.write(b"x")
    task.erledigt = True
    return task
