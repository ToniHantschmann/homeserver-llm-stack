"""Vorschläge aus dem Kalender: Auswahl, LLM-Prüfung, Schreiben in die Note, Zustand."""

import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from conftest import KW35, MUSTER, lege_note
from fastapi.testclient import TestClient

from assistent import api, dienste, llm, planung, vault, vorschlaege
from assistent.config import Config
from assistent.kalender import Termin

TZ = ZoneInfo("Europe/Berlin")
SO = date(2026, 11, 15)  # Sonntag KW 46 – Lauf vor der neuen Woche
MO = date(2026, 11, 16)  # Montag KW 47


def T(titel, tag, h=10, ganztags=False):
    b = datetime(tag.year, tag.month, tag.day, 0 if ganztags else h, tzinfo=TZ)
    return Termin(titel, b, b + (timedelta(days=1) if ganztags else timedelta(hours=1)), ganztags)


def termine():
    ts = [
        T("Deadline DBS Themenwahl", MO, 23),
        T("Zahnarzt", MO + timedelta(days=3)),
        T("Geburtstagsfeier", MO + timedelta(days=5), 19),
        T("Ligatraining", MO + timedelta(days=2)),
        T("Klausur GenAI", MO + timedelta(days=9)),  # Mittwoch der Woche danach
        T("Gaumeisterschaft", MO + timedelta(days=14), ganztags=True),  # zu weit weg
    ]
    # Wöchentliche Abgabe: hätte eine Kategorie, ist aber Routine
    ts += [T("Abgabe Übungsblatt", MO + timedelta(days=1 + 7 * i), 18) for i in range(-2, 3)]
    return ts


# ---------------------------------------------------------------- Auswahl


@pytest.mark.parametrize("titel,kat", [
    ("Klausur GenAI", "pruefung"),
    ("Deadline DBS Themenwahl", "frist"),
    ("Geburtstagsfeier", "geschenk"),
    ("Einweihungsfeier Johanna", "geschenk"),
    ("Gaumeisterschaft", "wettkampf"),
    ("Liga Tag 2", "wettkampf"),
    ("Ligatraining", None),
    ("Basistraining", None),
    ("Umzug Lena", None),
    ("Flug nach Rom", "reise"),
])
def test_kategorie(titel, kat):
    k = vorschlaege.kategorie_fuer(titel)
    assert (k.name if k else None) == kat


def test_kandidaten_fenster_routine_faelligkeit():
    ks, weg = vorschlaege.finde_kandidaten(termine(), MO, SO, [])
    assert [(k.termin.titel, k.faellig) for k in ks] == [
        ("Deadline DBS Themenwahl", MO),  # Frist: fällig am Tag selbst
        ("Geburtstagsfeier", date(2026, 11, 20)),  # Tag davor
        ("Klausur GenAI", date(2026, 11, 24)),  # Woche danach, aber Vorlauf 10 Tage
    ]
    assert [k.nr for k in ks] == [1, 2, 3]
    assert any("Abgabe Übungsblatt: Routine" in w for w in weg)


def test_faelligkeit_nie_vor_heute():
    ks, _ = vorschlaege.finde_kandidaten([T("Klausur Stochastik", MO, 9)], MO, SO, [])
    assert ks[0].faellig == SO


def test_kandidaten_zustand_und_bestehend():
    ks, _ = vorschlaege.finde_kandidaten(termine(), MO, SO, [])
    schon = {ks[0].schluessel}
    ks2, weg = vorschlaege.finde_kandidaten(
        termine(), MO, SO, ["Für die Klausur GenAI lernen (Kalender: Mi 25.11.)"], schon
    )
    assert [k.termin.titel for k in ks2] == ["Geburtstagsfeier"]
    assert any("schon einmal vorgeschlagen" in w for w in weg)
    assert any("steht schon in der Note" in w for w in weg)


def test_hoechstens_max_vorschlaege():
    viele = [T(f"Abgabe Blatt {i}", MO + timedelta(days=i % 7)) for i in range(12)]
    ks, weg = vorschlaege.finde_kandidaten(viele, MO, SO, [])
    assert len(ks) == vorschlaege.MAX_VORSCHLAEGE
    assert sum("mehr als" in w for w in weg) == 12 - vorschlaege.MAX_VORSCHLAEGE


# ---------------------------------------------------------------- LLM-Antwort


