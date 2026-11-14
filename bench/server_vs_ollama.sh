#!/usr/bin/env bash
# Phase 1: llama-server vs. Ollama, gleiches GGUF (gemma4:e4b aus Ollamas Blob-Store),
# einmal ohne und einmal mit dem Draft-Modell (gemma4-assistant), das Ollama
# automatisch fuer spekulatives Decoding nutzt.
#   bench/server_vs_ollama.sh  > bench/results/server-vs-ollama-<zeit>.txt
set -euo pipefail
cd "$(dirname "$0")/.."
IMG=ghcr.io/ggml-org/llama.cpp:full-b12316
M=sha256-370c2879f17648d337b0ab4e208fdce348e24d1060064e93d7645daafd56d39c
D=sha256-05612e54110f8f68bade01dfcc546cdc6914d11dde0b650a65f263f0f87bbbd2
PY=(nix run nixpkgs#python3 --)

stop() { docker rm -f llama-server-bench >/dev/null 2>&1 || true; }
trap stop EXIT

start() {  # extra llama-server args
  stop
  docker run -d --name llama-server-bench -p 127.0.0.1:8090:8080 \
    -v /mnt/data/llm/ollama/models/blobs:/models:ro "$IMG" \
    --server -m "/models/$M" -t 6 -c 4096 --host 0.0.0.0 --port 8080 "$@" >/dev/null
  for _ in $(seq 120); do curl -fs 127.0.0.1:8090/health >/dev/null 2>&1 && return 0; sleep 1; done
  echo "llama-server startet nicht:"; docker logs --tail 30 llama-server-bench; return 1
}

# Ollama entladen, damit nur eine Engine RAM/CPU belegt
curl -s 127.0.0.1:11434/api/generate -d '{"model":"gemma4:e4b","keep_alive":0}' >/dev/null

echo "== llama-server ohne Draft"
start
"${PY[@]}" bench/server_vs_ollama.py http://127.0.0.1:8090 gemma4 3 "llama-server"
docker stats --no-stream --format 'RAM llama-server: {{.MemUsage}}' llama-server-bench

echo "== llama-server mit Draft (gemma4-assistant, MTP)"
if start -md "/models/$D" --spec-type draft-mtp; then
  "${PY[@]}" bench/server_vs_ollama.py http://127.0.0.1:8090 gemma4 3 "llama-server+draft"
  docker stats --no-stream --format 'RAM llama-server+draft: {{.MemUsage}}' llama-server-bench
  docker logs llama-server-bench 2>&1 | grep -i -E 'draft acceptance|accept' | tail -3 || true
fi
stop

echo "== Ollama (nutzt Draft automatisch)"
"${PY[@]}" bench/server_vs_ollama.py http://127.0.0.1:11434 gemma4:e4b 3 "ollama"
docker stats --no-stream --format 'RAM ollama: {{.MemUsage}}' ollama
curl -s 127.0.0.1:11434/api/generate -d '{"model":"gemma4:e4b","keep_alive":0}' >/dev/null
