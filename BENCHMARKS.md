# Benchmarks: local LLM inference on a low-power home server

Measured 12–14 November 2026 on the production box, with all other services
(Immich, Nextcloud, Home Assistant, a Minecraft server, …) still running.
Raw output is in [`bench/results/`](bench/results/), the scripts next to it in [`bench/`](bench/).

**TL;DR:** on a 35 W desktop CPU with no GPU, **Gemma 4 E4B via Ollama** is the
only model tested that is both fast enough for voice (≈3 s per tool call) and
error-free on the task set (10/10 tool calls). It generates twice as fast as a
plain llama.cpp run because Ollama uses the model's built-in speculative-decoding
draft head automatically.

## Hardware and software

| | |
|---|---|
| CPU | Intel Core i5-13400T (35 W), 6 P-cores (12 threads) + 4 E-cores, AVX2 + AVX-VNNI |
| GPU | Intel UHD 730 iGPU (only used for the Vulkan test) |
| RAM | 31 GB, ~15 GB free during the runs (Minecraft alone holds 9.4 GB) |
| Ollama | 0.40.16 in Docker, `OLLAMA_MAX_LOADED_MODELS=1`, context 4096 |
| llama.cpp | b12316 (`ghcr.io/ggml-org/llama.cpp:full-b12316`), CPU backend `alderlake` |
| Background load | Other containers kept running; load average ~0.4 before the runs |

All models in Q4_K_M except `qwen3.5:2b` (Q8_0, the default Ollama ships).
llama-bench measures **the exact same GGUF files** Ollama uses: the `model` layer
from Ollama's blob store, mounted read-only into the llama.cpp container.

Candidates: the small instruct models that were current in November 2026 and
fit the RAM budget: Qwen3-4B-Instruct-2507 (baseline), Qwen3.5 2B / 4B / 9B,
and Gemma 4 E4B (native tool calling).

## 1. Raw throughput (llama-bench, tokens/s, 3 repetitions)

`pp512` = ingest 512 prompt tokens, `tg128` = generate 128 tokens, `@N` = threads.

| Model | Size | pp512 @4 | pp512 @6 | pp512 @10 | pp512 @12 | pp2048 @6 | tg128 @6 |
|---|---|---|---|---|---|---|---|
| qwen3.5:2b (Q8_0) | 1.9 GB | 125 | **167** | 123 | 129 | 164 | 16.7 |
| qwen3:4b-instruct | 2.3 GB | 62 | 86 | 97 | 100 | 76 | 13.0 |
| qwen3.5:4b | 2.5 GB | 61 | 84 | 95 | 98 | 82 | 11.2 |
| gemma4:e4b (no draft) | 5.1 GB¹ | 100 | **131** | 145 | 148 | 125 | 10.1 |
| qwen3.5:9b | 5.2 GB | 34 | 46 | 51 | 54 | 45 | 6.3 |

¹ Gemma 4 E4B has 7.5 B parameters, most of them per-layer embeddings; only
~4 B are active per token, which is why its prompt ingestion beats the real 4 B models.

Findings:

- **Generation does not scale with threads.** At 4, 6, 10 and 12 threads `tg`
  stays within ±3 %. Memory bandwidth is the bottleneck, not compute.
- **Prompt ingestion gains only 10–15 % from 6 to 12 threads**, and the 2 B
  model gets *slower* above 6 threads because the E-cores drag it down.
  → 6 threads (the P-cores) is the sweet spot, which Ollama already picks by itself.
- **Pinning to the P-cores** (`-C 0x555 --cpu-strict 1`) changes nothing
  (85.1 vs 84.1 pp, 11.13 vs 11.15 tg). The kernel scheduler already places threads well.
- **iGPU via Vulkan** (qwen3.5:4b, all layers on the UHD 730): pp512 72, tg128 7.2,
  i.e. **slower than the CPU** (84 / 11.2). The only upside would be a free CPU. Dropped.

## 2. Ollama end-to-end (daily summary prompt, ~420 tokens)

| Model | Cold load | Prompt t/s | Gen t/s | Total (cold) | RAM loaded |
|---|---|---|---|---|---|
| qwen3.5:2b | 2.3 s | 167 | 16.9 | 10.5 s | 3.0 GB |
| qwen3:4b-instruct | 10.9 s² | 80 | 12.7 | 24.1 s | 3.3 GB |
| qwen3.5:4b | 5.6 s | 83 | 11.4 | 24.2 s | 3.6 GB |
| **gemma4:e4b** | 5.7 s | 132 | **19.7** | **14.1 s** | 6.7 GB |
| qwen3.5:9b | 6.4 s | 42 | 6.4 | 44.2 s | 6.7 GB |

