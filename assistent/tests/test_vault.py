from datetime import date, datetime

import pytest
from conftest import KW35, MUSTER, lege_note, setze_mtime

from assistent import vault
from assistent.vault import TODO, NoteStatus


# ---------------------------------------------------------------- parsen


def test_parse_abschnitte_platzhalter_unterpunkte():
    tasks = vault.parse_tasks(KW35)
    texte = [t.text for t in tasks]
    assert "" not in texte  # leere Platzhalter fallen weg
    todo = [t for t in tasks if t.abschnitt == TODO]
    assert [t.text for t in todo][:2] == ["Jonas antworten", "Socken kaufen"]
    assert todo[1].erledigt
    packen = next(t for t in tasks if t.text == "Sachen packen")
    assert packen.abschnitt == 3 and packen.ebene == 0
    assert [u.text for u in packen.unterpunkte] == ["Anzug + Hose", "Fliege"]
    fliege = next(t for t in tasks if t.text == "Fliege")
    assert fliege.ebene == 1 and fliege.eltern == "Sachen packen" and not fliege.erledigt


def test_uhrzeit_und_faelligkeit():
    t = vault.parse_tasks("## Dienstag:\n- [ ] 17:45 Uhr: Mama Flughafen\n"
                          "- [ ] Abholung 9.05 Uhr\n- [ ] Steuer 📅 2026-11-20\n"
                          "- [ ] Paket bis 15.11. abholen\n- [ ] 30 Min. lernen")
    assert [x.uhrzeit for x in t] == ["17:45", "09:05", None, None, None]
    assert t[2].faellig == date(2026, 11, 20)


def test_grossbuchstaben_x_und_ueberschrift_ohne_doppelpunkt():
    t = vault.parse_tasks("## freitag\n- [X] fertig")
    assert t[0].erledigt and t[0].abschnitt == 4


# ---------------------------------------------------------------- finden


def test_finde_case_insensitiv(vault_dir):
    lege_note(vault_dir, 8, "## Montag:\n- [ ] a", datetime(2026, 2, 19),
              name="03 - notes/wochenplanung kw 08.md")
    n = vault.lade_weekly(vault_dir, MUSTER, 2026, 8)
    assert n.status is NoteStatus.OK and n.pfad.name == "wochenplanung kw 08.md"


def test_fehlende_note(vault_dir):
    n = vault.lade_weekly(vault_dir, MUSTER, 2026, 46)
    assert n.status is NoteStatus.FEHLT and n.tasks == []
    assert n.pfad.name == "Wochenplanung KW 46.md"


def test_note_aus_vorjahr_ist_veraltet(vault_dir):
    # KW 01/2027 beginnt am 04.01.2027; die Datei "KW 01" wurde im Juni 2026 zuletzt geändert
    lege_note(vault_dir, 1, KW35, datetime(2026, 6, 10, 11, 46))
    n = vault.lade_weekly(vault_dir, MUSTER, 2027, 1)
    assert n.status is NoteStatus.VERALTET and n.tasks == []
    # dieselbe Datei ist für KW 01/2026 (Montag 29.12.2025) gültig – später bearbeitet ist ok
    assert vault.lade_weekly(vault_dir, MUSTER, 2026, 1).status is NoteStatus.OK


def test_vorausgeplante_note_ist_nicht_veraltet(vault_dir):
    lege_note(vault_dir, 46, KW35, datetime(2026, 10, 20))  # 3 Wochen vorher angelegt
    assert vault.lade_weekly(vault_dir, MUSTER, 2026, 46).status is NoteStatus.OK


# ---------------------------------------------------------------- anlegen


def test_lege_weekly_an_und_vorlage_parsebar(vault_dir):
    n = vault.lege_weekly_an(vault_dir, MUSTER, 2026, 46)
    assert n.status is NoteStatus.OK and n.tasks == []
    text = n.pfad.read_text()
    assert text.startswith("# ToDo\n- [ ] \n---\n# Tage:\n## Montag:")
    assert text.count("- - - ") == 6
    # zweiter Aufruf ändert nichts
    vorher = n.pfad.stat().st_mtime_ns
    vault.lege_weekly_an(vault_dir, MUSTER, 2026, 46)
    assert n.pfad.stat().st_mtime_ns == vorher


def test_veraltete_note_wird_nicht_ueberschrieben(vault_dir):
    p = lege_note(vault_dir, 1, KW35, datetime(2026, 6, 10))
    with pytest.raises(vault.VeralteteNote):
        vault.lege_weekly_an(vault_dir, MUSTER, 2027, 1)
    assert p.read_text() == KW35


# ---------------------------------------------------------------- anhängen


def _note(vault_dir, text=KW35):
    return lege_note(vault_dir, 35, text, datetime(2026, 8, 27))