@pytest.mark.parametrize("text,titel,ok", [
    ("Für die GenAI-Klausur lernen", "Klausur GenAI", True),
    ("Geschenk für Johanna besorgen", "Einweihungsfeier Johanna", True),
    ("Geschenk für die Geburtstagsfeier besorgen", "Geburtstagsfeier", True),
    ("Geschenk besorgen", "Geburtstagsfeier", False),  # kein Bezug zum Termin
    ("Bis 14:00 Uhr lernen für GenAI", "Klausur GenAI", False),  # Uhrzeit
    ("GenAI lernen bis 24.11.", "Klausur GenAI", False),  # Datum
    ("**GenAI** lernen", "Klausur GenAI", False),  # Markdown
    ("GenAI " + "sehr " * 30 + "lernen", "Klausur GenAI", False),  # zu lang
    (None, "Klausur GenAI", False),
    (42, "Klausur GenAI", False),
])
def test_pruefe_text(text, titel, ok):
    assert (vorschlaege.pruefe_text(text, titel) is not None) is ok


def test_lies_antwort():
    assert vorschlaege.lies_antwort('```json\n{"1": "A", "2": null}\n```') == {"1": "A", "2": None}
    assert vorschlaege.lies_antwort("kein json") is None
    assert vorschlaege.lies_antwort('{"1": ') is None


def test_entscheide():
    ks, _ = vorschlaege.finde_kandidaten(termine(), MO, SO, [])
    antwort = {"1": "DBS-Thema wählen und einreichen", "2": None, "3": "Uhrzeit 9:00 lernen"}
    vs, weg = vorschlaege.entscheide(ks, antwort, [])
    assert [(v.kandidat.termin.titel, v.text, v.quelle) for v in vs] == [
        ("Deadline DBS Themenwahl", "DBS-Thema wählen und einreichen", "llm"),
        ("Klausur GenAI", "Für Klausur GenAI lernen", "vorlage"),  # ungültig → Vorlage
    ]
    assert weg == ["Geburtstagsfeier: laut LLM schon abgedeckt"]


def test_entscheide_ohne_llm_alles_vorlage():
    ks, _ = vorschlaege.finde_kandidaten(termine(), MO, SO, [])
    vs, weg = vorschlaege.entscheide(ks, None, [])
    assert [v.quelle for v in vs] == ["vorlage"] * 3 and weg == []
    assert vs[1].zeile() == ("- [ ] Geschenk für Geburtstagsfeier besorgen "
                             "(Kalender: Sa 21.11. Geburtstagsfeier) 📅 2026-11-20")


# ---------------------------------------------------------------- Vault


def test_vorschlaege_sind_keine_aufgaben(vault_dir):
    p = lege_note(vault_dir, 35, KW35, datetime(2026, 8, 27, 9))
    vor = p.read_bytes()
    vault.haenge_vorschlaege_an(p, ["- [ ] Blumen besorgen 📅 2026-08-20", "- [ ] Jonas antworten"])
    nach = p.read_bytes()
    assert nach.startswith(vor)  # nur angehängt
    assert nach.decode().endswith(
        "- [ ] \n\n# Vorschläge (Assistent)\n" + vault.VORSCHLAG_HINWEIS
        + "\n- [ ] Blumen besorgen 📅 2026-08-20\n- [ ] Jonas antworten\n"
    )
    n = vault.lade_weekly(vault_dir, MUSTER, 2026, 35)
    assert [t.text for t in n.vorschlaege] == ["Blumen besorgen 📅 2026-08-20", "Jonas antworten"]
    assert all(t.abschnitt != vault.VORSCHLAG for t in n.tasks)
    assert len(n.tasks) == len(vault.parse_tasks(KW35))
    # überfällig (📅 in der Vergangenheit) zählt nur für echte Aufgaben
    assert "Blumen besorgen 📅 2026-08-20" not in [t.text for t in planung.ueberfaellig(n, date(2026, 8, 27))]
    # "hake Jonas antworten ab" trifft die echte Aufgabe, nie den Vorschlag
    t = vault.hake_ab(p, "Jonas antworten")
    assert t.abschnitt != vault.VORSCHLAG


def test_vorschlaege_zweiter_lauf_im_abschnitt(vault_dir):
    p = lege_note(vault_dir, 35, KW35 + "\n\n# Notizen\nfrei\n", datetime(2026, 8, 27, 9))
    vault.haenge_vorschlaege_an(p, ["- [ ] A"])
    # Nutzer schreibt danach weiter unten etwas – der zweite Lauf bleibt im Abschnitt
    with open(p, "a", encoding="utf-8") as f:
        f.write("\n# Danach\ntext\n")
    vault.haenge_vorschlaege_an(p, ["- [ ] B"])
    text = p.read_text()
    assert "- [ ] A\n- [ ] B\n" in text
    assert text.index("- [ ] B") < text.index("# Danach")


