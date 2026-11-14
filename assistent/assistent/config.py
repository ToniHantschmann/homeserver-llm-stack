"""Konfiguration aus Umgebungsvariablen.

Alles, was zwischen Testlauf (dev, localhost) und Container (Phase 5) wechselt,
kommt aus der Umgebung. Geheimnisse (LiteLLM-Key, iCal-URL) nie ins Repo.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

STANDARD_WEEKLY_MUSTER = "03 - notes/Wochenplanung KW {kw:02d}.md"


@dataclass
class Config:
    vault: Path
    weekly_muster: str = STANDARD_WEEKLY_MUSTER
    # Kommagetrennt: https-URLs (private iCal-Adresse) oder lokale .ics-Pfade
    ical_quellen: list[str] = field(default_factory=list)
    llm_base_url: str = "http://127.0.0.1:4000/v1"
    llm_key: str = ""
    modell: str = "assistent"
    # Wochenbericht (Batch, So 19:00) darf ein gründlicheres Alias nutzen, z. B.
    # "assistent-gruendlich" (Thinking). Leer = wie ``modell``. Denk-Tokens zählen
    # gegen max_tokens, deshalb eigene Grenze und eigener Timeout.
    modell_woche: str = ""
    max_tokens_woche: int = 700
    llm_timeout_woche: float = 180.0
    zeitzone: ZoneInfo = field(default_factory=lambda: ZoneInfo("Europe/Berlin"))
    # Fenster für "freie Zeit" pro Tag (volle Stunden)
    tag_beginn: int = 8
    tag_ende: int = 21
    # Bearer-Token für die HTTP-API (Phase 5); ohne Token startet ``assistent api`` nicht
    api_token: str = ""
    # Verzeichnis außerhalb des Vaults für den Vorschlags-Zustand (im Container ein
    # Docker-Volume). Leer = nichts merken (Host/Tests): gelöschte Vorschläge kämen wieder.
    zustand: Path | None = None
    # Vorschläge aus dem Kalender formulieren lassen; "0" = nur feste Vorlagentexte
    vorschlaege_llm: bool = True

    @classmethod
    def aus_umgebung(cls) -> "Config":
        env = os.environ
        quellen = [q.strip() for q in env.get("ASSISTENT_ICAL", "").split(",") if q.strip()]
        return cls(
            vault=Path(env.get("ASSISTENT_VAULT", "/mnt/data/llm/vault")),
            weekly_muster=env.get("ASSISTENT_WEEKLY_MUSTER", STANDARD_WEEKLY_MUSTER),
            ical_quellen=quellen,
            llm_base_url=env.get("LITELLM_BASE_URL", "http://127.0.0.1:4000/v1"),
            llm_key=env.get("LITELLM_API_KEY", ""),
            modell=env.get("ASSISTENT_MODELL", "assistent"),
            modell_woche=env.get("ASSISTENT_MODELL_WOCHE", ""),
            max_tokens_woche=int(env.get("ASSISTENT_MAX_TOKENS_WOCHE", "700")),
            llm_timeout_woche=float(env.get("ASSISTENT_LLM_TIMEOUT_WOCHE", "180")),
            zeitzone=ZoneInfo(env.get("ASSISTENT_TZ", "Europe/Berlin")),
            tag_beginn=int(env.get("ASSISTENT_TAG_BEGINN", "8")),
            tag_ende=int(env.get("ASSISTENT_TAG_ENDE", "21")),
            api_token=env.get("ASSISTENT_API_TOKEN", ""),
            zustand=Path(env["ASSISTENT_ZUSTAND"]) if env.get("ASSISTENT_ZUSTAND") else None,
            vorschlaege_llm=env.get("ASSISTENT_VORSCHLAEGE_LLM", "1") != "0",
        )
