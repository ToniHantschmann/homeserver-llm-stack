"""HTTP-API (Phase 5): Home Assistant holt hier Berichte ab und schreibt Aufgaben.

Nur auf 127.0.0.1 veröffentlicht (Home Assistant läuft mit network_mode: host),
jede Anfrage außer ``/gesund`` braucht ``Authorization: Bearer <ASSISTENT_API_TOKEN>``.
Ohne Token startet der Dienst nicht.

| Methode | Pfad                       | Zweck                                         |
|---------|----------------------------|-----------------------------------------------|
| GET     | /gesund                    | Healthcheck, ohne Auth, verrät nichts         |
| GET     | /zusammenfassung/tag       | Text für Push / Vorlesen                      |
| GET     | /zusammenfassung/woche     | Wochenbericht                                 |
| GET     | /tasks?offen=true          | Aufgaben der aktuellen Weekly Note            |
| POST    | /tasks                     | {text, wann?} → Zeile einfügen   (SCHREIBT)   |
| POST    | /tasks/erledigt            | {text} → passende Aufgabe abhaken (SCHREIBT)  |
| POST    | /woche/vorbereiten         | Note nächste Woche + Kalender-Vorschläge (SCHREIBT) |

Jede schreibende Antwort hat ein Feld ``meldung`` – ein fertiger deutscher Satz
für die gesprochene Bestätigung in Phase 6.
"""

from __future__ import annotations

import hmac
import logging
import threading
from datetime import date
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import AliasChoices, BaseModel, Field

from . import dienste, vault
from .config import Config

log = logging.getLogger("assistent.api")


class AufgabeNeu(BaseModel):
    text: str = Field(min_length=1, max_length=300)
    # Plan nennt das Feld "faellig"; beides wird angenommen
    wann: str | None = Field(default=None, max_length=60, validation_alias=AliasChoices("wann", "faellig"))
    anlegen: bool = True  # fehlende Weekly Note der Zielwoche anlegen (nie überschreiben)
    dry_run: bool = False


class WocheVorbereiten(BaseModel):
    datum: date | None = None  # Bezugstag; Ziel ist die Woche ab dem nächsten Montag
    ohne_llm: bool = False
    dry_run: bool = False


class AufgabeErledigt(BaseModel):
    text: str = Field(min_length=1, max_length=300)
    dry_run: bool = False


def _fehler(status: int, fehler: str, meldung: str, **extra) -> HTTPException:
    return HTTPException(status, detail={"fehler": fehler, "meldung": meldung, **extra})


