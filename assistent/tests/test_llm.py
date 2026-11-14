"""LLM-Logik ohne Netz: ein Fake-Client statt LiteLLM."""

from types import SimpleNamespace

from assistent import llm

FAKTEN = "Termine heute:\n- 09:00–10:00 Zahnarzt\n- 17:45 Mama Flughafen"


def test_pruefe_uhrzeiten():
    assert llm.pruefe_uhrzeiten("Um 09:00 Zahnarzt, 17:45 Flughafen.", FAKTEN) == []
    assert llm.pruefe_uhrzeiten("Um 9 Uhr Zahnarzt.", FAKTEN) == []
    assert llm.pruefe_uhrzeiten("Um 11:00 Meeting und 14 Uhr Sport.", FAKTEN) == ["11:00", "14:00"]
    assert llm.pruefe_uhrzeiten("Bis 15.11. erledigen, 30 Min. lernen.", FAKTEN) == []


class FakeClient:
    def __init__(self, antworten):
        self.antworten = list(antworten)
        self.aufrufe = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.aufrufe.append(kw)
        a = self.antworten.pop(0)
        if isinstance(a, Exception):
            raise a
        text, ende = a if isinstance(a, tuple) else (a, "stop")
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=text), finish_reason=ende)],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20),
        )


def test_formuliere_abgeschnitten_ist_fallback_ohne_neuversuch():
    """Thinking frisst max_tokens: kein zweiter Versuch, der liefe in dieselbe Grenze."""
    c = FakeClient([("Um 09:00 Zah", "length"), "nie benutzt"])
    e = llm.formuliere(c, "assistent-gruendlich", "tag", FAKTEN, max_tokens=50)
    assert e.text == FAKTEN and "abgeschnitten" in e.fallback and len(c.aufrufe) == 1
    assert c.aufrufe[0]["max_tokens"] == 50


def test_formuliere_ok_und_think_wird_entfernt():
    c = FakeClient(["<think>hm</think>Heute um 09:00 Zahnarzt."])
    e = llm.formuliere(c, "assistent", "tag", FAKTEN)
    assert e.text == "Heute um 09:00 Zahnarzt." and e.fallback is None and e.versuche == 1
    assert c.aufrufe[0]["model"] == "assistent"
    assert FAKTEN in c.aufrufe[0]["messages"][1]["content"]


def test_formuliere_korrigiert_erfundene_uhrzeit():
    c = FakeClient(["Um 11:00 Zahnarzt.", "Um 09:00 Zahnarzt."])
    e = llm.formuliere(c, "assistent", "tag", FAKTEN)
    assert e.text == "Um 09:00 Zahnarzt." and e.versuche == 2 and e.fallback is None
    assert "11:00" in c.aufrufe[1]["messages"][-1]["content"]
    assert e.prompt_tokens == 200


def test_formuliere_fallback_auf_fakten():
    c = FakeClient(["Um 11:00.", "Um 12:00."])
    e = llm.formuliere(c, "assistent", "tag", FAKTEN)
    assert e.text == FAKTEN and "12:00" in e.fallback


def test_formuliere_netzfehler():
    e = llm.formuliere(FakeClient([ConnectionError("weg")]), "assistent", "woche", FAKTEN)
    assert e.text == FAKTEN and e.fallback.startswith("LLM-Fehler: ConnectionError")


def test_fehlende_termine():
    assert llm.fehlende_termine("Dienstag Bogentraining um 19:00.", ["Bogentraining (verschoben)", "Nachtzug"]) == ["Nachtzug"]


def test_formuliere_fordert_fehlenden_termin_nach():
    c = FakeClient(["Um 09:00 Zahnarzt.", "Um 09:00 Zahnarzt, 17:45 Mama Flughafen."])
    e = llm.formuliere(c, "assistent", "tag", FAKTEN, termine=["Zahnarzt", "Mama Flughafen"])
    assert e.fallback is None and e.versuche == 2
    assert "Mama Flughafen" in c.aufrufe[1]["messages"][-1]["content"]


