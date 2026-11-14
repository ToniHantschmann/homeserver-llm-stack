#!/usr/bin/env python3
"""Gleicher Request (Tageszusammenfassung aus ollama_bench.py) gegen einen
OpenAI-kompatiblen Endpunkt, N Laeufe, gibt Prompt-/Generierungs-t/s aus.

    nix run nixpkgs#python3 -- bench/server_vs_ollama.py <base_url> <modell> [laeufe] [label]

llama-server liefert 'timings' (inkl. Draft-Akzeptanz), Ollama nur 'usage' -
fuer Ollama daher zusaetzlich /api/chat mit seinen Nanosekunden-Zaehlern.
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from ollama_bench import FAKTEN_TAG, SYSTEM_TAG  # noqa: E402

base, model = sys.argv[1], sys.argv[2]
runs = int(sys.argv[3]) if len(sys.argv) > 3 else 3
label = sys.argv[4] if len(sys.argv) > 4 else base


def post(url, payload):
    req = urllib.request.Request(url, json.dumps(payload).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=900) as r:
        return json.loads(r.read())


msgs = [{"role": "system", "content": SYSTEM_TAG}, {"role": "user", "content": FAKTEN_TAG}]
rows = []
for i in range(runs):
    t0 = time.time()
    if "11434" in base:
        r = post(base + "/api/chat", {"model": model, "messages": msgs, "stream": False, "think": False,
                                      "options": {"temperature": 0, "num_predict": 200}})
        pt = r["prompt_eval_count"] / (r["prompt_eval_duration"] / 1e9) if r.get("prompt_eval_duration") else None
        gt = r["eval_count"] / (r["eval_duration"] / 1e9)
        extra = ""
    else:
        r = post(base + "/v1/chat/completions", {"model": model, "messages": msgs, "temperature": 0,
                                                  "max_tokens": 200, "cache_prompt": False})
        t = r.get("timings", {})
        pt, gt = t.get("prompt_per_second"), t.get("predicted_per_second")
        extra = f"draft={t.get('draft_n', '-')}/{t.get('draft_n_accepted', '-')}" if "draft_n" in t else ""
    wall = time.time() - t0
    rows.append((pt, gt, wall))
    print(f"{label}  lauf {i+1}: prompt {pt and round(pt,1)} t/s  gen {round(gt,1)} t/s  wall {wall:.1f}s {extra}", flush=True)

gs = sorted(r[1] for r in rows)
print(f"{label}  MEDIAN gen {gs[len(gs)//2]:.1f} t/s")