def test_vorschlaege_alle_geloescht_neue_hinter_hinweis(vault_dir):
    p = lege_note(vault_dir, 35, f"# ToDo\n- [ ] x\n\n{vault.VORSCHLAG_UEBERSCHRIFT}\n"
                  f"{vault.VORSCHLAG_HINWEIS}\n", datetime(2026, 8, 27, 9))
    vault.haenge_vorschlaege_an(p, ["- [ ] C"])
    assert p.read_text().endswith(f"{vault.VORSCHLAG_HINWEIS}\n- [ ] C\n")


def test_vorschlaege_crlf(vault_dir):
    p = lege_note(vault_dir, 35, "# ToDo\r\n- [ ] x\r\n", datetime(2026, 8, 27, 9))
    vault.haenge_vorschlaege_an(p, ["- [ ] C"])
    assert p.read_bytes().endswith(b"- [ ] x\r\n\r\n# Vorschl\xc3\xa4ge (Assistent)\r\n"
                                   + vault.VORSCHLAG_HINWEIS.encode() + b"\r\n- [ ] C\r\n")


# ---------------------------------------------------------------- Ablauf (dienste)

ICS = """BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:test\r\n{events}END:VCALENDAR\r\n"""
EV = ("BEGIN:VEVENT\r\nUID:{uid}\r\nDTSTART;TZID=Europe/Berlin:{start}\r\n"
      "DTEND;TZID=Europe/Berlin:{ende}\r\nSUMMARY:{titel}\r\n{extra}END:VEVENT\r\n")


@pytest.fixture
def cfg(vault_dir, tmp_path):
    events = "".join([
        EV.format(uid="a", start="20261116T230000", ende="20261116T233000",
                  titel="Deadline DBS Themenwahl", extra=""),
        EV.format(uid="b", start="20261121T190000", ende="20261121T230000",
                  titel="Geburtstagsfeier", extra=""),
        EV.format(uid="c", start="20261125T100000", ende="20261125T120000",
                  titel="Klausur GenAI", extra=""),
        EV.format(uid="d", start="20261103T180000", ende="20261103T200000",
                  titel="Basistraining", extra="RRULE:FREQ=WEEKLY;BYDAY=TU\r\n"),
    ])
    ics = tmp_path / "k.ics"
    ics.write_text(ICS.format(events=events))
    z = tmp_path / "zustand"
    z.mkdir()
    # Laufende KW 46 mit einer offenen Aufgabe, die eine Vorbereitung schon abdeckt
    lege_note(vault_dir, 46, "# ToDo\n- [ ] Geschenk für Geburtstagsfeier besorgen\n",
              datetime(2026, 11, 13, 9))
    return Config(vault=vault_dir, weekly_muster=MUSTER, ical_quellen=[str(ics)],
                  zustand=z, api_token="t" * 32)


@pytest.fixture
def llm_antwort(monkeypatch):
    aufrufe = []

    def fake(cli, modell, termine, bestehend):
        aufrufe.append((termine, bestehend))
        return llm.VorschlagAntwort(json.dumps({"1": "DBS-Thema festlegen und einreichen",
                                                "2": "Für die GenAI-Klausur lernen"}), modell)

    monkeypatch.setattr(llm, "frage_vorschlaege", fake)
    return aufrufe


def test_vorbereiten_legt_an_und_traegt_ein(cfg, llm_antwort):
    v = dienste.woche_vorbereiten(cfg, SO)
    assert (v.kw, v.angelegt) == (47, True)
    assert [x.text for x in v.vorschlaege] == ["DBS-Thema festlegen und einreichen",
                                               "Für die GenAI-Klausur lernen"]
    # Geburtstagsfeier: steht als offene Aufgabe in KW 46 → gar nicht erst Kandidat
    assert any("Geburtstagsfeier: steht schon" in w for w in v.uebersprungen)
    assert "Geburtstagsfeier" not in llm_antwort[0][0]
    n = vault.lade_weekly(cfg.vault, MUSTER, 2026, 47)
    assert n.tasks == [] and len(n.vorschlaege) == 2
    assert n.vorschlaege[0].faellig == MO
    assert v.meldung() == "KW 47: Wochenplanung angelegt, 2 Vorschläge aus dem Kalender eingetragen."
    assert set(json.loads((cfg.zustand / "vorschlaege.json").read_text())) == {
        "deadline dbs themenwahl|2026-11-16", "klausur genai|2026-11-25"}