def erstelle_app(cfg: Config) -> FastAPI:
    if not cfg.api_token or len(cfg.api_token) < 24:
        raise SystemExit("ASSISTENT_API_TOKEN fehlt oder ist kürzer als 24 Zeichen – Start verweigert")

    app = FastAPI(title="assistent", docs_url=None, redoc_url=None, openapi_url=None)
    # Ein Schreiber zur Zeit: zwei gleichzeitige Einfügungen in dieselbe Datei
    # würden sich sonst gegenseitig den Offset verschieben.
    schreib_lock = threading.Lock()
    # Ollama rechnet ohnehin nur eine Anfrage gleichzeitig (NUM_PARALLEL=1);
    # so wartet ein zweiter Bericht hier statt im LiteLLM-Timeout.
    llm_lock = threading.Lock()

    def auth(request: Request) -> None:
        h = request.headers.get("authorization", "")
        if not (h.startswith("Bearer ") and hmac.compare_digest(h[7:].encode(), cfg.api_token.encode())):
            raise HTTPException(401, detail={"fehler": "auth", "meldung": "Nicht autorisiert."})

    @app.exception_handler(HTTPException)
    async def _http(_: Request, e: HTTPException):
        body = e.detail if isinstance(e.detail, dict) else {"fehler": "http", "meldung": str(e.detail)}
        return JSONResponse(body, status_code=e.status_code)

    @app.exception_handler(Exception)
    async def _intern(_: Request, e: Exception):
        log.exception("unerwarteter Fehler")
        return JSONResponse({"fehler": "intern", "meldung": f"Interner Fehler: {type(e).__name__}"},
                            status_code=500)

    def _tag(datum: date | None) -> date:
        return datum or dienste.heute(cfg)

    @app.get("/gesund")
    def gesund():
        return {"ok": True}

    @app.get("/zusammenfassung/{art}", dependencies=[Depends(auth)])
    def zusammenfassung(art: Literal["tag", "woche"], datum: date | None = None, ohne_llm: bool = False):
        with llm_lock:
            b = dienste.bericht(cfg, art, _tag(datum), ohne_llm=ohne_llm)
        e = b.llm
        if e:
            log.info("%s %s: %s, %.1f s, %d+%d Tokens, %d Versuch(e)%s", art, b.datum, e.modell,
                     e.sekunden, e.prompt_tokens, e.completion_tokens, e.versuche,
                     f", FALLBACK: {e.fallback}" if e.fallback else "")
        if b.rueckfall:
            log.warning("%s %s: Rückfall auf %s – erster Lauf: %s", art, b.datum, e.modell if e else "-",
                        b.rueckfall)
        return {
            "art": art,
            "datum": b.datum.isoformat(),
            "titel": b.titel,
            "text": b.text,
            "fallback": e.fallback if e else "ohne_llm",
            "modell": e.modell if e else None,
            "sekunden": round(e.sekunden, 1) if e else 0.0,
        }

    @app.get("/tasks", dependencies=[Depends(auth)])
    def tasks(offen: bool = True, datum: date | None = None):
        n = dienste.note(cfg, _tag(datum))
        if n.status is not vault.NoteStatus.OK:
            raise _fehler(404, "note_" + n.status.name.lower(),
                          f"Für KW {n.kw:02d} gibt es keine Wochenplanung.", datei=n.pfad.name)
        return {
            "kw": n.kw,
            "datei": n.pfad.name,
            "tasks": [
                {"text": t.text, "erledigt": t.erledigt, "abschnitt": dienste.abschnitt_name(t),
                 "ebene": t.ebene, "faellig": t.faellig.isoformat() if t.faellig else None}
                for t in n.tasks if not (offen and t.erledigt) and t.text.strip()
            ],
        }

    @app.post("/tasks", dependencies=[Depends(auth)])
    def task_neu(a: AufgabeNeu):
        try:
            with schreib_lock:
                e = dienste.aufgabe_hinzufuegen(cfg, a.text, a.wann, dienste.heute(cfg),
                                                anlegen=a.anlegen, dry_run=a.dry_run)
        except dienste.DatumUnklar:
            raise _fehler(422, "datum", f"Das Datum „{a.wann}“ habe ich nicht verstanden.")
        except dienste.NoteFehlt as ex:
            raise _fehler(409, "note_fehlt", f"Die Wochenplanung für KW {ex.note.kw:02d} fehlt.")
        except vault.VeralteteNote as ex:
            raise _fehler(409, "note_veraltet", str(ex))
        text = " ".join(a.text.split())
        if e.abschnitt == "ToDo":
            wo = f", fällig {e.faellig:%d.%m.}" if e.faellig else " zur Wochenliste"
        else:
            wo = f" für {e.abschnitt}"
        log.info("task_neu%s: %s / %s: %s", " (dry-run)" if e.dry_run else "", e.datei, e.abschnitt, e.zeile)
        return {
            "datei": e.datei, "abschnitt": e.abschnitt, "zeile": e.zeile,
            "faellig": e.faellig.isoformat() if e.faellig else None,
            "angelegt": e.angelegt, "dry_run": e.dry_run,
            "meldung": f"Hinzugefügt: {text}{wo}.",
        }

    @app.post("/woche/vorbereiten", dependencies=[Depends(auth)])
    def woche_vorbereiten(a: WocheVorbereiten | None = None):
        a = a or WocheVorbereiten()
        try:
            with llm_lock, schreib_lock:
                v = dienste.woche_vorbereiten(cfg, _tag(a.datum), dry_run=a.dry_run, ohne_llm=a.ohne_llm)
        except vault.VeralteteNote as ex:
            raise _fehler(409, "note_veraltet", str(ex))
        e = v.llm
        log.info("woche_vorbereiten%s: %s, angelegt=%s, %d Vorschläge (%s), %d übersprungen%s",
                 " (dry-run)" if v.dry_run else "", v.datei, v.angelegt, len(v.vorschlaege),
                 ", ".join(x.quelle for x in v.vorschlaege) or "-", len(v.uebersprungen),
                 f", LLM {e.sekunden:.1f} s" if e else "")
        for grund in v.uebersprungen:
            log.info("  übersprungen: %s", grund)
        return {
            "kw": v.kw, "datei": v.datei, "angelegt": v.angelegt, "dry_run": v.dry_run,
            "vorschlaege": [
                {"text": x.text, "faellig": x.kandidat.faellig.isoformat(), "termin": x.kandidat.termin.titel,
                 "termin_datum": x.kandidat.datum.isoformat(), "kategorie": x.kandidat.kategorie.name,
                 "quelle": x.quelle, "zeile": x.zeile()}
                for x in v.vorschlaege
            ],
            "uebersprungen": v.uebersprungen,
            "hinweise": v.hinweise,
            "meldung": v.meldung(),
        }

    @app.post("/tasks/erledigt", dependencies=[Depends(auth)])
    def task_erledigt(a: AufgabeErledigt):
        try:
            with schreib_lock:
                t = dienste.aufgabe_erledigt(cfg, a.text, dienste.heute(cfg), dry_run=a.dry_run)
        except dienste.NoteFehlt as ex:
            raise _fehler(409, "note_fehlt", f"Die Wochenplanung für KW {ex.note.kw:02d} fehlt.")
        except vault.Mehrdeutig as ex:
            k = [x.text for x in ex.kandidaten]
            raise _fehler(409, "mehrdeutig", "Meinst du " + " oder ".join(f"„{x}“" for x in k[:3]) + "?",
                          kandidaten=k)
        except vault.KeinTreffer:
            raise _fehler(404, "kein_treffer", f"Keine offene Aufgabe passt zu „{a.text}“.")
        log.info("task_erledigt%s: %s", " (dry-run)" if a.dry_run else "", t.text)
        return {"text": t.text, "abschnitt": dienste.abschnitt_name(t), "dry_run": a.dry_run,
                "meldung": f"Abgehakt: {t.text}."}

    return app
