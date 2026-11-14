"""HTTP-API (Phase 5): Auth, Statuscodes, Schreibwege – ohne Netz und ohne LLM."""

from datetime import datetime

import pytest
from conftest import KW35, KW35_DONNERSTAG, MUSTER, lege_note
from fastapi.testclient import TestClient

from assistent import api, dienste, llm
from assistent.config import Config

TOKEN = "t" * 32
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def note35(vault_dir):
    return lege_note(vault_dir, 35, KW35, datetime(2026, 8, 27, 9))


@pytest.fixture
def client(vault_dir, note35, monkeypatch):
    monkeypatch.setattr(dienste, "heute", lambda cfg: KW35_DONNERSTAG)
    cfg = Config(vault=vault_dir, weekly_muster=MUSTER, api_token=TOKEN)
    return TestClient(api.erstelle_app(cfg))


def test_start_ohne_token_verweigert(vault_dir):
    with pytest.raises(SystemExit):
        api.erstelle_app(Config(vault=vault_dir))
    with pytest.raises(SystemExit):
        api.erstelle_app(Config(vault=vault_dir, api_token="kurz"))


def test_gesund_ohne_auth(client):
    assert client.get("/gesund").json() == {"ok": True}


@pytest.mark.parametrize("pfad", ["/tasks", "/zusammenfassung/tag"])
@pytest.mark.parametrize("header", [{}, {"Authorization": "Bearer falsch"}, {"Authorization": TOKEN}])
def test_ohne_gueltigen_token_401(client, pfad, header):
    r = client.get(pfad, headers=header)
    assert r.status_code == 401 and r.json()["fehler"] == "auth"


def test_keine_doku_endpunkte(client):
    for p in ("/docs", "/openapi.json", "/redoc"):
        assert client.get(p).status_code == 404


def test_tasks_offen(client):
    r = client.get("/tasks", headers=AUTH)
    assert r.status_code == 200
    j = r.json()
    assert j["kw"] == 35
    texte = [t["text"] for t in j["tasks"]]
    assert "Blumen besorgen" in texte and "Socken kaufen" not in texte
    assert "" not in texte
    alle = client.get("/tasks?offen=false", headers=AUTH).json()["tasks"]
    assert "Socken kaufen" in [t["text"] for t in alle]


def test_tasks_note_fehlt_404(client):
    r = client.get("/tasks?datum=2026-09-10", headers=AUTH)
    assert r.status_code == 404 and r.json()["fehler"] == "note_fehlt"


def test_zusammenfassung_ohne_llm(client):
    r = client.get("/zusammenfassung/tag?ohne_llm=true", headers=AUTH)
    assert r.status_code == 200
    j = r.json()
    assert j["titel"] == "Dein Donnerstag, 27. Aug"
    assert j["fallback"] == "ohne_llm" and "Sachen packen" in j["text"]


def test_zusammenfassung_mit_llm_fallback_bleibt_200(client, monkeypatch):
    """LLM nicht erreichbar → Faktentext statt Fehler, damit der Push trotzdem kommt."""
    def kaputt(*a, **k):
        return llm.LLMErgebnis(text=a[3], modell="assistent", fallback="LLM-Fehler: ConnectError")
    monkeypatch.setattr(llm, "formuliere", kaputt)
    j = client.get("/zusammenfassung/woche", headers=AUTH).json()
    assert j["fallback"].startswith("LLM-Fehler") and j["titel"] == "Wochenbericht – Ausblick KW 36"
    assert j["text"]


def test_unbekannte_art_422(client):
    assert client.get("/zusammenfassung/monat", headers=AUTH).status_code == 422


def test_task_hinzufuegen_tagesabschnitt(client, note35):
    r = client.post("/tasks", headers=AUTH, json={"text": "Steuer  abgeben", "wann": "Freitag"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["abschnitt"] == "Freitag" and j["meldung"] == "Hinzugefügt: Steuer abgeben für Freitag."
    assert "- [ ] Steuer abgeben\n" in note35.read_text(encoding="utf-8")


def test_task_hinzufuegen_alias_faellig_und_dry_run(client, note35):
    vorher = note35.read_bytes()
    r = client.post("/tasks", headers=AUTH, json={"text": "Test", "faellig": "morgen", "dry_run": True})
    assert r.status_code == 200 and r.json()["dry_run"] and r.json()["abschnitt"] == "Freitag"
    assert note35.read_bytes() == vorher


def test_task_hinzufuegen_datum_unklar_422(client, note35):
    vorher = note35.read_bytes()
    r = client.post("/tasks", headers=AUTH, json={"text": "X", "wann": "irgendwann mal"})
    assert r.status_code == 422 and r.json()["fehler"] == "datum"
    assert note35.read_bytes() == vorher


def test_task_hinzufuegen_leer_422(client):
    assert client.post("/tasks", headers=AUTH, json={"text": ""}).status_code == 422


def test_task_erledigt(client, note35):
    r = client.post("/tasks/erledigt", headers=AUTH, json={"text": "blumen besorgen"})
    assert r.status_code == 200 and r.json()["meldung"] == "Abgehakt: Blumen besorgen."
    assert "- [x] Blumen besorgen" in note35.read_text(encoding="utf-8")


def test_task_erledigt_kein_treffer_404(client, note35):
    vorher = note35.read_bytes()
    r = client.post("/tasks/erledigt", headers=AUTH, json={"text": "Mond landen"})
    assert r.status_code == 404 and r.json()["fehler"] == "kein_treffer"
    assert note35.read_bytes() == vorher


def test_task_erledigt_mehrdeutig_409(client, note35):
    vorher = note35.read_bytes()
    r = client.post("/tasks/erledigt", headers=AUTH, json={"text": "Bewerbung"})
    assert r.status_code == 409
    j = r.json()
    assert j["fehler"] == "mehrdeutig" and len(j["kandidaten"]) >= 2 and j["meldung"].startswith("Meinst du")
    assert note35.read_bytes() == vorher