def test_wochenprompt_ohne_aufgaben_fragt_nicht_nach_beispielen():
    from assistent.planung import KEINE_AUFGABEN
    leer = llm.prompt_fuer("woche", "Rückblick KW 46\n" + KEINE_AUFGABEN)
    assert "Beispiele" not in leer and "KEINE erledigten" in leer
    assert "Beispiele" in llm.prompt_fuer("woche", "Rückblick KW 35\nErledigt: 3, offen: 1")


# ---------------------------------------------------------------- gründlicher Wochenbericht


def _cfg(tmp_path, **kw):
    from assistent.config import Config
    return Config(vault=tmp_path, modell="assistent", **kw)


def test_llm_parameter_nur_woche_gruendlich(tmp_path):
    from assistent import dienste
    cfg = _cfg(tmp_path, modell_woche="assistent-gruendlich", max_tokens_woche=2500, llm_timeout_woche=180)
    assert dienste.llm_parameter(cfg, "woche") == ("assistent-gruendlich", 2500, 180)
    assert dienste.llm_parameter(cfg, "tag") == ("assistent", 700, 180.0)
    assert dienste.llm_parameter(cfg, "woche", "assistent-9b-gruendlich")[0] == "assistent-9b-gruendlich"
    # ohne ASSISTENT_MODELL_WOCHE: wie bisher
    assert dienste.llm_parameter(_cfg(tmp_path), "woche")[0] == "assistent"


def _bericht_mit(monkeypatch, tmp_path, antworten):
    from assistent import dienste
    c = FakeClient(antworten)
    monkeypatch.setattr(dienste, "fakten", lambda cfg, art, d: (FAKTEN, ["Zahnarzt"]))
    monkeypatch.setattr(dienste.llm, "client", lambda *a, **kw: c)
    cfg = _cfg(tmp_path, modell_woche="assistent-gruendlich", max_tokens_woche=2500)
    from datetime import date
    return dienste.bericht(cfg, "woche", date(2026, 11, 15)), c


def test_bericht_woche_gruendlich_ok(monkeypatch, tmp_path):
    b, c = _bericht_mit(monkeypatch, tmp_path, ["Um 09:00 Zahnarzt."])
    assert b.text == "Um 09:00 Zahnarzt." and b.rueckfall is None
    assert c.aufrufe[0]["model"] == "assistent-gruendlich" and c.aufrufe[0]["max_tokens"] == 2500


def test_bericht_woche_rueckfall_auf_schnelles_modell(monkeypatch, tmp_path):
    # gründlich: erfundene Uhrzeit → kein zweiter gründlicher Versuch, sondern assistent
    b, c = _bericht_mit(monkeypatch, tmp_path, ["Um 11:00 Zahnarzt.", "Um 09:00 Zahnarzt."])
    assert [a["model"] for a in c.aufrufe] == ["assistent-gruendlich", "assistent"]
    assert b.text == "Um 09:00 Zahnarzt." and b.llm.fallback is None
    assert b.rueckfall.startswith("assistent-gruendlich: erfundene Uhrzeiten")


def test_bericht_woche_beide_scheitern_faktenblock(monkeypatch, tmp_path):
    b, c = _bericht_mit(monkeypatch, tmp_path, [TimeoutError("zu lang"), ConnectionError("weg")])
    assert len(c.aufrufe) == 2 and b.text == FAKTEN and b.llm.fallback and b.rueckfall


def test_vorschlagssatz_nur_wenn_vorschlaege_im_faktenblock():
    ohne = llm.prompt_fuer("woche", "Rückblick KW 46\nErledigt: 0, offen: 1")
    mit = llm.prompt_fuer("woche", "Rückblick KW 46\nVorschläge aus dem Kalender (…):\n- X")
    assert "auf Übernahme warten" not in ohne
    assert "auf Übernahme warten" in mit
    from assistent.planung import KEINE_AUFGABEN
    assert "auf Übernahme warten" not in llm.prompt_fuer("woche", KEINE_AUFGABEN)
    assert "{vorschlaege}" not in ohne + mit
