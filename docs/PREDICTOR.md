# Atelier Generation-Time Predictor

A modular, persistent, **per-model** ETA predictor living in the governor sidecar
(`sidecars/governor/predictor.py`, store at `~/.atelier/predictor.db`). Answers
"this call will take ~N seconds" for any TTS synth or LLM reply, local or cloud.

## Model

```
ETA ≈ fixed_costs(network RTT + cold-load) + core
core(tts) = input_chars × seconds_per_char
core(llm) = predicted_output_tokens ÷ decode_tokens_per_sec
```

Each rate/output value is a **Bayesian blend**: a per-class PRIOR shrunk toward the
EMPIRICAL median as runs accumulate — `(n·empirical + k·prior)/(n+k)`. Prior dominates
with few samples; real data takes over with use. Returns `eta_seconds` + `eta_p90_seconds`
(a band) + `human` ("~2.8 min (up to ~4.4 min)").

### Classes & priors
| class | rate | typical out | notes |
|---|---|---|---|
| `llm:thinking` (r1/qwq) | 12 tok/s | **2000** | long reasoning stream — the hard case |
| `llm:large` (27B–70B) | 8 tok/s | 400 | |
| `llm:standard` | 20 tok/s | 350 | |
| `llm:claude` (cloud) | 60 tok/s | 600 | RTT-dominated (+~400ms net) |
| `tts` | 0.13 s/char | — | num_step folded into model key (`omnivoice:ns64`) |

## Feature context (the rich predictor)
Every run records: `kind, model, location(local|cloud), host, device(mps|cuda|cpu),
state(warm|cold), net_latency_ms, queue_depth, in_units, out_units, seconds, rate`.
Fixed costs added per call: network RTT (cloud/Claude), cold-load (unloaded model).
*(Roadmap: auto-detect host/device; per-host rate buckets so a CPU host and a GPU host
learn separately; measure live net latency.)*

## The data is the asset — shareable compute-stats
The store is a **defined, portable dataset** (`schema: atelier-predictor-v1`), not a
black box. `GET /predictor/export` dumps it as JSON; another Atelier instance (or a
shared community base) re-imports via `POST /report`. So "gemma4 on M1 Max MPS = X tok/s"
becomes knowledge any server or local model can reuse — a growing mathematical base of
compute stats across the fleet.

## API (governor `:8799`)
- `GET /estimate?engine=omnivoice&chars=N[&num_step=48|64]` — TTS ETA
- `GET /estimate?model=<m>[&out_tokens=N][&location=local|cloud][&state=warm|cold]` — LLM ETA
- `POST /report {kind,model,seconds,in_units,out_units,location,host,device,state,...}` —
  feed a completed run; the predictor sharpens. **The gateway should POST its real Ollama
  generate stats here** (send `eval_count` as out_units and `eval_duration` as the decode
  basis — NOT total_duration — so tps is clean).
- `GET /predictor/stats` — learned summary per (kind, model)
- `GET /predictor/export` — full portable dataset

## Status / roadmap
- ✅ module + store + priors + Bayesian blend + `/estimate` + `/report` + export/stats
- ✅ live TTS synths auto-recorded (governor tails sidecar logs)
- ✅ gateway → `/report`: githubawesome `podcast.ts` reports its Gemini script-gen runs
  (cloud LLM) — verified: gemini-2.5-flash learned at ~90 tok/s, /estimate now empirical+prior
- ⏳ on-demand `/benchmark?model=X` (governor fires a tiny generate → seeds tps)
- ⏳ host/device auto-detection + per-host rate buckets (CPU vs GPU vs cloud)
- ⏳ live network-latency measurement for the cloud/Claude fixed cost
