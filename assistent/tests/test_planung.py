from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from conftest import DATEN, KW35, KW35_DONNERSTAG, MUSTER, lege_note

from assistent import kalender, planung, vault
from assistent.planung import loese_datum

TZ = ZoneInfo("Europe/Berlin")
FR = date(2026, 11, 13)  # Freitag, KW 46


# ---------------------------------------------------------------- Datum


@pytest.mark.parametrize("ausdruck,erwartet", [
    ("heute", FR),
    ("morgen", date(2026, 11, 14)),
    ("übermorgen", date(2026, 11, 15)),
    ("Freitag", FR),  # heute ist Freitag
    ("bis Freitag", FR),
    ("Montag", date(2026, 11, 16)),
    ("am Sonntag", date(2026, 11, 15)),
    ("nächsten Freitag", date(2026, 11, 20)),
    ("naechsten montag", date(2026, 11, 16)),
    ("Mittwoch nächste Woche", date(2026, 11, 18)),
    ("nächste Woche", date(2026, 11, 16)),
    ("Wochenende", date(2026, 11, 14)),
    ("in 3 Tagen", date(2026, 11, 16)),
    ("in drei Tagen", date(2026, 11, 16)),
    ("in einer Woche", date(2026, 11, 20)),
    ("in zwei Wochen", date(2026, 11, 27)),
    ("20.11.", date(2026, 11, 20)),
    ("1.2.", date(2027, 2, 1)),  # schon vorbei → nächstes Jahr
    ("3.1.2027", date(2027, 1, 3)),
    ("3.1.27", date(2027, 1, 3)),
    ("2026-12-24", date(2026, 12, 24)),
    ("irgendwann", None),
    ("31.02.", None),
    ("nächsten Urlaub", None),
])
def test_loese_datum(ausdruck, erwartet):
    assert loese_datum(ausdruck, FR) == erwartet


def test_loese_datum_ueber_jahreswechsel():
    silvester = date(2026, 12, 31)  # Donnerstag, KW 53
    assert loese_datum("Montag", silvester) == date(2027, 1, 4)
    assert loese_datum("nächsten Donnerstag", silvester) == date(2027, 1, 7)
    assert loese_datum("Wochenende", silvester) == date(2027, 1, 2)
    assert planung.kw_von(silvester) == (2026, 53)
    assert planung.kw_von(date(2027, 1, 3)) == (2026, 53)


# ---------------------------------------------------------------- Ziel


def test_ziel_ohne_datum_und_in_dieser_woche():
    assert planung.ziel_fuer(None, FR) == planung.Ziel(2026, 46, None, None)
    assert planung.ziel_fuer(date(2026, 11, 15), FR) == planung.Ziel(2026, 46, 6, None)


def test_ziel_naechste_woche_mit_und_ohne_note():
    d = date(2026, 11, 18)
    assert planung.ziel_fuer(d, FR) == planung.Ziel(2026, 46, None, d)
    assert planung.ziel_fuer(d, FR, lambda j, w: (j, w) == (2026, 47)) == planung.Ziel(2026, 47, 2, None)


def test_ziel_vergangenheit_geht_in_todo():
    assert planung.ziel_fuer(date(2026, 11, 1), FR) == planung.Ziel(2026, 46, None, None)


# ---------------------------------------------------------------- Kalender-Logik


def _kal(von, bis):
    return kalender.termine_aus(kalender.lade_quelle(str(DATEN / "kalender.ics")), von, bis, TZ)


def test_konflikt_zahnarzt_meeting():
    ts = _kal(FR, date(2026, 11, 14))
    k = planung.konflikte(ts)
    assert [(a.titel, b.titel) for a, b in k] == [("Zahnarzt", "Meeting")]


def test_freie_slots():
    ts = _kal(FR, date(2026, 11, 14))
    frei = planung.freie_slots(ts, FR, TZ, 8, 21)
    assert [(a.strftime("%H:%M"), b.strftime("%H:%M")) for a, b in frei] == [
        ("08:00", "09:00"), ("10:30", "21:00")]


