#!/usr/bin/env python3
"""Phase-1-Messung gegen Ollama: Ladezeit, Prompt-/Generierungs-Tokens/s, RAM,
plus zwei Qualitaetstests (Tageszusammenfassung, Tool-Calling mit 10 Sprachsaetzen).

Nur Standardbibliothek. Aufruf (python3 ist auf dem Host nicht installiert):
    nix run nixpkgs#python3 -- bench/ollama_bench.py [modell ...]

Ergebnisse: bench/results/<zeitstempel>/  (JSON + Klartext-Ausgaben je Modell)

Testdaten sind SYNTHETISCH. Der Plan verlangt 3 echte Tage - die kommen,
sobald die livesync-bridge (Phase 3) den Vault lesbar macht.
"""
import json
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

OLLAMA = "http://127.0.0.1:11434"
MODELS = sys.argv[1:] or [
    "qwen3.5:2b",
    "qwen3:4b-instruct",
    "qwen3.5:4b",
    "gemma4:e4b",
    "qwen3.5:9b",
]

# --- Testdaten: so wie planung.py sie spaeter aufbereitet liefern soll ------
FAKTEN_TAG = """Datum: Freitag, 13.11.2026 (KW 46)

Termine heute:
- 08:30-10:00 Vorlesung Datenbanksysteme (Raum B 101)
- 10:15-11:45 Software-Praktikum, Gruppentreffen (online)
- 14:00-14:30 Telefonat Küchenplanung Beispiel GmbH
- 18:00-20:00 Bogentraining (Halle)

Konflikte: keine
Freie Zeitfenster (mind. 60 min): 11:45-14:00, 14:30-18:00

Offene Aufgaben dieser Woche:
- [ ] Seminar-Folien Verteilte Systeme fertig machen (faellig: 2026-11-16)
- [ ] Literaturuebersicht Graphdatenbanken ergaenzen
- [ ] Geschenk fuer Lena & Tom besorgen (faellig: 2026-11-14)
- [ ] Pfeile beim Bogenladen bestellen

Ueberfaellig:
- [ ] Mail an Buergeramt wegen Ummeldung (faellig war: 2026-11-10)

Erledigt diese Woche: 5 von 11 Aufgaben
"""

SYSTEM_TAG = (
    "Du bist ein persoenlicher Assistent. Schreibe eine kurze Tageszusammenfassung "
    "auf Deutsch fuer eine Push-Nachricht (hoechstens 6 Saetze, kein Markdown). "
    "Nutze ausschliesslich die gegebenen Fakten, erfinde keine Termine oder Uhrzeiten. "
    "Nenne zuerst den naechsten Termin, dann Ueberfaelliges, dann einen konkreten "
    "Vorschlag, welche Aufgabe in welches freie Zeitfenster passt."
)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "aufgabe_hinzufuegen",
            "description": "Fuegt eine neue Aufgabe zur Wochenliste hinzu.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Die Aufgabe, kurz, ohne Datum."},
                    "wann": {
                        "type": "string",
                        "description": "Faelligkeit genau wie gesagt (z. B. 'Freitag', 'morgen', "
                        "'20. November'). Weglassen, wenn keine genannt wurde.",
                    },
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "aufgabe_erledigt",
            "description": "Markiert eine bestehende offene Aufgabe als erledigt.",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string", "description": "Die erledigte Aufgabe."}},
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tagesueberblick",
            "description": "Liest die Termine und Aufgaben von heute vor.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

SYSTEM_TOOLS = (
    "Du steuerst die Aufgabenliste des Nutzers per Sprache. Rufe immer genau ein "
    "passendes Werkzeug auf. Datumsangaben NICHT umrechnen, sondern woertlich in 'wann' "
    "uebernehmen."
)

# (satz, erwartetes_tool, schluesselwoerter_text, schluesselwort_wann | None)
SAETZE = [
    ("Füge Steuererklärung abgeben zu meiner Wochenliste hinzu", "aufgabe_hinzufuegen", ["steuer"], None),
    ("Erinner mich, bis Freitag die Steuer abzugeben", "aufgabe_hinzufuegen", ["steuer"], "freitag"),
    ("Ich muss morgen noch Pfeile beim Bogenladen bestellen", "aufgabe_hinzufuegen", ["pfeil"], "morgen"),
    ("Was steht heute an?", "tagesueberblick", [], None),
    ("Hake Wäsche waschen ab", "aufgabe_erledigt", ["wäsche|waesche"], None),
    ("Die Bewerbung bei Beispiel GmbH ist erledigt", "aufgabe_erledigt", ["bewerbung|beispiel"], None),
    ("Setz Zahnarzttermin vereinbaren auf die Liste, spätestens nächsten Mittwoch",
     "aufgabe_hinzufuegen", ["zahnarzt"], "mittwoch"),
    ("Was habe ich heute noch vor?", "tagesueberblick", [], None),
    ("Ich hab das Paket zur Post gebracht", "aufgabe_erledigt", ["paket"], None),
    ("Bis zum 20. November die Seminar-Folien fertig machen", "aufgabe_hinzufuegen", ["folie|seminar"], "20"),
]