² first load straight after download, from disk. The others were already in the page cache.

Ollama and llama-bench agree on the same GGUF, with one exception:
**gemma4:e4b generates twice as fast under Ollama.** See section 4.

## 3. Quality

Test data in [`bench/ollama_bench.py`](bench/ollama_bench.py) is synthetic: an
invented Friday with 4 appointments, 4 open and 1 overdue task, and 10 German
voice commands for a to-do list. Names, courses and other personal details in the
test data and in the stored model outputs were replaced with fictional ones after the
run (same grammatical gender, so the outputs read as generated); metrics are unaffected.

### Tool calling (10 German utterances, `think: false`, temperature 0)

Tools: `aufgabe_hinzufuegen` (add task, optional due date), `aufgabe_erledigt`
(mark done), `tagesuebersicht` (what's on today). A hit requires the right tool
*and* the right arguments.

| Model | Hits | Median latency | Errors |
|---|---|---|---|
| qwen3.5:2b | 8/10 | 5.1 s | "next Wednesday" → `tomorrow`; "brought the parcel to the post office" → *add* instead of *done* |
| qwen3:4b-instruct | 7/10 | 3.3 s | invents `due: tomorrow`; "tick off …" → *add*; once no tool call at all |
| qwen3.5:4b | 9/10 | 9.6 s | "tick off laundry" → *add* |
| **gemma4:e4b** | **10/10** | **3.0 s** | – |
| qwen3.5:9b | 10/10 | 17.0 s | – |

The Qwen3.5 latencies are surprisingly high (4 B ≈ 10 s per utterance). The
tool template is large and apparently not served from the prompt cache. Too slow
for a voice assistant either way.

### Daily summary (read and graded by hand)

| Model | Verdict |
|---|---|
| qwen3.5:2b | Cuts sentences off, mixes up appointments. Unusable |
| qwen3:4b-instruct | Short and correct, informal "du", but only 3 sentences |
| qwen3.5:4b | Correct, but formal "Sie" and > 6 sentences, too long for a push notification |
| **gemma4:e4b** | Short, correct, informal, concrete suggestions per free time slot |
| qwen3.5:9b | Most detailed, but contains a logic error and is 3× slower |

**Invented times:** none from any model (the only hit, from the 2 B model, was the
date "10.11." being read as a time, which the checker now distinguishes).

**Most important finding for the design:** every model named a *different* "next
appointment", because the facts contained no current time. That led to the core
rule of the assistant: **Python computes, the LLM only phrases.** Everything that
can be calculated (next appointment, conflicts, free slots, overdue tasks) is
computed in Python and handed over as a fact block, and the LLM output is checked
against it (see [`assistent/`](assistent/)).

## 4. Ollama vs. llama-server (gemma4:e4b, same request, temp 0, 200 tokens)

| Engine | Gen t/s | Prompt t/s | RAM (docker stats) |
|---|---|---|---|
| llama-server, no draft | 9.9 | 75 | 2.4 GB |
| llama-server, `-md gemma4-assistant --spec-type draft-mtp` | **19.5** | 75 | 2.1 GB |
| Ollama 0.40.16 | 19.3 | 131 | 6.7 GB |

- **Ollama uses speculative decoding for Gemma 4 automatically.** The model
  manifest ships a `draft` layer (98 MB, architecture `gemma4-assistant`,
  4 blocks, MTP-style). Acceptance rate 58 %, 2.73 tokens per step on average,
  so generation **doubles**. llama-server with the same draft reaches the same speed.
- Ollama 0.40 runs on llama.cpp's server code (visible in its logs as
  `srv load_model`, `common_speculative_init`); the engines differ mostly in defaults.
- **RAM is not directly comparable:** llama-server `mmap`s the GGUF and that page
  cache barely shows in `docker stats`; Ollama also loads the vision/audio projector (~1 GB).
- Open: why llama-server ingests prompts at only 75 t/s when Ollama and llama-bench
  reach 131 with 6 threads (suspects: `--threads-batch`, batch sizes, SWA checkpoints).

## 5. Thinking for the weekly report

Real fact block (review of the past week + outlook, ~575 prompt tokens), temperature 0.3, through LiteLLM.

| Alias | Model | Duration | Tokens (incl. thinking) | "Carry over" advice | Errors |
|---|---|---|---|---|---|
| `assistent` | gemma4:e4b, `think: false` | 24 s | 269 | missing | mentioned "suggestions" that did not exist |
| `assistent-gruendlich` | gemma4:e4b, `think: true` | 58–67 s | 1176–1412 | yes | none |
| – | qwen3.5:9b, `think: true` | 578 s / > 600 s timeout | 3529 | – | – |

