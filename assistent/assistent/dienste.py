"""Gemeinsame Abläufe für CLI und HTTP-API (Phase 5).

Hier liegt, was ``assistent tag`` und ``GET /zusammenfassung/tag`` gleich tun
müssen – die CLI druckt das Ergebnis, die API serialisiert es. Keine Ausgabe,
kein ``sys.exit``: Fehler kommen als Exceptions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

import logging

from . import kalender, llm, planung, vault, vorschlaege
from .config import Config

log = logging.getLogger("assistent.dienste")

MONATE_KURZ = ["Jan", "Feb", "Mär", "Apr", "Mai", "Jun", "Jul", "Aug", "Sep", "Okt", "Nov", "Dez"]


class DatumUnklar(ValueError):
    """``wann`` konnte nicht in ein Datum aufgelöst werden."""


class NoteFehlt(vault.VaultFehler):
    """Ziel-Note fehlt oder ist veraltet und soll nicht angelegt werden."""

    def __init__(self, note: vault.WeeklyNote):
        super().__init__(f"Weekly Note KW {note.kw:02d} {note.status.value} ({note.pfad.name})")
        self.note = note


def heute(cfg: Config) -> date:
    return datetime.now(cfg.zeitzone).date()


def note(cfg: Config, d: date) -> vault.WeeklyNote:
    j, w = planung.kw_von(d)
    return vault.lade_weekly(cfg.vault, cfg.weekly_muster, j, w)


# ---------------------------------------------------------------- Berichte


def fakten_tag(cfg: Config, d: date) -> tuple[str, list[str]]:
    n = note(cfg, d)
    kal = kalender.lade_termine(cfg.ical_quellen, d - timedelta(days=1), d + timedelta(days=1), cfg.zeitzone)
    jetzt = datetime.now(cfg.zeitzone)
    f = planung.tages_fakten(
        d, n, kal.termine, kal.fehler, cfg.zeitzone, cfg.tag_beginn, cfg.tag_ende,
        jetzt=jetzt if jetzt.date() == d else None,
    )
    return planung.tages_text(f), [t.titel for t in f.termine]


def fakten_woche(cfg: Config, d: date) -> tuple[str, list[str]]:
    n = note(cfg, d)
    naechste = note(cfg, d + timedelta(days=7))
    mo = naechste.montag
    kal = kalender.lade_termine(cfg.ical_quellen, d, mo + timedelta(days=7), cfg.zeitzone)
    jetzt = datetime.now(cfg.zeitzone)
    # Bericht für einen anderen Tag (Test): so tun, als liefe er dort um 19:00
    ab = jetzt if jetzt.date() == d else datetime.combine(d, datetime.min.time().replace(hour=19), cfg.zeitzone)
    f = planung.wochen_fakten(n, naechste, kal.termine, kal.fehler, ab=ab)
    return planung.wochen_text(f), [t.titel for t in f.termine_rest + f.termine_naechste]


def fakten(cfg: Config, art: str, d: date) -> tuple[str, list[str]]:
    return fakten_tag(cfg, d) if art == "tag" else fakten_woche(cfg, d)


def titel(art: str, d: date) -> str:
    """Überschrift für die Push-Nachricht."""
    if art == "tag":
        return f"Dein {vault.TAGE[d.weekday()]}, {d.day}. {MONATE_KURZ[d.month - 1]}"
    _, kw = planung.kw_von(d + timedelta(days=7))  # dieselbe "nächste Woche" wie fakten_woche
    return f"Wochenbericht – Ausblick KW {kw:02d}"


@dataclass
class Bericht:
    art: str
    datum: date
    titel: str
    text: str
    fakten: str
    llm: llm.LLMErgebnis | None  # None bei ohne_llm
    # Gründliches Alias gescheitert → mit cfg.modell neu formuliert: Grund des ersten Laufs
    rueckfall: str | None = None


def llm_parameter(cfg: Config, art: str, modell: str | None = None) -> tuple[str, int, float]:
    """(Alias, max_tokens, Timeout) für eine Berichtsart. Nur der Wochenbericht ist
    ein Batch ohne Latenzanspruch und darf ein gründlicheres Alias nutzen."""
    if art == "woche":
        return modell or cfg.modell_woche or cfg.modell, cfg.max_tokens_woche, cfg.llm_timeout_woche
    return modell or cfg.modell, 700, 180.0


def bericht(cfg: Config, art: str, d: date, modell: str | None = None, ohne_llm: bool = False) -> Bericht:
    f, termine = fakten(cfg, art, d)
    if ohne_llm:
        return Bericht(art, d, titel(art, d), f, f, None)
    alias, max_tokens, timeout = llm_parameter(cfg, art, modell)
    gruendlich = modell is None and alias != cfg.modell
    # Gründlich: nur EIN Versuch (ein Lauf dauert ~60 s); scheitert er, formuliert das
    # schnelle Modell. Summe bleibt unter dem HA-Timeout von 300 s.
    e = llm.formuliere(llm.client(cfg.llm_base_url, cfg.llm_key, timeout), alias, art, f, termine,
                       max_versuche=1 if gruendlich else 2, max_tokens=max_tokens)
    rueckfall = None
    if e.fallback and gruendlich:
        # Lieber Fließtext vom schnellen Modell als nur der Faktenblock. Ein Versuch,
        # damit HA (Timeout 300 s) nicht ins Leere wartet.
        rueckfall = f"{alias}: {e.fallback}"
        log.warning("Bericht %s: %s – neu mit %s", art, rueckfall, cfg.modell)
        e = llm.formuliere(llm.client(cfg.llm_base_url, cfg.llm_key), cfg.modell, art, f, termine,
                           max_versuche=1)
    return Bericht(art, d, titel(art, d), e.text, f, e, rueckfall)


# ---------------------------------------------------------------- Aufgaben (schreibend)


@dataclass
class Eingefuegt:
    datei: str
    abschnitt: str  # "ToDo" oder Wochentag
    zeile: str
    faellig: date | None
    angelegt: bool  # Note wurde dafür neu angelegt
    dry_run: bool


def aufgabe_hinzufuegen(
    cfg: Config, text: str, wann: str | None, d: date,
    anlegen: bool = False, dry_run: bool = False,
) -> Eingefuegt:
    faellig = None
    if wann:
        faellig = planung.loese_datum(wann, d)
        if faellig is None:
            raise DatumUnklar(f"Datum nicht verstanden: {wann!r}")

    def existiert(j: int, w: int) -> bool:
        return vault.lade_weekly(cfg.vault, cfg.weekly_muster, j, w).status is vault.NoteStatus.OK

    ziel = planung.ziel_fuer(faellig, d, existiert)
    n = vault.lade_weekly(cfg.vault, cfg.weekly_muster, ziel.jahr, ziel.kw)
    wo = "ToDo" if ziel.tag is None else vault.TAGE[ziel.tag]
    if dry_run:
        zeile = f"- [ ] {' '.join(text.split())}" + (f" 📅 {ziel.faellig}" if ziel.faellig else "")
        return Eingefuegt(n.pfad.name, wo, zeile, faellig, n.status is not vault.NoteStatus.OK, True)
    angelegt = False
    if n.status is not vault.NoteStatus.OK:
        if not anlegen:
            raise NoteFehlt(n)
        n = vault.lege_weekly_an(cfg.vault, cfg.weekly_muster, ziel.jahr, ziel.kw)
        angelegt = True
    zeile = vault.haenge_task_an(n.pfad, text, ziel.tag, ziel.faellig)
    return Eingefuegt(n.pfad.name, wo, zeile, faellig, angelegt, False)


def aufgabe_erledigt(cfg: Config, text: str, d: date, dry_run: bool = False) -> vault.Task:
    """Wirft ``vault.Mehrdeutig`` / ``vault.KeinTreffer`` / ``NoteFehlt``."""
    n = note(cfg, d)
    if n.status is not vault.NoteStatus.OK:
        raise NoteFehlt(n)
    if dry_run:
        return vault.finde_offene(n.tasks, text)
    return vault.hake_ab(n.pfad, text)


# ---------------------------------------------------------------- Woche vorbereiten (schreibend)


def naechster_montag(d: date) -> date:
    """Der Montag nach ``d`` (Sonntag → morgen, Montag → in einer Woche)."""
    return d + timedelta(days=7 - d.weekday())


@dataclass
class Vorbereitung:
    jahr: int
    kw: int
    datei: str
    angelegt: bool  # Note wurde neu angelegt (bzw. würde, bei dry_run)
    vorschlaege: list[vorschlaege.Vorschlag]
    uebersprungen: list[str]
    hinweise: list[str]  # Kalenderfehler, LLM-Ausfall
    dry_run: bool
    llm: llm.VorschlagAntwort | None

    def meldung(self) -> str:
        teile = [f"KW {self.kw:02d}: Wochenplanung {'angelegt' if self.angelegt else 'vorhanden'}"]
        n = len(self.vorschlaege)
        teile.append(f"{n} Vorschlag aus dem Kalender eingetragen" if n == 1
                     else f"{n} Vorschläge aus dem Kalender eingetragen" if n
                     else "keine neuen Vorschläge aus dem Kalender")
        s = ", ".join(teile) + "."
        if self.hinweise:
            s += " " + " ".join(self.hinweise)
        return s


def _kandidaten_text(kandidaten: list[vorschlaege.Kandidat]) -> str:
    return "\n".join(
        f"{k.nr}. {k.termin.titel} (Kategorie: {k.kategorie.beschreibung}) am "
        f"{vault.TAGE[k.datum.weekday()]}" for k in kandidaten
    )


def woche_vorbereiten(cfg: Config, d: date, dry_run: bool = False, ohne_llm: bool = False) -> Vorbereitung:
    """Weekly Note der nächsten Woche sicherstellen und Kalender-Vorschläge eintragen.

    Idempotent: ein zweiter Lauf legt nichts doppelt an. Schreibt nur (a) eine neue
    Note per ``open(…, "x")`` und (b) Zeilen in den Abschnitt "Vorschläge (Assistent)".
    Wirft ``vault.VeralteteNote``, wenn unter dem Namen eine Note aus dem Vorjahr liegt.
    """
    montag = naechster_montag(d)
    j, w = planung.kw_von(montag)
    n = vault.lade_weekly(cfg.vault, cfg.weekly_muster, j, w)
    if n.status is vault.NoteStatus.VERALTET:
        vault.lege_weekly_an(cfg.vault, cfg.weekly_muster, j, w)  # wirft VeralteteNote
    angelegt = n.status is vault.NoteStatus.FEHLT

    kal = kalender.lade_termine(cfg.ical_quellen, montag - timedelta(days=14),
                                montag + timedelta(days=21), cfg.zeitzone)
    hinweise = list(kal.fehler)
    # Bestehend = alles, was in der Ziel-Note schon steht (auch Vorschläge), plus die
    # offenen Aufgaben der laufenden Woche – die werden oft mitgenommen.
    aktuell = note(cfg, d)
    bestehend = [t.text for t in n.tasks + n.vorschlaege]
    bestehend += [t.text for t in aktuell.offen() if t.ebene == 0]
    zustand = vorschlaege.Zustand.lade(cfg.zustand)
    kandidaten, weg = vorschlaege.finde_kandidaten(
        kal.termine, montag, d, bestehend, zustand.schluessel()
    )

    antwort = None
    erg = None
    if kandidaten and not ohne_llm and cfg.vorschlaege_llm:
        offen_bestehend = [t.text for t in n.tasks if not t.erledigt]
        offen_bestehend += [t.text for t in aktuell.offen() if t.ebene == 0]
        erg = llm.frage_vorschlaege(llm.client(cfg.llm_base_url, cfg.llm_key), cfg.modell,
                                    _kandidaten_text(kandidaten), offen_bestehend)
        antwort = vorschlaege.lies_antwort(erg.roh) if erg.roh is not None else None
        if antwort is None:
            log.warning("Vorschläge: keine brauchbare LLM-Antwort (%s) – Vorlagentexte",
                        erg.fehler or (erg.roh or "")[:200])
            hinweise.append("Texte aus Vorlagen (LLM nicht verfügbar).")
    liste, weg2 = vorschlaege.entscheide(kandidaten, antwort, bestehend)

    if not dry_run:
        if angelegt:
            n = vault.lege_weekly_an(cfg.vault, cfg.weekly_muster, j, w)
        if liste:
            vault.haenge_vorschlaege_an(n.pfad, [v.zeile() for v in liste])
            zustand.merke(liste, datetime.now(cfg.zeitzone))
            try:
                zustand.speichere(d)
            except OSError as e:  # Vorschläge stehen schon in der Note – nicht scheitern
                log.warning("Zustand nicht gespeichert: %s", e)
                hinweise.append("Vorschlags-Zustand nicht gespeichert.")
    return Vorbereitung(j, w, n.pfad.name, angelegt, liste, weg + weg2, hinweise, dry_run, erg)


def abschnitt_name(t: vault.Task) -> str:
    if t.abschnitt == vault.TODO:
        return "ToDo"
    return vault.TAGE[t.abschnitt] if isinstance(t.abschnitt, int) else "?"
