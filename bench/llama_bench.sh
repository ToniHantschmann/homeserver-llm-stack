#!/usr/bin/env bash
# Phase 1: llama-bench gegen DIESELBEN GGUF-Dateien, die Ollama benutzt
# (die "model"-Layer aus Ollamas Blob-Store, read-only gemountet).
#
#   bench/llama_bench.sh            # alle Modelle
#   bench/llama_bench.sh vulkan     # nur iGPU-Test (Vulkan) mit qwen3.5:4b
#
# Ergebnisse als Markdown nach bench/results/llama-bench-<zeit>.md
# Ollama sollte waehrenddessen kein Modell geladen haben (sonst RAM/CPU-Konkurrenz).
set -euo pipefail

IMG=ghcr.io/ggml-org/llama.cpp:full-b12316
IMG_VK=ghcr.io/ggml-org/llama.cpp:full-vulkan-b12316
MAN=/mnt/data/llm/ollama/models/manifests/registry.ollama.ai/library
OUT="$(dirname "$0")/results/llama-bench-$(date +%Y%m%d-%H%M).md"
mkdir -p "$(dirname "$OUT")"
JQ=(nix run nixpkgs#jq --)

blob() {  # ollama-name -> blob-dateiname des Textmodells
  local f="$MAN/${1/://}"
  "${JQ[@]}" -r '.layers[] | select(.mediaType=="application/vnd.ollama.image.model") | .digest | sub(":";"-")' "$f"
}

run() {  # image extra-docker-args -- llama-bench-args
  local img=$1; shift
  docker run --rm --name llama-bench -v /mnt/data/llm/ollama/models/blobs:/models:ro "$@"
}

{
  echo "# llama-bench $(date -Iseconds)"
  echo
  echo "Image: \`$IMG\`, Host-Last vorher: $(cut -d' ' -f1-3 /proc/loadavg)"
  echo
} >> "$OUT"

if [[ "${1:-}" == "vulkan" ]]; then
  b=$(blob qwen3.5:4b)
  echo "## qwen3.5:4b - Vulkan (Intel UHD 730), alle Layer auf der iGPU" >> "$OUT"
  run "$IMG_VK" --device /dev/dri "$IMG_VK" --bench -m "/models/$b" -ngl 99 -t 6 -p 512 -n 128 -r 3 -o md >> "$OUT"
  echo "Fertig: $OUT"; exit 0
fi

for m in qwen3.5:2b qwen3:4b-instruct qwen3.5:4b gemma4:e4b qwen3.5:9b; do
  b=$(blob "$m")
  echo "== $m ($b)"
  {
    echo "## $m"
    echo
    echo "Thread-Sweep (pp512 = 512 Prompt-Tokens einlesen, tg128 = 128 Tokens erzeugen; t/s):"
    echo
  } >> "$OUT"
  run "$IMG" "$IMG" --bench -m "/models/$b" -t 4,6,10,12 -p 512 -n 128 -r 3 -o md >> "$OUT"
  {
    echo
    echo "Langer Prompt (pp2048, wie HA-Agent mit Entitaetenliste), 6 Threads:"
    echo
  } >> "$OUT"
  run "$IMG" "$IMG" --bench -m "/models/$b" -t 6 -p 2048 -n 0 -r 2 -o md >> "$OUT"
  echo >> "$OUT"
done

# Pinning-Vergleich: 6 Threads frei vs. fest auf je einen Hyperthread der 6 P-Kerne
# (CPU 0,2,4,6,8,10 -> Maske 0x555; E-Kerne sind CPU 12-15).
b=$(blob qwen3.5:4b)
{
  echo "## qwen3.5:4b - Pinning auf P-Kerne (6 Threads, Maske 0x555, strict)"
  echo
} >> "$OUT"
run "$IMG" "$IMG" --bench -m "/models/$b" -t 6 -C 0x555 --cpu-strict 1 -p 512 -n 128 -r 3 -o md >> "$OUT"

echo "Fertig: $OUT"
