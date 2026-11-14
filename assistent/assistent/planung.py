"""Deterministischer Teil: Datumsauflösung, Konflikte, freie Zeit, offene/überfällige Tasks.

Grundprinzip: Python rechnet, das LLM formuliert. Alles, was hier herauskommt,
ist getestet und landet als Faktenblock im Prompt – und dient als Text-Fallback,
wenn das LLM nicht erreichbar ist oder Uhrzeiten erfindet.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .kalender import Termin
from .vault import TAGE, TODO, NoteStatus, Task, WeeklyNote, _normal

# ---------------------------------------------------------------- Datum


def kw_von(d: date) -> tuple[int, int]:
    j, w, _ = d.isocalendar()
    return j, w


_ZAHLWOERTER = {
    "einem": 1, "einen": 1, "einer": 1, "eins": 1, "ein": 1, "zwei": 2, "drei": 3, "vier": 4,
    "fünf": 5, "fuenf": 5, "sechs": 6, "sieben": 7, "acht": 8, "neun": 9, "zehn": 10,
}
_TAG_KURZ = {t.lower(): i for i, t in enumerate(TAGE)} | {
    t[:2].lower(): i for i, t in enumerate(TAGE)
}


def _zahl(s: str) -> int | None:
    return int(s) if s.isdigit() else _ZAHLWOERTER.get(s)


def loese_datum(ausdruck: str, heute: date) -> date | None:
    """Freie deutsche Datumsangabe → Datum. ``None``, wenn nicht erkannt.

    Wochentag allein = nächstes Vorkommen, heute eingeschlossen ("bis Freitag" am
    Freitag ist heute). "nächsten Freitag" = Freitag der nächsten Kalenderwoche.
    """
    s = ausdruck.lower().strip().rstrip(".!?")
    s = re.sub(r"^(bis|am|zum|spätestens|spaetestens|ab|für|fuer)\s+", "", s).strip()

    if s in ("heute", "heut"):
        return heute
    if s == "morgen":
        return heute + timedelta(days=1)
    if s in ("übermorgen", "uebermorgen"):
        return heute + timedelta(days=2)
    if s in ("wochenende", "zum wochenende", "am wochenende"):
        return heute if heute.weekday() >= 5 else heute + timedelta(days=5 - heute.weekday())
    if s in ("nächste woche", "naechste woche", "kommende woche"):
        return heute + timedelta(days=7 - heute.weekday())

    m = re.fullmatch(r"(?:(?:nächsten|naechsten|kommenden|nächster|naechster)\s+(\w+)"
                     r"|(\w+)\s+(?:nächste|naechste|kommende)\s+woche"
                     r"|(?:nächste|naechste|kommende)\s+woche\s+(\w+))", s)
    if m:
        name = next(g for g in m.groups() if g)
        if name in _TAG_KURZ:
            montag_naechste = heute + timedelta(days=7 - heute.weekday())
            return montag_naechste + timedelta(days=_TAG_KURZ[name])
        return None

    if s in _TAG_KURZ:
        return heute + timedelta(days=(_TAG_KURZ[s] - heute.weekday()) % 7)

    m = re.fullmatch(r"in\s+(\w+)\s+(tag|tagen|woche|wochen)", s)
    if m:
        n = _zahl(m.group(1))
        if n is None:
            return None
        return heute + timedelta(days=n * (7 if m.group(2).startswith("woche") else 1))

    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None

    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.?(\d{2,4})?", s)
    if m:
        t, mo = int(m.group(1)), int(m.group(2))
        try:
            if m.group(3):
                j = int(m.group(3))
                return date(j + 2000 if j < 100 else j, mo, t)
            d = date(heute.year, mo, t)
            return d if d >= heute else date(heute.year + 1, mo, t)
        except ValueError:
            return None
    return None


@dataclass(frozen=True)
class Ziel:
    """Wohin eine neue Aufgabe geschrieben wird."""

    jahr: int
    kw: int
    tag: int | None  # Wochentag-Abschnitt oder None = "# ToDo"
    faellig: date | None  # nur gesetzt, wenn es als 📅 in die ToDo-Liste geht


def ziel_fuer(faellig: date | None, heute: date, note_existiert=lambda j, w: False) -> Ziel:
    """Aufgabe mit Datum in der aktuellen Woche → Tagesabschnitt. In einer späteren
    Woche → deren Note, falls es sie schon gibt, sonst ToDo der aktuellen Woche mit 📅.
    Ohne Datum (oder Datum in der Vergangenheit) → ToDo der aktuellen Woche."""
    j, w = kw_von(heute)
    if faellig is None or faellig < heute:
        return Ziel(j, w, None, None)
    fj, fw = kw_von(faellig)
    if (fj, fw) == (j, w):
        return Ziel(j, w, faellig.weekday(), None)
    if note_existiert(fj, fw):
        return Ziel(fj, fw, faellig.weekday(), None)
    return Ziel(j, w, None, faellig)


# ---------------------------------------------------------------- Kalender-Logik


def konflikte(termine: list[Termin]) -> list[tuple[Termin, Termin]]:
    zeitlich = [t for t in termine if not t.ganztags and t.ende > t.beginn]
    paare = []
    for i, a in enumerate(zeitlich):
        for b in zeitlich[i + 1 :]:
            if a.beginn < b.ende and b.beginn < a.ende:
                paare.append((a, b))
    return paare


def freie_slots(
    termine: list[Termin],
    tag: date,
    tz: ZoneInfo,
    beginn: int = 8,
    ende: int = 21,
    min_minuten: int = 30,
    jetzt: datetime | None = None,
) -> list[tuple[datetime, datetime]]:
    t0 = datetime.combine(tag, time(beginn), tz)
    t1 = datetime.combine(tag, time(ende), tz)
    if jetzt is not None and jetzt.date() == tag:
        # auf die nächste Viertelstunde aufrunden
        j = jetzt.astimezone(tz).replace(second=0, microsecond=0)
        j += timedelta(minutes=(-j.minute) % 15)
        t0 = max(t0, j)
    belegt = sorted(
        (max(t.beginn, t0), min(t.ende, t1))
        for t in termine
        if not t.ganztags and t.ende > t0 and t.beginn < t1
    )
    frei, pos = [], t0
    for b, e in belegt:
        if b > pos:
            frei.append((pos, b))
        pos = max(pos, e)
    if pos < t1:
        frei.append((pos, t1))
    return [(a, b) for a, b in frei if (b - a) >= timedelta(minutes=min_minuten)]


# ---------------------------------------------------------------- Task-Logik


def gleiche_aufgabe(a: str, b: str) -> bool:
    """Gleiche Aufgabe trotz leicht anderer Formulierung (beide schon ``_normal``).

    "fahrrad anschauen" ≙ "alex fahrrad anschauen". Der kürzere Text muss als ganze
    Wörter im längeren stehen, mindestens 2 Wörter und 60 % der Länge haben – sonst
    würde "laufen" auch "laufen mit jonas" erledigen.
    """
    if a == b:
        return True
    k, lang = sorted((a, b), key=len)
    return len(k.split()) >= 2 and len(k) >= 0.6 * len(lang) and f" {k} " in f" {lang} "


def fertig_pruefer(note: WeeklyNote):
    """Die Notes werden von Tag zu Tag weitergeschoben: dieselbe Aufgabe steht Mittwoch
    offen und Donnerstag erledigt. Eine Aufgabe gilt als erledigt, wenn sie abgehakt ist,
    irgendwo in der Note eine gleiche Aufgabe abgehakt ist, oder alle ihre Unterpunkte
    abgehakt sind."""
    fertig = [_normal(t.text) for t in note.tasks if t.erledigt]

    def ist_fertig(t: Task) -> bool:
        if t.erledigt or (t.unterpunkte and all(u.erledigt for u in t.unterpunkte)):
            return True
        n = _normal(t.text)
        return any(gleiche_aufgabe(n, f) for f in fertig)

    return ist_fertig


def offene_oben(note: WeeklyNote, abschnitt) -> list[Task]:
    """Offene Aufgaben der obersten Ebene eines Abschnitts, ohne Dubletten."""
    ist_fertig = fertig_pruefer(note)
    gesehen, aus = set(), []
    for t in note.tasks:
        n = _normal(t.text)
        if t.abschnitt == abschnitt and t.ebene == 0 and not ist_fertig(t) and n not in gesehen:
            gesehen.add(n)
            aus.append(t)
    return aus


def ueberfaellig(note: WeeklyNote, heute: date) -> list[Task]:
    """Offen in einem vergangenen Tag dieser Woche (und nicht auf heute/später verschoben)
    oder mit 📅-Datum vor heute."""
    if note.status is not NoteStatus.OK:
        return []
    ist_fertig = fertig_pruefer(note)
    gleiche_woche = kw_von(heute) == (note.jahr, note.kw)
    spaeter = {
        _normal(t.text)
        for t in note.tasks
        if isinstance(t.abschnitt, int) and gleiche_woche and t.abschnitt >= heute.weekday()
    }
    aus, gesehen = [], set()
    for t in note.tasks:
        n = _normal(t.text)
        if t.ebene != 0 or n in gesehen or ist_fertig(t):
            continue
        vergangen = isinstance(t.abschnitt, int) and (
            (gleiche_woche and t.abschnitt < heute.weekday())
            or (not gleiche_woche and note.montag < heute)
        )
        if (vergangen and n not in spaeter) or (t.faellig and t.faellig < heute):
            gesehen.add(n)
            aus.append(t)
    return aus


# ---------------------------------------------------------------- Fakten


KEINE_AUFGABEN = "Keine Aufgaben erfasst (keine gültige Weekly Note)."


def _hm(d: datetime) -> str:
    return d.strftime("%H:%M")


def termin_zeile(t: Termin, tag: date | None = None) -> str:
    if t.ganztags:
        zeit = "ganztägig"
    elif tag and t.beginn.date() != tag:
        zeit = f"bis {_hm(t.ende)} (seit {t.beginn:%d.%m.})"
    elif tag and t.ende.date() != tag and t.ende > t.beginn:
        zeit = f"ab {_hm(t.beginn)} (bis {t.ende:%d.%m.} {_hm(t.ende)})"
    elif t.ende > t.beginn:
        zeit = f"{_hm(t.beginn)}–{_hm(t.ende)}"
    else:
        zeit = _hm(t.beginn)
    ort = f" ({t.ort})" if t.ort else ""
    return f"{zeit} {t.titel}{ort}"


def task_zeile(t: Task) -> str:
    s = t.text
    offen_unter = [u.text for u in t.unterpunkte if not u.erledigt]
    if t.unterpunkte:
        s += f" [{len(t.unterpunkte) - len(offen_unter)}/{len(t.unterpunkte)} Unterpunkte erledigt"
        s += f"; offen: {', '.join(offen_unter)}]" if offen_unter else "]"
    return s


def _note_hinweis(note: WeeklyNote) -> str | None:
    if note.status is NoteStatus.FEHLT:
        return f"Für KW {note.kw:02d} gibt es noch keine Weekly Note."
    if note.status is NoteStatus.VERALTET:
        return (f"Die Datei für KW {note.kw:02d} stammt vom {note.geaendert:%d.%m.%Y} "
                "(früheres Jahr) und wird ignoriert.")
    return None


@dataclass
class TagesFakten:
    datum: date
    termine: list[Termin]
    konflikte: list[tuple[Termin, Termin]]
    frei: list[tuple[datetime, datetime]]
    tasks_heute: list[Task]
    erledigt_heute: int
    wochen_todo: list[Task]
    ueberfaellig: list[Task]
    note: WeeklyNote
    hinweise: list[str] = field(default_factory=list)


def tages_fakten(
    datum: date,
    note: WeeklyNote,
    termine: list[Termin],
    kalender_fehler: list[str],
    tz: ZoneInfo,
    beginn: int = 8,
    ende: int = 21,
    jetzt: datetime | None = None,
) -> TagesFakten:
    heute_termine = [t for t in termine if t.betrifft(datum)]
    hinweise = list(kalender_fehler)
    if h := _note_hinweis(note):
        hinweise.append(h)
    tasks_heute: list[Task] = []
    erledigt = 0
    wochen_todo: list[Task] = []
    if note.status is NoteStatus.OK:
        tasks_heute = offene_oben(note, datum.weekday())
        ist_fertig = fertig_pruefer(note)
        tasks_heute += [
            t for t in note.tasks
            if t.faellig == datum and t.ebene == 0 and not ist_fertig(t) and t not in tasks_heute
        ]
        erledigt = sum(
            1 for t in note.tasks if t.abschnitt == datum.weekday() and t.erledigt and t.ebene == 0
        )
        wochen_todo = [t for t in offene_oben(note, TODO) if t not in tasks_heute]
    return TagesFakten(
        datum=datum,
        termine=heute_termine,
        konflikte=konflikte(heute_termine),
        frei=freie_slots(heute_termine, datum, tz, beginn, ende, jetzt=jetzt),
        tasks_heute=tasks_heute,
        erledigt_heute=erledigt,
        wochen_todo=wochen_todo,
        ueberfaellig=ueberfaellig(note, datum),
        note=note,
        hinweise=hinweise,
    )


def tages_text(f: TagesFakten) -> str:
    z = [f"Datum: {TAGE[f.datum.weekday()]}, {f.datum:%d.%m.%Y} (KW {f.note.kw:02d})"]
    z.append("Termine heute:" if f.termine else "Termine heute: keine")
    z += [f"- {termin_zeile(t, f.datum)}" for t in f.termine]
    if f.konflikte:
        z.append("Überschneidungen:")
        z += [f"- {a.titel} ({_hm(a.beginn)}) und {b.titel} ({_hm(b.beginn)})" for a, b in f.konflikte]
    if f.frei:
        z.append("Freie Zeit: " + ", ".join(f"{_hm(a)}–{_hm(b)}" for a, b in f.frei))
    z.append("Aufgaben für heute:" if f.tasks_heute else "Aufgaben für heute: keine eingetragen")
    z += [f"- {task_zeile(t)}" for t in f.tasks_heute]
    if f.erledigt_heute:
        z.append(f"Heute schon erledigt: {f.erledigt_heute}")
    if f.ueberfaellig:
        z.append("Überfällig (aus vergangenen Tagen):")
        z += [f"- {task_zeile(t)} ({TAGE[t.abschnitt] if isinstance(t.abschnitt, int) else 'fällig ' + t.faellig.strftime('%d.%m.')})"
              for t in f.ueberfaellig]
    if f.wochen_todo:
        z.append("Offen auf der Wochenliste:")
        z += [f"- {task_zeile(t)}" for t in f.wochen_todo]
    if f.hinweise:
        z.append("Hinweise:")
        z += [f"- {h}" for h in f.hinweise]
    return "\n".join(z)


@dataclass
class WochenFakten:
    note: WeeklyNote
    erledigt: list[Task]
    offen: list[Task]
    naechste: WeeklyNote
    naechste_offen: list[Task]
    termine_naechste: list[Termin]
    konflikte_naechste: list[tuple[Termin, Termin]]
    termine_rest: list[Termin] = field(default_factory=list)  # zwischen Bericht und Montag
    hinweise: list[str] = field(default_factory=list)
    vorschlaege: list[Task] = field(default_factory=list)  # offene Vorschläge der nächsten Note


def wochen_fakten(
    note: WeeklyNote,
    naechste: WeeklyNote,
    termine: list[Termin],
    kalender_fehler: list[str],
    ab: datetime | None = None,
) -> WochenFakten:
    """Rückblick auf die Woche von ``note``, Ausblick auf ``naechste``.

    Termine, die nach ``ab`` (Zeitpunkt des Berichts) aber vor dem Montag beginnen –
    etwa Sonntagabend – stehen getrennt in ``termine_rest``, sonst würden sie dem
    Rückblick als "erledigt" oder der nächsten Woche zugeschlagen.
    """
    hinweise = list(kalender_fehler)
    if h := _note_hinweis(note):
        hinweise.append(h)
    erledigt, offen = [], []
    if note.status is NoteStatus.OK:
        ist_fertig = fertig_pruefer(note)
        gesehen: list[str] = []
        for t in note.tasks:
            n = _normal(t.text)
            if t.ebene != 0 or any(gleiche_aufgabe(n, g) for g in gesehen):
                continue
            gesehen.append(n)
            (erledigt if ist_fertig(t) else offen).append(t)
    naechste_offen = naechste.offen() if naechste.status is NoteStatus.OK else []
    naechste_offen = [t for t in naechste_offen if t.ebene == 0]
    mo = datetime.combine(naechste.montag, time(0), termine[0].beginn.tzinfo) if termine else None
    rest, woche = [], []
    for t in termine:
        if t.beginn >= mo + timedelta(days=7):
            continue
        if t.beginn >= mo:
            woche.append(t)
        elif ab is not None and t.ende > ab:
            rest.append(t)
    vorschlaege = [t for t in naechste.vorschlaege if not t.erledigt and t.ebene == 0]
    return WochenFakten(note, erledigt, offen, naechste, naechste_offen, woche, konflikte(woche),
                        rest, hinweise, vorschlaege)


_QUELLE_RE = re.compile(r"\s*\(Kalender:[^)]*\)")


def vorschlag_zeile(t: Task) -> str:
    """Vorschlag ohne Quellenangabe und 📅, mit Fälligkeit als Text."""
    s = _QUELLE_RE.sub("", re.sub(r"\s*📅\s*\d{4}-\d{2}-\d{2}", "", t.text)).strip()
    return f"{s} (fällig {TAGE[t.faellig.weekday()]} {t.faellig:%d.%m.})" if t.faellig else s


def wochen_text(f: WochenFakten) -> str:
    n = f.note
    z = [f"Rückblick KW {n.kw:02d} ({n.montag:%d.%m.}–{n.montag + timedelta(days=6):%d.%m.%Y})"]
    if n.status is NoteStatus.OK:
        z.append(f"Erledigt: {len(f.erledigt)}, offen: {len(f.offen)}")
        z += [f"- erledigt: {t.text}" for t in f.erledigt]
        z += [f"- offen: {task_zeile(t)}" for t in f.offen]
    else:
        z.append(KEINE_AUFGABEN)
    if f.termine_rest:
        z.append("Noch vor Wochenbeginn:")
        for t in f.termine_rest:
            z.append(f"- {TAGE[t.beginn.weekday()]} {t.beginn:%d.%m.}: {termin_zeile(t, t.beginn.date())}")
    nx = f.naechste
    z.append(f"Ausblick KW {nx.kw:02d} ({nx.montag:%d.%m.}–{nx.montag + timedelta(days=6):%d.%m.%Y})")
    if f.termine_naechste:
        z.append("Termine:")
        for t in f.termine_naechste:
            z.append(f"- {TAGE[t.beginn.weekday()]} {t.beginn:%d.%m.}: {termin_zeile(t, t.beginn.date())}")
    else:
        z.append("Termine: keine")
    if f.konflikte_naechste:
        z.append("Überschneidungen:")
        z += [f"- {a.titel} und {b.titel} am {a.beginn:%d.%m.}" for a, b in f.konflikte_naechste]
    if f.naechste_offen:
        z.append("Schon geplante Aufgaben:")
        z += [f"- {task_zeile(t)}" for t in f.naechste_offen]
    elif nx.status is NoteStatus.FEHLT:
        z.append(f"Für KW {nx.kw:02d} gibt es noch keine Weekly Note.")
    if f.vorschlaege:
        z.append("Vorschläge aus dem Kalender (stehen in der Weekly Note unter „Vorschläge“, "
                 "noch nicht übernommen):")
        z += [f"- {vorschlag_zeile(t)}" for t in f.vorschlaege]
    if f.hinweise:
        z.append("Hinweise:")
        z += [f"- {h}" for h in f.hinweise]
    return "\n".join(z)


def erlaubte_uhrzeiten(fakten_text: str) -> set[str]:
    """Jede Uhrzeit, die im Faktenblock vorkommt (Grundlage für die Halluzinationsprüfung)."""
    return {f"{int(h):02d}:{m}" for h, m in re.findall(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)", fakten_text)}
