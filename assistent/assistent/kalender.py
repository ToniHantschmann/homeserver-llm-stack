"""Google Calendar über die private iCal-Adresse (oder eine lokale .ics-Datei).

Serientermine löst ``recurring-ical-events`` auf (RRULE, EXDATE, verschobene
Einzeltermine per RECURRENCE-ID). Alles wird nach ``zeitzone`` umgerechnet.
Eine Quelle, die nicht geladen werden kann, wird als Fehler gemeldet statt
stillschweigend weggelassen – sonst sähe ein leerer Tag aus wie ein freier Tag.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import icalendar
import recurring_ical_events


@dataclass(frozen=True)
class Termin:
    titel: str
    beginn: datetime  # zeitzonenbewusst; bei ganztägigen 00:00 Ortszeit
    ende: datetime
    ganztags: bool = False
    ort: str | None = None

    def betrifft(self, tag: date) -> bool:
        tz = self.beginn.tzinfo
        t0 = datetime.combine(tag, time(0), tz)
        t1 = t0 + timedelta(days=1)
        if self.ende <= self.beginn:  # Punkt-Termin ohne Dauer
            return t0 <= self.beginn < t1
        return self.beginn < t1 and self.ende > t0


@dataclass
class KalenderErgebnis:
    termine: list[Termin] = field(default_factory=list)
    fehler: list[str] = field(default_factory=list)


def lade_quelle(quelle: str, timeout: float = 20.0) -> icalendar.Calendar:
    if quelle.startswith(("http://", "https://", "webcal://")):
        url = "https://" + quelle.removeprefix("webcal://") if quelle.startswith("webcal://") else quelle
        r = httpx.get(url, timeout=timeout, follow_redirects=True)
        r.raise_for_status()
        roh = r.content
    else:
        roh = Path(quelle).read_bytes()
    return icalendar.Calendar.from_ical(roh)


def _als_datetime(wert: date | datetime, tz: ZoneInfo) -> datetime:
    if isinstance(wert, datetime):
        return wert.replace(tzinfo=tz) if wert.tzinfo is None else wert.astimezone(tz)
    return datetime.combine(wert, time(0), tz)


def termine_aus(cal: icalendar.Calendar, von: date, bis: date, tz: ZoneInfo) -> list[Termin]:
    """Termine, die den Zeitraum [von, bis) berühren."""
    start = datetime.combine(von, time(0), tz)
    ende = datetime.combine(bis, time(0), tz)
    ergebnis = []
    for ev in recurring_ical_events.of(cal).between(start, ende):
        if str(ev.get("STATUS", "")).upper() == "CANCELLED":
            continue
        dtstart = ev["DTSTART"].dt
        ganztags = not isinstance(dtstart, datetime)
        b = _als_datetime(dtstart, tz)
        if "DTEND" in ev:
            e = _als_datetime(ev["DTEND"].dt, tz)
        elif "DURATION" in ev:
            e = b + ev["DURATION"].dt
        else:
            e = b + timedelta(days=1) if ganztags else b
        ort = str(ev.get("LOCATION", "")).strip() or None
        titel = str(ev.get("SUMMARY", "")).strip() or "(ohne Titel)"
        ergebnis.append(Termin(titel, b, e, ganztags, ort))
    ergebnis.sort(key=lambda t: (t.beginn, not t.ganztags, t.titel))
    return ergebnis


def lade_termine(quellen: list[str], von: date, bis: date, tz: ZoneInfo) -> KalenderErgebnis:
    erg = KalenderErgebnis()
    if not quellen:
        erg.fehler.append("kein Kalender konfiguriert (ASSISTENT_ICAL fehlt)")
        return erg
    for i, q in enumerate(quellen, 1):
        try:
            erg.termine += termine_aus(lade_quelle(q), von, bis, tz)
        except Exception as e:  # Netz, HTTP, Parse – alles soll im Bericht auftauchen
            # Die URL nie ausgeben: die private iCal-Adresse ist ein Geheimnis.
            erg.fehler.append(f"Kalender {i} nicht ladbar: {type(e).__name__}")
    # Mehrere Kalender: derselbe Termin steht oft in zweien (Einladung aus einem
    # geteilten Kalender erscheint auch im eigenen). Doppelt hiesse doppelt im
    # Bericht und eine "Überschneidung" mit sich selbst.
    eindeutig: dict[tuple, Termin] = {}
    for t in erg.termine:
        schluessel = (" ".join(t.titel.lower().split()), t.beginn, t.ende, t.ganztags)
        if schluessel not in eindeutig or (t.ort and not eindeutig[schluessel].ort):
            eindeutig[schluessel] = t  # die Variante mit Ort behalten
    erg.termine = sorted(eindeutig.values(), key=lambda t: (t.beginn, not t.ganztags, t.titel))
    return erg