def post(path, payload, timeout=900):
    req = urllib.request.Request(
        OLLAMA + path, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def unload_all():
    # Jedes Kandidatenmodell mit keep_alive 0 ansprechen = entladen
    for m in MODELS:
        try:
            post("/api/generate", {"model": m, "keep_alive": 0}, 60)
        except Exception:
            pass


def container_mem():
    out = subprocess.run(
        ["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}", "ollama"],
        capture_output=True, text=True,
    ).stdout.strip()
    return out.split("/")[0].strip()


def rates(r):
    ns = 1e9
    pe, pd = r.get("prompt_eval_count", 0), r.get("prompt_eval_duration", 1)
    ec, ed = r.get("eval_count", 0), r.get("eval_duration", 1)
    return {
        "load_s": round(r.get("load_duration", 0) / ns, 2),
        "prompt_tokens": pe,
        "prompt_tps": round(pe / (pd / ns), 1) if pd else None,
        "gen_tokens": ec,
        "gen_tps": round(ec / (ed / ns), 1) if ed else None,
        "total_s": round(r.get("total_duration", 0) / ns, 2),
    }


def erfundene_uhrzeiten(text):
    erlaubt = set(re.findall(r"\b\d{1,2}[:.]\d{2}\b", FAKTEN_TAG.replace(".", ":")))
    gefunden = set(re.findall(r"\b\d{1,2}[:.]\d{2}\b", text.replace(".", ":")))
    norm = lambda s: s.zfill(5)
    erlaubt = {norm(s) for s in erlaubt}
    return sorted(s for s in gefunden if norm(s) not in erlaubt)


def bewerte_tool(antwort, tool, keys, wann_key):
    calls = antwort.get("message", {}).get("tool_calls") or []
    if len(calls) != 1:
        return False, f"{len(calls)} Tool-Calls"
    fn = calls[0]["function"]
    args = fn.get("arguments") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return False, f"Argumente kein JSON: {args!r}"
    if fn["name"] != tool:
        return False, f"falsches Tool {fn['name']}"
    text = str(args.get("text", "")).lower()
    for k in keys:
        if not re.search(k, text):
            return False, f"text={args.get('text')!r}"
    wann = str(args.get("wann") or "").lower()
    if wann_key and wann_key not in wann:
        return False, f"wann={args.get('wann')!r}"
    if not wann_key and wann and tool == "aufgabe_hinzufuegen":
        return False, f"wann erfunden: {args.get('wann')!r}"
    return True, json.dumps(args, ensure_ascii=False)


def main():
    out = Path(__file__).parent / "results" / datetime.now().strftime("%Y%m%d-%H%M")
    out.mkdir(parents=True, exist_ok=True)
    gesamt = {"zeit": datetime.now().isoformat(), "loadavg": open("/proc/loadavg").read().split()[:3], "modelle": {}}

    for m in MODELS:
        print(f"== {m}", flush=True)
        unload_all()
        time.sleep(2)
        res = {}

        # 1) Kaltstart + Zusammenfassung
        r = post("/api/chat", {
            "model": m, "stream": False, "think": False,
            "messages": [{"role": "system", "content": SYSTEM_TAG}, {"role": "user", "content": FAKTEN_TAG}],
            "options": {"temperature": 0.3},
        })
        res["tag_kalt"] = rates(r)
        res["ram_geladen"] = container_mem()
        text = r["message"]["content"]
        res["tag_text"] = text
        res["tag_erfundene_uhrzeiten"] = erfundene_uhrzeiten(text)
        print(f"   Tag (kalt): {res['tag_kalt']}  RAM={res['ram_geladen']}", flush=True)

        # 2) Warm wiederholen (Modell schon geladen) -> reine Inferenzzeit
        r = post("/api/chat", {
            "model": m, "stream": False, "think": False,
            "messages": [{"role": "system", "content": SYSTEM_TAG}, {"role": "user", "content": FAKTEN_TAG}],
            "options": {"temperature": 0.3},
        })
        res["tag_warm"] = rates(r)
        res["tag_text_2"] = r["message"]["content"]
        print(f"   Tag (warm): {res['tag_warm']}", flush=True)

        # 3) Tool-Calling
        treffer, details = 0, []
        for satz, tool, keys, wann_key in SAETZE:
            t0 = time.time()
            try:
                r = post("/api/chat", {
                    "model": m, "stream": False, "think": False, "tools": TOOLS,
                    "messages": [{"role": "system", "content": SYSTEM_TOOLS}, {"role": "user", "content": satz}],
                    "options": {"temperature": 0},
                })
                ok, info = bewerte_tool(r, tool, keys, wann_key)
                lat = round(time.time() - t0, 2)
            except Exception as e:  # noqa: BLE001
                ok, info, lat, r = False, f"Fehler: {e}", None, {}
            treffer += ok
            details.append({"satz": satz, "ok": ok, "info": info, "latenz_s": lat,
                            "content": (r.get("message") or {}).get("content", "")[:200]})
            print(f"   {'OK ' if ok else 'XX '} {lat}s  {satz}  ->  {info}", flush=True)
        res["tools_treffer"] = f"{treffer}/{len(SAETZE)}"
        res["tools_latenz_median_s"] = sorted(d["latenz_s"] or 999 for d in details)[len(details) // 2]
        res["tools"] = details
        gesamt["modelle"][m] = res

        (out / f"{m.replace(':', '_')}.txt").write_text(
            f"# {m}\n\n## Tageszusammenfassung (Lauf 1)\n{res['tag_text']}\n\n"
            f"## Tageszusammenfassung (Lauf 2)\n{res['tag_text_2']}\n\n"
            f"Erfundene Uhrzeiten: {res['tag_erfundene_uhrzeiten'] or 'keine'}\n"
        )
        (out / "ergebnisse.json").write_text(json.dumps(gesamt, ensure_ascii=False, indent=2))

    unload_all()
    print(f"\nErgebnisse: {out}")


if __name__ == "__main__":
    main()