def test_vorbereiten_idempotent_und_geloeschtes_kommt_nicht_wieder(cfg, llm_antwort):
    dienste.woche_vorbereiten(cfg, SO)
    p = vault.lade_weekly(cfg.vault, MUSTER, 2026, 47).pfad
    v2 = dienste.woche_vorbereiten(cfg, SO)
    assert (v2.angelegt, v2.vorschlaege) == (False, [])
    assert v2.meldung() == "KW 47: Wochenplanung vorhanden, keine neuen Vorschläge aus dem Kalender."
    # Nutzer löscht einen Vorschlag (simuliert, der Assistent selbst kürzt nie)
    p.write_text("\n".join(z for z in p.read_text().splitlines() if "GenAI" not in z))
    v3 = dienste.woche_vorbereiten(cfg, SO)
    assert v3.vorschlaege == [] and "GenAI" not in p.read_text()
    assert len(llm_antwort) == 1  # ohne Kandidaten kein LLM-Aufruf


def test_vorbereiten_ohne_zustand_dedupliziert_ueber_die_note(cfg, llm_antwort):
    cfg.zustand = None
    dienste.woche_vorbereiten(cfg, SO)
    v2 = dienste.woche_vorbereiten(cfg, SO)
    assert v2.vorschlaege == []


def test_vorbereiten_dry_run_schreibt_nichts(cfg, llm_antwort):
    v = dienste.woche_vorbereiten(cfg, SO, dry_run=True)
    assert v.angelegt and len(v.vorschlaege) == 2
    assert vault.lade_weekly(cfg.vault, MUSTER, 2026, 47).status is vault.NoteStatus.FEHLT
    assert not (cfg.zustand / "vorschlaege.json").exists()


def test_vorbereiten_llm_ausfall_vorlagen(cfg, monkeypatch):
    monkeypatch.setattr(llm, "frage_vorschlaege",
                        lambda *a: llm.VorschlagAntwort(None, "assistent", fehler="LLM-Fehler: Timeout"))
    v = dienste.woche_vorbereiten(cfg, SO)
    assert [x.quelle for x in v.vorschlaege] == ["vorlage", "vorlage"]
    assert "Texte aus Vorlagen" in v.meldung()


def test_vorbereiten_bestehende_note_wird_nur_ergaenzt(cfg, llm_antwort, vault_dir):
    p = lege_note(vault_dir, 47, KW35, datetime(2026, 11, 14, 9))
    vor = p.read_bytes()
    v = dienste.woche_vorbereiten(cfg, SO)
    assert not v.angelegt and len(v.vorschlaege) == 2
    assert p.read_bytes().startswith(vor)


def test_vorbereiten_veraltete_note(cfg, llm_antwort, vault_dir):
    lege_note(vault_dir, 47, KW35, datetime(2025, 11, 20, 9))
    with pytest.raises(vault.VeralteteNote):
        dienste.woche_vorbereiten(cfg, SO)


def test_wochenbericht_nennt_vorschlaege(cfg, llm_antwort):
    dienste.woche_vorbereiten(cfg, SO)
    fakten, _ = dienste.fakten_woche(cfg, SO)
    assert "Vorschläge aus dem Kalender" in fakten
    assert "- DBS-Thema festlegen und einreichen (fällig Montag 16.11.)" in fakten
    assert "Kalender:" not in fakten and "📅" not in fakten


def test_api_vorbereiten(cfg, llm_antwort, monkeypatch):
    monkeypatch.setattr(dienste, "heute", lambda c: SO)
    c = TestClient(api.erstelle_app(cfg))
    assert c.post("/woche/vorbereiten").status_code == 401
    r = c.post("/woche/vorbereiten", headers={"Authorization": "Bearer " + "t" * 32},
               json={"dry_run": True})
    assert r.status_code == 200
    j = r.json()
    assert j["kw"] == 47 and j["dry_run"] and len(j["vorschlaege"]) == 2
    assert j["vorschlaege"][0]["faellig"] == "2026-11-16"
    r = c.post("/woche/vorbereiten", headers={"Authorization": "Bearer " + "t" * 32})
    assert r.json()["angelegt"] is True
