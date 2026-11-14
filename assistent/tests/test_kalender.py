from datetime import date, datetime
from zoneinfo import ZoneInfo

from conftest import DATEN

from assistent import kalender

TZ = ZoneInfo("Europe/Berlin")
ICS = str(DATEN / "kalender.ics")


def _termine(von, bis):
    return kalender.termine_aus(kalender.lade_quelle(ICS), von, bis, TZ)


def _titel(ts):
    return [(t.titel, t.beginn.strftime("%d.%m. %H:%M")) for t in ts]


def test_mehrere_kalender_doppelte_termine_einmal(tmp_path):
    """Derselbe Termin in zwei Kalendern: einmal im Ergebnis, keine Selbst-Überschneidung."""
    from assistent import planung

    def ics(name, ort):
        p = tmp_path / name
        p.write_text(
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:test\r\n"
            "BEGIN:VEVENT\r\nUID:a@x\r\nDTSTART;TZID=Europe/Berlin:20261116T100000\r\n"
            "DTEND;TZID=Europe/Berlin:20261116T110000\r\nSUMMARY:Zahnarzt\r\n"
            + (f"LOCATION:{ort}\r\n" if ort else "") +
            "END:VEVENT\r\n"
            "BEGIN:VEVENT\r\nUID:b@x\r\nDTSTART;TZID=Europe/Berlin:20261116T140000\r\n"
            f"DTEND;TZID=Europe/Berlin:20261116T150000\r\nSUMMARY:Nur in {name}\r\nEND:VEVENT\r\n"
            "END:VCALENDAR\r\n", encoding="utf-8")
        return str(p)

    erg = kalender.lade_termine([ics("a.ics", None), ics("b.ics", "Praxis")],
                                date(2026, 11, 16), date(2026, 11, 17), TZ)
    assert not erg.fehler
    zahnarzt = [t for t in erg.termine if t.titel == "Zahnarzt"]
    assert len(zahnarzt) == 1 and zahnarzt[0].ort == "Praxis"
    assert len(erg.termine) == 3  # Zahnarzt + je ein eigener Termin
    assert not any(a.titel == b.titel == "Zahnarzt" for a, b in planung.konflikte(erg.termine))


def test_serientermin_mit_exdate_und_verschiebung():
    ts = [t for t in _termine(date(2026, 11, 1), date(2026, 11, 25)) if "Bogen" in t.titel]
    assert _titel(ts) == [
        ("Bogentraining", "03.11. 18:00"),
        # 10.11. per EXDATE ausgefallen
        ("Bogentraining (verschoben)", "17.11. 19:00"),
        ("Bogentraining", "24.11. 18:00"),
    ]
    assert ts[0].ort == "Halle Nord"


def test_utc_wird_ortszeit_auch_im_sommer():
    zahn = next(t for t in _termine(date(2026, 11, 13), date(2026, 11, 14)) if t.titel == "Zahnarzt")
    assert zahn.beginn == datetime(2026, 11, 13, 9, 0, tzinfo=TZ)
    sommer = _termine(date(2026, 7, 1), date(2026, 7, 2))
    assert _titel(sommer) == [("Sommerfest", "01.07. 12:00")]


def test_abgesagt_faellt_weg():
    assert "Abgesagt" not in [t.titel for t in _termine(date(2026, 11, 13), date(2026, 11, 14))]


def test_ganztags():
    g = next(t for t in _termine(date(2026, 11, 14), date(2026, 11, 15)) if t.titel == "Geburtstag Alex")
    assert g.ganztags and g.betrifft(date(2026, 11, 14)) and not g.betrifft(date(2026, 11, 15))


def test_ueber_wochenwechsel():
    # Sonntag 22:00 bis Montag 01:00 – gehört zu beiden Tagen / beiden Wochen
    mo = _termine(date(2026, 11, 16), date(2026, 11, 17))
    nacht = next(t for t in mo if t.titel == "Nachtzug")
    assert nacht.betrifft(date(2026, 11, 15)) and nacht.betrifft(date(2026, 11, 16))
    assert not nacht.betrifft(date(2026, 11, 17))


def test_serie_ueber_jahreswechsel_kw53():
    # KW 53/2026 = 28.12.2026 – 03.01.2027
    def st(von, bis):
        return [t for t in _termine(von, bis) if t.titel == "Stammtisch"]

    assert _titel(st(date(2026, 12, 28), date(2027, 1, 4))) == [("Stammtisch", "31.12. 20:00")]
    assert _titel(st(date(2027, 1, 4), date(2027, 1, 11))) == [("Stammtisch", "07.01. 20:00")]
    assert st(date(2027, 1, 11), date(2027, 2, 1)) == []  # COUNT=4 ist erschöpft


def test_fehlende_quelle_wird_gemeldet_ohne_url():
    erg = kalender.lade_termine(["/gibt/es/nicht.ics", ICS], date(2026, 11, 13), date(2026, 11, 14), TZ)
    assert erg.fehler == ["Kalender 1 nicht ladbar: FileNotFoundError"]
    assert any(t.titel == "Zahnarzt" for t in erg.termine)


def test_ohne_konfiguration():
    erg = kalender.lade_termine([], date(2026, 11, 13), date(2026, 11, 14), TZ)
    assert erg.termine == []
    assert erg.fehler == ["kein Kalender konfiguriert (ASSISTENT_ICAL fehlt)"]


def test_http_quelle(monkeypatch):
    class Antwort:
        content = (DATEN / "kalender.ics").read_bytes()

        def raise_for_status(self):
            pass

    gesehen = {}

    def fake_get(url, **kw):
        gesehen["url"] = url
        return Antwort()

    monkeypatch.setattr(kalender.httpx, "get", fake_get)
    erg = kalender.lade_termine(["webcal://example.org/basic.ics"], date(2026, 11, 13), date(2026, 11, 14), TZ)
    assert gesehen["url"] == "https://example.org/basic.ics" and not erg.fehler
