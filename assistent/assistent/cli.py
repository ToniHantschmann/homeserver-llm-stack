"""``assistent tag|woche|tasks|vergleich|neue-woche|hinzufuegen|erledigt|vorbereiten|api``.

Lesend: tag, woche, tasks, vergleich. Schreibend (wirkt über livesync-bridge auf
alle Geräte!): neue-woche, hinzufuegen, erledigt, vorbereiten – jeweils mit ``--dry-run``.
``api`` startet den HTTP-Dienst (Phase 5, siehe api.py).
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from . import dienste, llm, planung, vault
from .config import Config

# Für Aufrufer, die die Funktionen noch über cli importieren
fakten_tag = dienste.fakten_tag
fakten_woche = dienste.fakten_woche


def _datum(s: str | None, cfg: Config) -> date:
    return date.fromisoformat(s) if s else dienste.heute(cfg)


def _meta(e: llm.LLMErgebnis) -> str:
    s = (f"[{e.modell}: {e.sekunden:.1f} s, {e.prompt_tokens}+{e.completion_tokens} Tokens, "
         f"{e.versuche} Versuch(e)")
    if e.fallback:
        s += f", FALLBACK auf Faktentext – {e.fallback}"
    return s + "]"


def cmd_bericht(cfg: Config, a: argparse.Namespace) -> int:
    b = dienste.bericht(cfg, a.art, _datum(a.datum, cfg), a.modell, ohne_llm=a.ohne_llm)
    if b.rueckfall:
        print(f"[Rückfall auf {cfg.modell} – erster Lauf: {b.rueckfall}]", file=sys.stderr)
    if b.llm is None:
        print(b.fakten)
    elif a.dry_run:
        print(f"=== Titel\n{b.titel}\n\n=== Fakten\n{b.fakten}\n\n=== Text\n{b.text}\n\n{_meta(b.llm)}")
    else:
        print(b.text)
        if b.llm.fallback:
            print(_meta(b.llm), file=sys.stderr)
    return 0


def cmd_vergleich(cfg: Config, a: argparse.Namespace) -> int:
    fakten, termine = dienste.fakten(cfg, a.art, _datum(a.datum, cfg))
    # Gleiche Grenzen für alle Modelle; beim Wochenbericht die (großzügigen) Wochenwerte,
    # sonst würden Thinking-Aliase an max_tokens abgeschnitten
    _, max_tokens, timeout = dienste.llm_parameter(cfg, a.art, "x")
    cli = llm.client(cfg.llm_base_url, cfg.llm_key, timeout)
    print("=== Fakten\n" + fakten)
    for m in a.modelle.split(","):
        e = llm.formuliere(cli, m.strip(), a.art, fakten, termine, max_tokens=max_tokens)
        print(f"\n=== {m.strip()}\n{e.text}\n{_meta(e)}")
    return 0


def cmd_tasks(cfg: Config, a: argparse.Namespace) -> int:
    note = dienste.note(cfg, _datum(a.datum, cfg))
    if note.status is not vault.NoteStatus.OK:
        print(f"Weekly Note KW {note.kw:02d}: {note.status.value} ({note.pfad})")
        return 1
    for t in note.tasks:
        if a.alle or not t.erledigt:
            print(f"{'  ' * t.ebene}[{'x' if t.erledigt else ' '}] {t.text}  ({dienste.abschnitt_name(t)})")
    return 0


def cmd_neue_woche(cfg: Config, a: argparse.Namespace) -> int:
    j, w = planung.kw_von(_datum(a.datum, cfg))
    note = vault.lade_weekly(cfg.vault, cfg.weekly_muster, j, w)
    if note.status is vault.NoteStatus.OK:
        print(f"existiert schon: {note.pfad}")
        return 0
    if a.dry_run:
        print(f"würde anlegen ({note.status.value}): {note.pfad}\n---\n{vault.weekly_vorlage()}")
        return 0
    note = vault.lege_weekly_an(cfg.vault, cfg.weekly_muster, j, w)
    print(f"angelegt: {note.pfad}")
    return 0


def cmd_hinzufuegen(cfg: Config, a: argparse.Namespace) -> int:
    try:
        e = dienste.aufgabe_hinzufuegen(cfg, a.text, a.wann, _datum(a.datum, cfg),
                                        anlegen=a.anlegen, dry_run=a.dry_run)
    except dienste.DatumUnklar as ex:
        print(str(ex), file=sys.stderr)
        return 2
    except dienste.NoteFehlt as ex:
        print(f"{ex} – mit --anlegen erzeugen", file=sys.stderr)
        return 1
    if e.dry_run:
        print(f"würde in {e.datei} unter {e.abschnitt} einfügen: {e.zeile}"
              + (" (Note würde angelegt)" if e.angelegt else ""))
    else:
        print(f"{e.datei} / {e.abschnitt}: {e.zeile}")
    return 0


def cmd_erledigt(cfg: Config, a: argparse.Namespace) -> int:
    try:
        t = dienste.aufgabe_erledigt(cfg, a.text, _datum(a.datum, cfg), dry_run=a.dry_run)
    except dienste.NoteFehlt as e:
        print(str(e), file=sys.stderr)
        return 1
    except vault.Mehrdeutig as e:
        print("mehrdeutig, bitte genauer: " + " | ".join(k.text for k in e.kandidaten), file=sys.stderr)
        return 3
    except vault.KeinTreffer as e:
        print(str(e), file=sys.stderr)
        return 4
    print(f"{'würde abhaken' if a.dry_run else 'abgehakt'}: {t.text}")
    return 0


def cmd_vorbereiten(cfg: Config, a: argparse.Namespace) -> int:
    try:
        v = dienste.woche_vorbereiten(cfg, _datum(a.datum, cfg), dry_run=a.dry_run, ohne_llm=a.ohne_llm)
    except vault.VeralteteNote as e:
        print(str(e), file=sys.stderr)
        return 1
    print(("[dry-run] " if v.dry_run else "") + v.meldung())
    print(f"Datei: {v.datei}")
    for x in v.vorschlaege:
        print(f"  + {x.zeile()}   [{x.kandidat.kategorie.name}, {x.quelle}]")
    for grund in v.uebersprungen:
        print(f"  - {grund}")
    if v.llm:
        print(f"[{v.llm.modell}: {v.llm.sekunden:.1f} s, {v.llm.prompt_tokens}+"
              f"{v.llm.completion_tokens} Tokens{', ' + v.llm.fehler if v.llm.fehler else ''}]")
        if a.dry_run:
            print(f"LLM roh: {v.llm.roh}")
    return 0


def cmd_api(cfg: Config, a: argparse.Namespace) -> int:
    import logging

    import uvicorn

    from .api import erstelle_app

    # Eigene Zeilen (Dauer, Tokens, FALLBACK, Schreibvorgänge) ins docker logs
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    # httpx loggt auf INFO jede URL – darunter die private iCal-Adresse, ein Geheimnis
    # (und openai/LiteLLM-Aufrufe). Nur Warnungen durchlassen.
    for name in ("httpx", "httpcore", "openai"):
        logging.getLogger(name).setLevel(logging.WARNING)
    uvicorn.run(erstelle_app(cfg), host=a.host, port=a.port, log_level="info",
                proxy_headers=False, server_header=False, date_header=False)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="assistent")
    p.add_argument("--vault", type=Path, help="überschreibt ASSISTENT_VAULT")
    sub = p.add_subparsers(dest="cmd", required=True)

    for art in ("tag", "woche"):
        s = sub.add_parser(art, help=f"{'Tages' if art == 'tag' else 'Wochen'}zusammenfassung")
        s.add_argument("--datum", help="YYYY-MM-DD statt heute")
        s.add_argument("--dry-run", action="store_true", help="Fakten + LLM-Text + Messwerte zeigen")
        s.add_argument("--ohne-llm", action="store_true", help="nur den Faktenblock")
        s.add_argument("--modell", help="LiteLLM-Alias statt ASSISTENT_MODELL")
        s.set_defaults(fn=cmd_bericht, art=art)

    s = sub.add_parser("vergleich", help="denselben Faktenblock an mehrere Aliase")
    s.add_argument("art", choices=["tag", "woche"])
    s.add_argument("--modelle", default="assistent,assistent-gruendlich",
                   help="kommagetrennte LiteLLM-Aliase (assistent-cloud nur mit API-Key)")
    s.add_argument("--datum")
    s.set_defaults(fn=cmd_vergleich)

    s = sub.add_parser("tasks", help="Aufgaben der Weekly Note")
    s.add_argument("--alle", action="store_true")
    s.add_argument("--datum")
    s.set_defaults(fn=cmd_tasks)

    s = sub.add_parser("neue-woche", help="Weekly Note der Woche anlegen (SCHREIBT)")
    s.add_argument("--datum")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_neue_woche)

    s = sub.add_parser("hinzufuegen", help="Aufgabe einfügen (SCHREIBT)")
    s.add_argument("text")
    s.add_argument("--wann", help='z. B. "Freitag", "morgen", "15.11.", "nächsten Montag"')
    s.add_argument("--datum")
    s.add_argument("--anlegen", action="store_true", help="fehlende Weekly Note anlegen")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_hinzufuegen)

    s = sub.add_parser("erledigt", help="passende offene Aufgabe abhaken (SCHREIBT)")
    s.add_argument("text")
    s.add_argument("--datum")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_erledigt)

    s = sub.add_parser("vorbereiten",
                       help="Note der nächsten Woche anlegen + Kalender-Vorschläge eintragen (SCHREIBT)")
    s.add_argument("--datum", help="Bezugstag (Ziel: Woche ab dem nächsten Montag)")
    s.add_argument("--ohne-llm", action="store_true", help="nur Vorlagentexte")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_vorbereiten)

    s = sub.add_parser("api", help="HTTP-Dienst starten (Phase 5)")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8100)
    s.set_defaults(fn=cmd_api)

    a = p.parse_args(argv)
    cfg = Config.aus_umgebung()
    if a.vault:
        cfg.vault = a.vault
    return a.fn(cfg, a)


if __name__ == "__main__":
    sys.exit(main())
