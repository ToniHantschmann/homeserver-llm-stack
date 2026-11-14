"""Text über LiteLLM (OpenAI-kompatibel). Clients nutzen nur Aliase, nie Backend-Namen.

Schutz gegen erfundene Termine (Risiko-Tabelle im Plan): jede Uhrzeit im LLM-Text
muss im Faktenblock vorkommen. Bei einem Verstoß wird einmal mit Hinweis neu
gefragt; scheitert auch das, wird der deterministische Faktentext zurückgegeben.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from openai import OpenAI

from .planung import KEINE_AUFGABEN, erlaubte_uhrzeiten

SYSTEM = (
    "Du bist ein persönlicher Assistent und schreibst auf Deutsch in der Du-Form. "
    "Du bekommst einen Faktenblock. Verwende ausschließlich diese Fakten: erfinde keine "
    "Termine, Uhrzeiten, Orte oder Aufgaben und lass keinen Termin weg. Nenne Uhrzeiten "
    "genau so wie im Faktenblock (HH:MM). Keine Überschriften, kein Markdown, keine "
    "Emojis, keine Rückfragen, keine Begrüßungsfloskel am Ende."
)

PROMPT_TAG = (
    "Schreibe die Tageszusammenfassung für eine Push-Nachricht am Morgen, höchstens "
    "6 kurze Sätze. Reihenfolge: Termine des Tages, die wichtigsten Aufgaben, "
    "Überfälliges. Wenn es Überschneidungen gibt, sag das zuerst. Wenn es freie Zeit "
    "gibt, schlag knapp vor, welche Aufgabe da hineinpasst. Steht unter Hinweise etwas, "
    "gib den Inhalt am Schluss in einem Satz wieder (nicht nur „siehe Hinweise“).\n\n"
    "Faktenblock:\n{fakten}"
)

PROMPT_WOCHE = (
    "Schreibe den Wochenbericht für Sonntagabend, höchstens 10 Sätze in zwei Absätzen. "
    "Erster Absatz, Rückblick: Anzahl erledigt/offen, zwei bis drei Beispiele für "
    "Erledigtes, dann alle offenen Punkte. Termine unter „Noch vor Wochenbeginn“ liegen "
    "noch vor dir (heute Abend oder morgen früh) – nenne sie als bevorstehend, nie als "
    "erledigt. Zweiter Absatz, Ausblick: alle Termine der nächsten Woche mit Tag und "
    "Uhrzeit und ein Vorschlag, welche offenen Punkte übernommen werden sollten.{vorschlaege} "
    "Steht unter Hinweise etwas, gib den Inhalt am Schluss in einem Satz wieder.\n\n"
    "Faktenblock:\n{fakten}"
)

# Ohne Aufgaben-Daten darf der Prompt nicht nach "Beispielen" fragen – gemma4:e4b
# hat dann "Dokument A, Thema B, Bericht C" als erledigt erfunden (Test 2026-11-13).
PROMPT_WOCHE_OHNE_AUFGABEN = (
    "Schreibe den Wochenbericht für Sonntagabend, höchstens 8 Sätze in zwei Absätzen. "
    "Erster Absatz: Für die vergangene Woche liegen keine Aufgaben vor – sag das in "
    "einem Satz und nenne KEINE erledigten oder offenen Aufgaben. Termine unter „Noch "
    "vor Wochenbeginn“ liegen noch vor dir – nenne sie als bevorstehend. Zweiter Absatz, "
    "Ausblick: alle Termine der nächsten Woche mit Tag und Uhrzeit.{vorschlaege} Steht unter "
    "Hinweise etwas, gib den Inhalt am Schluss in einem Satz wieder.\n\nFaktenblock:\n{fakten}"
)



# Nur einfügen, wenn der Faktenblock Vorschläge enthält: als feste Bedingung im Prompt
# ("Stehen Vorschläge im Faktenblock, …") hat gemma4:e4b ohne Thinking sie auch dann
# erwähnt, wenn es keine gab (Test 2026-11-14).
VORSCHLAG_SATZ = (
    " Nenne außerdem kurz die „Vorschläge aus dem Kalender“ und sag, dass sie in der "
    "Weekly Note unter „Vorschläge“ auf Übernahme warten."
)
VORSCHLAG_MARKE = "Vorschläge aus dem Kalender"


def prompt_fuer(art: str, fakten: str) -> str:
    if art == "tag":
        return PROMPT_TAG.format(fakten=fakten)
    v = VORSCHLAG_SATZ if VORSCHLAG_MARKE in fakten else ""
    vorlage = PROMPT_WOCHE_OHNE_AUFGABEN if KEINE_AUFGABEN in fakten else PROMPT_WOCHE
    return vorlage.format(fakten=fakten, vorschlaege=v)

_ZEIT_IM_TEXT = re.compile(
    r"(?<![\d:.])([01]?\d|2[0-3])(?::([0-5]\d)(?!\d)|\.([0-5]\d)(?=\s*Uhr)|(?=\s*Uhr))"
)
_THINK = re.compile(r"<think>.*?</think>", re.S)


def _kern(titel: str) -> str:
    return " ".join(re.sub(r"\(.*?\)", "", titel).lower().split())


def fehlende_termine(text: str, titel: list[str]) -> list[str]:
    """Termine aus dem Faktenblock, die im Text nicht vorkommen ("lass keinen weg")."""
    t = " ".join(text.lower().split())
    return [x for x in titel if _kern(x) and _kern(x) not in t]


def pruefe_uhrzeiten(text: str, fakten: str) -> list[str]:
    """Uhrzeiten im Text, die nicht im Faktenblock stehen ("9 Uhr" zählt als 09:00)."""
    erlaubt = erlaubte_uhrzeiten(fakten)
    falsch = []
    for m in _ZEIT_IM_TEXT.finditer(text):
        hm = f"{int(m.group(1)):02d}:{m.group(2) or m.group(3) or '00'}"
        if hm not in erlaubt and hm not in falsch:
            falsch.append(hm)
    return falsch


@dataclass
class LLMErgebnis:
    text: str
    modell: str
    sekunden: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    verstoesse: list[str] = field(default_factory=list)  # erfundene Uhrzeiten (letzter Versuch)
    fehlend: list[str] = field(default_factory=list)  # weggelassene Termine (letzter Versuch)
    versuche: int = 0
    fallback: str | None = None  # Grund, falls der Faktentext statt LLM-Text kam


SYSTEM_VORSCHLAG = (
    "Du formulierst Einträge für eine deutsche To-do-Liste. Antworte ausschließlich mit "
    "einem JSON-Objekt, ohne Erklärung."
)

PROMPT_VORSCHLAG = (
    "Für jeden nummerierten Termin unten soll in der To-do-Liste eine Vorbereitungsaufgabe "
    "stehen. Die Kategorie sagt, was zu tun ist. Formuliere je Termin genau eine kurze, "
    "konkrete Aufgabe im To-do-Stil (3 bis 8 Wörter, Infinitiv am Ende, z. B. „Folien für "
    "den DBS-Vortrag fertigstellen“). Übernimm Namen und Stichwörter aus dem Termintitel. "
    "Keine Uhrzeiten, keine Datumsangaben, nichts hinzuerfinden, was nicht im Titel steht.\n"
    "Wenn eine der bestehenden Aufgaben dieselbe Vorbereitung schon abdeckt, gib für diesen "
    "Termin null statt eines Textes.\n\n"
    "Termine:\n{termine}\n\nBestehende Aufgaben:\n{bestehend}\n\n"
    'Antwortformat: {{"1": "Aufgabe" oder null, "2": ...}} – ein Eintrag pro Terminnummer.'
)


@dataclass
class VorschlagAntwort:
    roh: str | None  # Text des Modells oder None bei Ausfall
    modell: str
    sekunden: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    fehler: str | None = None


def frage_vorschlaege(cli: OpenAI, modell: str, termine: str, bestehend: list[str]) -> VorschlagAntwort:
    """Ein Aufruf für alle Kandidaten. Prüfen und Rückfall auf Vorlagen: vorschlaege.py."""
    erg = VorschlagAntwort(None, modell)
    prompt = PROMPT_VORSCHLAG.format(
        termine=termine, bestehend="\n".join(f"- {b}" for b in bestehend) or "- (keine)"
    )
    t0 = time.monotonic()
    try:
        r = cli.chat.completions.create(
            model=modell,
            messages=[{"role": "system", "content": SYSTEM_VORSCHLAG}, {"role": "user", "content": prompt}],
            temperature=0.2, max_tokens=500, response_format={"type": "json_object"},
        )
        erg.roh = _THINK.sub("", r.choices[0].message.content or "").strip()
        if r.usage:
            erg.prompt_tokens = r.usage.prompt_tokens or 0
            erg.completion_tokens = r.usage.completion_tokens or 0
    except Exception as e:
        erg.fehler = f"LLM-Fehler: {type(e).__name__}: {str(e)[:200]}"
    erg.sekunden = time.monotonic() - t0
    return erg


def client(base_url: str, key: str, timeout: float = 180.0) -> OpenAI:
    # Erster Aufruf nach keep_alive lädt das Modell (~10 s); Wochenbericht dauert länger.
    return OpenAI(base_url=base_url, api_key=key or "kein-key", timeout=timeout, max_retries=0)


def formuliere(
    cli: OpenAI, modell: str, art: str, fakten: str,
    termine: list[str] | None = None, max_versuche: int = 2, max_tokens: int = 700,
) -> LLMErgebnis:
    """``termine``: Titel, die im Text vorkommen müssen (sonst Neuversuch/Fallback).

    ``max_tokens`` zählt bei Thinking-Aliasen auch die Denk-Tokens. Wird die Grenze
    erreicht (``finish_reason == "length"``), ist der Text abgeschnitten oder fehlt –
    das ist ein Fallback, kein Neuversuch (der liefe in dieselbe Grenze).
    """
    erg = LLMErgebnis(text=fakten, modell=modell)
    nachrichten = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": prompt_fuer(art, fakten)},
    ]
    t0 = time.monotonic()
    for versuch in range(1, max_versuche + 1):
        erg.versuche = versuch
        try:
            r = cli.chat.completions.create(
                model=modell, messages=nachrichten, temperature=0.3, max_tokens=max_tokens
            )
        except Exception as e:
            erg.fallback = f"LLM-Fehler: {type(e).__name__}: {str(e)[:200]}"
            break
        if r.usage:
            erg.prompt_tokens += r.usage.prompt_tokens or 0
            erg.completion_tokens += r.usage.completion_tokens or 0
        if getattr(r.choices[0], "finish_reason", None) == "length":
            erg.fallback = f"Antwort abgeschnitten (max_tokens={max_tokens})"
            break
        text = _THINK.sub("", r.choices[0].message.content or "").strip()
        if not text:
            erg.fallback = "LLM lieferte leeren Text"
            continue
        erg.verstoesse = pruefe_uhrzeiten(text, fakten)
        erg.fehlend = fehlende_termine(text, termine or [])
        if not erg.verstoesse and not erg.fehlend:
            erg.text, erg.fallback = text, None
            break
        gruende, korrektur = [], []
        if erg.verstoesse:
            gruende.append(f"erfundene Uhrzeiten: {', '.join(erg.verstoesse)}")
            korrektur.append(f"Die Uhrzeiten {', '.join(erg.verstoesse)} stehen nicht im "
                             "Faktenblock; verwende nur Uhrzeiten aus dem Faktenblock.")
        if erg.fehlend:
            gruende.append(f"fehlende Termine: {', '.join(erg.fehlend)}")
            korrektur.append(f"Es fehlen die Termine {', '.join(erg.fehlend)}; nenne alle Termine.")
        erg.fallback = "; ".join(gruende)
        nachrichten += [
            {"role": "assistant", "content": text},
            {"role": "user", "content": " ".join(korrektur) + " Schreib den Text neu."},
        ]
    erg.sekunden = time.monotonic() - t0
    if erg.fallback:
        erg.text = fakten
    return erg