def test_anhaengen_im_tagesabschnitt_vor_platzhalter(vault_dir):
    p = _note(vault_dir)
    vault.haenge_task_an(p, "Steuer abgeben", tag=3)
    zeilen = p.read_text().splitlines()
    i = zeilen.index("- [ ] Steuer abgeben")
    assert zeilen[i - 1] == "- [ ] Bewerbung vorbereiten"
    assert zeilen[i + 1] == "- [ ] " and zeilen[i + 2] == "- - - "


def test_anhaengen_nach_unterpunkten(vault_dir):
    text = "## Donnerstag:\n- [ ] Sachen packen\n\t- [ ] Fliege\n- [ ] \n## Freitag:\n- [ ] \n"
    p = _note(vault_dir, text)
    vault.haenge_task_an(p, "Neu", tag=3)
    assert p.read_text().splitlines()[3] == "- [ ] Neu"


def test_anhaengen_leerer_abschnitt_direkt_unter_ueberschrift(vault_dir):
    p = _note(vault_dir)
    vault.haenge_task_an(p, "Ausschlafen", tag=5)
    zeilen = p.read_text().splitlines()
    i = zeilen.index("## Samstag:")
    assert zeilen[i + 1] == "- [ ] Ausschlafen" and zeilen[i + 2] == "- [ ] "


def test_anhaengen_letzte_zeile_ohne_zeilenende(vault_dir):
    p = _note(vault_dir)
    vault.haenge_task_an(p, "Wäsche", tag=6)
    text = p.read_text()
    assert text.endswith("## Sonntag:\n- [ ] Wäsche\n- [ ] ")


def test_anhaengen_todo_mit_datum(vault_dir):
    p = _note(vault_dir)
    zeile = vault.haenge_task_an(p, "  Steuer   abgeben ", faellig=date(2026, 9, 4))
    assert zeile == "- [ ] Steuer abgeben 📅 2026-09-04"
    zeilen = p.read_text().splitlines()
    assert zeilen[5] == zeile and zeilen[6] == "---"


def test_anhaengen_aendert_nur_eine_zeile(vault_dir):
    p = _note(vault_dir)
    vault.haenge_task_an(p, "X", tag=1)
    neu = p.read_text().splitlines()
    neu.remove("- [ ] X")
    assert neu == KW35.splitlines()


def test_anhaengen_ohne_abschnitt_ans_ende(vault_dir):
    p = _note(vault_dir, "Nur Text")
    vault.haenge_task_an(p, "A", tag=2)
    assert p.read_text() == "Nur Text\n- [ ] A"


def test_anhaengen_crlf(vault_dir):
    p = vault_dir / "n.md"
    p.write_bytes(b"# ToDo\r\n- [ ] a\r\n---\r\n")
    vault.haenge_task_an(p, "b")
    assert p.read_bytes() == b"# ToDo\r\n- [ ] a\r\n- [ ] b\r\n---\r\n"


def test_leerer_text_wird_abgelehnt(vault_dir):
    with pytest.raises(vault.VaultFehler):
        vault.haenge_task_an(_note(vault_dir), "   ")


# ---------------------------------------------------------------- abhaken


def test_abhaken_aendert_genau_ein_byte(vault_dir):
    p = _note(vault_dir)
    vorher = p.read_bytes()
    t = vault.hake_ab(p, "bewerbung vorbereiten")
    nachher = p.read_bytes()
    assert t.text == "Bewerbung vorbereiten"
    assert len(vorher) == len(nachher)
    diff = [i for i, (a, b) in enumerate(zip(vorher, nachher)) if a != b]
    assert len(diff) == 1 and nachher[diff[0]:diff[0] + 1] == b"x"


def test_abhaken_fuzzy_hoerfehler(vault_dir):
    p = _note(vault_dir)
    assert vault.hake_ab(p, "Fahrad anschauen").text == "Alex Fahrrad anschauen"


def test_abhaken_gleicher_text_mehrfach_nimmt_frühesten(vault_dir):
    p = _note(vault_dir)
    t = vault.hake_ab(p, "Blumen besorgen")
    assert t.abschnitt == 2  # Mittwoch vor Freitag


def test_abhaken_mehrdeutig(vault_dir):
    p = _note(vault_dir)
    with pytest.raises(vault.Mehrdeutig) as e:
        vault.hake_ab(p, "Bewerbung")
    assert {k.text for k in e.value.kandidaten} == {"1 Bewerbung abschicken", "Bewerbung vorbereiten"}
    assert p.read_text() == KW35


def test_abhaken_kein_treffer(vault_dir):
    p = _note(vault_dir)
    with pytest.raises(vault.KeinTreffer):
        vault.hake_ab(p, "Rasenmähen")
    with pytest.raises(vault.KeinTreffer):
        vault.hake_ab(p, "Socken kaufen")  # schon erledigt