- Thinking arrives separately as `reasoning_content` through LiteLLM, not as `<think>` in the text,
  so the output validators only ever see the final answer.
- **Ollama finishes a request even after the client has disconnected.** After the 9 B
  timeout Ollama stayed busy for ~15 min; a gemma4 report queued behind it hit its own
  timeout. With `MAX_LOADED_MODELS=1` and one slot, **one slow model blocks every
  alias, including the voice agent**. This is why there is no alias for the 9 B model.
- Decision: weekly report (a batch job, Sunday 19:00) uses thinking with
  `max_tokens` 2500 (thinking counts against Ollama's 4096 context); everything else stays without.

## 6. In production

From LiteLLM's request log (`LiteLLM_SpendLogs`), first two days of operation (13–14 Nov 2026), CPU only:

| Alias | Use | Requests | Median | Max | Avg prompt / completion tokens |
|---|---|---|---|---|---|
| `assistent` | daily summary, calendar suggestions | 29 | 7.6 s | 24 s | 330 / 90 |
| `assistent-gruendlich` | weekly report with thinking | 3 | 62 s | 67 s | 553 / 1264 |
| `ha-agent` | voice smoke test | 2 | 1.1 s | 2 s | 57 / 16 |

Query to reproduce:

```sql
select model_group, count(*),
       percentile_cont(0.5) within group (order by extract(epoch from "endTime"-"startTime")) as p50_s
from "LiteLLM_SpendLogs" where status = 'success' group by 1;
```

## 7. Keeping the model resident (22 Nov 2026)

One model behind all aliases did **not** mean no reloads. Home Assistant's Ollama
integration sends `num_ctx=8192`, LiteLLM requests ran at the server default 4096, and
Ollama starts a separate runner per context size. With `MAX_LOADED_MODELS=1` every switch
between voice and a report evicted the other, and `keep_alive=5m` unloaded it on top.
The voice log showed 3 of 15 commands at ~27 s instead of ~1 s.

Most of that time is not loading. HA's system prompt plus tool definitions is ~2,600
tokens; on a cold cache it has to be evaluated in full, on a warm one Ollama reuses the
cached prefix.

Fix: `OLLAMA_CONTEXT_LENGTH=8192` (same as HA), `OLLAMA_KEEP_ALIVE=-1`,
`OLLAMA_NUM_PARALLEL=2`. The second parallel slot is the important part: with one slot a
report overwrites the voice prefix in the KV cache, with two Ollama picks the slot with the
longest matching prefix.

Replaying a real HA voice request (2,585 prompt tokens) directly against Ollama:

| Step | Total | Load | Prompt eval |
|---|---|---|---|
| voice, cold (after container restart) | 43 s | 7.0 s | 34.9 s |
| voice, warm | 1.2 s | 0 s | 0.12 s |
| daily report | 11.6 s | | |
| voice right after | **1.2 s** | 0 s | 0.12 s |
| weekly report (thinking) | 112 s ¹ | | |
| voice right after | **1.2 s** | 0 s | 0.13 s |

¹ 79 s thinking run plus a 31 s re-phrase with `assistent`, because validation caught a
calendar entry missing from the thinking output. Unrelated to this change; the thinking
run alone matches 15 Nov (80 s).

`ollama ps` reports 2.1 GB for the loaded model; the container's cgroup shows 7.4 of
10 GiB including page cache of the mmapped weights.

## Decisions that came out of this

- **One model (`gemma4:e4b`) behind all aliases**, kept loaded permanently. That
  only avoids reloads if every client uses the same context size, see section 7.
- **6 threads.** More buys nothing for generation and steals CPU from other services.
- **RAM is the price:** 6.7 GB instead of 3.6 GB. Since 22 Nov it stays loaded
  (`keep_alive=-1`); alongside Minecraft (9.4 GB) ~11 GB remain available.
- **The 2 B model is not a useful lower bound** for voice: Gemma 4 E4B is faster *and* better.
  `gemma4:e2b` would be the remaining candidate (not measured).

## Not measured yet

- Quality on **real** days instead of synthetic data at scale (spot checks on real
  weekly notes: 4.5–15 s per report, nothing invented after three prompt fixes).
- True memory footprint (RSS vs. page cache) per engine.
- `gemma4:e2b` as a lighter voice model.
- Full voice round trip (wake word → STT → LLM tool call → TTS) in Home Assistant.
- Local vs. cloud comparison (the `assistent vergleich` command exists; cloud is disabled by choice).