def test_freie_slots_ab_jetzt_und_mindestdauer():
    ts = _kal(FR, date(2026, 11, 14))
    jetzt = datetime(2026, 11, 13, 8, 41, tzinfo=TZ)  # → 08:45, bis 09:00 nur 15 min
    frei = planung.freie_slots(ts, FR, TZ, 8, 21, jetzt=jetzt)
    assert [(a.strftime("%H:%M"), b.strftime("%H:%M")) for a, b in frei] == [("10:30", "21:00")]


def test_ganztags_blockiert_keine_zeit():
    ts = _kal(date(2026, 11, 14), date(2026, 11, 15))
    assert len(planung.freie_slots(ts, date(2026, 11, 14), TZ)) == 1


# ---------------------------------------------------------------- Tasks


@pytest.fixture
def kw35(vault_dir):
    lege_note(vault_dir, 35, KW35, datetime(2026, 8, 27))
    return vault.lade_weekly(vault_dir, MUSTER, 2026, 35)


def test_offene_tasks_heute_ohne_dubletten_und_erledigte(kw35):
    texte = [t.text for t in planung.offene_oben(kw35, 3)]
    assert texte == ["Sachen packen", "Bewerbung vorbereiten"]  # "Jonas antworten" ist erledigt


def test_wochen_todo_ohne_anderswo_erledigte(kw35):
    texte = [t.text for t in planung.offene_oben(kw35, vault.TODO)]
    assert "Jonas antworten" not in texte and "Socken kaufen" not in texte
    assert texte == ["1 Bewerbung abschicken", "Alex Fahrrad anschauen"]


def test_ueberfaellig(kw35):
    texte = [t.text for t in planung.ueberfaellig(kw35, KW35_DONNERSTAG)]
    # Dienstag offen → überfällig; "Blumen besorgen" ist auf Freitag verschoben;
    # "Jonas antworten" am Donnerstag erledigt
    assert texte == ["17:45 Uhr: Mama Flughafen"]


def test_ueberfaellig_mit_datum(vault_dir):
    lege_note(vault_dir, 46, "# ToDo\n- [ ] Steuer 📅 2026-11-10\n- [ ] Später 📅 2026-11-30\n",
              datetime(2026, 11, 9))
    n = vault.lade_weekly(vault_dir, MUSTER, 2026, 46)
    assert [t.text for t in planung.ueberfaellig(n, FR)] == ["Steuer 📅 2026-11-10"]


def test_tagesfakten_fehlende_note_und_kalenderfehler(vault_dir):
    n = vault.lade_weekly(vault_dir, MUSTER, 2026, 46)
    f = planung.tages_fakten(FR, n, _kal(FR, date(2026, 11, 14)), ["Kalender 1 nicht ladbar: X"], TZ)
    text = planung.tages_text(f)
    assert "09:00–10:00 Zahnarzt" in text
    assert "Überschneidungen:\n- Zahnarzt (09:00) und Meeting (09:30)" in text
    assert "Für KW 46 gibt es noch keine Weekly Note." in text
    assert "Kalender 1 nicht ladbar" in text
    assert "Aufgaben für heute: keine eingetragen" in text


def test_tagesfakten_mit_note(kw35):
    f = planung.tages_fakten(KW35_DONNERSTAG, kw35, [], [], TZ)
    text = planung.tages_text(f)
    assert "Termine heute: keine" in text
    assert "- Sachen packen [1/2 Unterpunkte erledigt; offen: Fliege]" in text
    assert "Heute schon erledigt: 1" in text
    assert "- 17:45 Uhr: Mama Flughafen (Dienstag)" in text
    assert "Offen auf der Wochenliste:\n- 1 Bewerbung abschicken" in text


def test_tagesfakten_nachtzug_ueber_mitternacht(vault_dir):
    n = vault.lade_weekly(vault_dir, MUSTER, 2026, 47)
    mo = date(2026, 11, 16)
    text = planung.tages_text(planung.tages_fakten(mo, n, _kal(mo, date(2026, 11, 17)), [], TZ))
    assert "bis 01:00 (seit 15.11.) Nachtzug" in text


def test_wochenfakten(kw35, vault_dir):
    naechste = vault.lade_weekly(vault_dir, MUSTER, 2026, 36)
    f = planung.wochen_fakten(kw35, naechste, [], [])
    assert "Jonas antworten" in [t.text for t in f.erledigt]
    assert "Sachen packen" in [t.text for t in f.offen]
    text = planung.wochen_text(f)
    assert text.startswith("Rückblick KW 35 (24.08.–30.08.2026)")
    assert "Ausblick KW 36 (31.08.–06.09.2026)" in text
    assert "Für KW 36 gibt es noch keine Weekly Note." in text
    assert "Keine Aufgaben erfasst" not in text


def test_wochenfakten_termine_ueber_jahreswechsel(vault_dir):
    n = vault.lade_weekly(vault_dir, MUSTER, 2026, 52)
    naechste = vault.lade_weekly(vault_dir, MUSTER, 2026, 53)
    f = planung.wochen_fakten(n, naechste, _kal(date(2026, 12, 28), date(2027, 1, 4)), [])
    text = planung.wochen_text(f)
    assert "- Dienstag 29.12.: 18:00–19:30 Bogentraining (Halle Nord)" in text
    assert "- Donnerstag 31.12.: 20:00–22:00 Stammtisch" in text


def test_erlaubte_uhrzeiten():
    assert planung.erlaubte_uhrzeiten("9:00–10:30 X, 17:45 Uhr") == {"09:00", "10:30", "17:45"}


# ---------------------------------------------------------------- Nachbesserungen aus dem Echtdatentest


def test_gleiche_aufgabe():
    assert planung.gleiche_aufgabe("fahrrad anschauen", "alex fahrrad anschauen")
    assert planung.gleiche_aufgabe("30 min genai", "30 min genai lernen")
    assert not planung.gleiche_aufgabe("laufen", "laufen mit jonas")  # nur ein Wort
    assert not planung.gleiche_aufgabe("bewerbung vorbereiten", "1 bewerbung abschicken")
    assert not planung.gleiche_aufgabe("mama anrufen", "mama anrufen wegen weihnachtsgeschenk")


def test_umformulierte_und_unterpunkt_erledigte_gelten_als_erledigt(vault_dir):
    lege_note(vault_dir, 35, "# ToDo\n- [ ] Alex Fahrrad anschauen\n- [ ] Packen\n\t- [x] Hemd\n"
              "## Donnerstag:\n- [x] Fahrrad anschauen\n", datetime(2026, 8, 27))
    n = vault.lade_weekly(vault_dir, MUSTER, 2026, 35)
    assert planung.offene_oben(n, vault.TODO) == []
    f = planung.wochen_fakten(n, vault.lade_weekly(vault_dir, MUSTER, 2026, 36), [], [])
    assert [t.text for t in f.erledigt] == ["Alex Fahrrad anschauen", "Packen"] and f.offen == []


def test_wochenbericht_sonntagabend_termin_getrennt(vault_dir):
    n = vault.lade_weekly(vault_dir, MUSTER, 2026, 46)
    naechste = vault.lade_weekly(vault_dir, MUSTER, 2026, 47)
    ab = datetime(2026, 11, 15, 19, 0, tzinfo=TZ)
    f = planung.wochen_fakten(n, naechste, _kal(date(2026, 11, 15), date(2026, 11, 23)), [], ab=ab)
    assert [t.titel for t in f.termine_rest] == ["Nachtzug"]
    assert [t.titel for t in f.termine_naechste] == ["Bogentraining (verschoben)"]
    text = planung.wochen_text(f)
    assert "Noch vor Wochenbeginn:\n- Sonntag 15.11.: ab 22:00 (bis 16.11. 01:00) Nachtzug" in text
