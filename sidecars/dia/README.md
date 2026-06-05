# dia-sidecar (voice-clone) — expressive dialogue TTS

Multi-speaker dialogue TTS via [Dia 1.6B](https://github.com/nari-labs/dia) (PyTorch MPS). Port **8769**. Voice cloning is baked in: the server preloads **LEO** (`[S1]`) and **SARAH** (`[S2]`) reference clips so output locks onto the canonical voices.

~10× realtime — **batch/overnight use**, not live. (Retired from the live pipeline in favor of omnivoice/kokoro.)

## Use

```sh
curl -s http://192.168.0.159:8769/tts -H 'content-type: application/json' \
  -d '{"text":"[S1] Welcome back. [S2] Glad to be here."}' --output out.wav
```

Tag lines with `[S1]` (LEO) / `[S2]` (SARAH); untagged text defaults to `[S1]`.

## Endpoints

| Endpoint | Purpose |
|---|---|
| `GET /healthz` · `GET /readyz` | liveness / warm-cold-busy |
| `GET /agent` | machine-readable manifest for agents |
| `POST /tts` | `{text, use_voice_clone, max_new_tokens, guidance_scale, temperature, top_p, top_k}` → `audio/wav` (44.1 kHz) |
| `POST /admin/unload[?force=true]` | drop the model from MPS to free memory |

Single-flight; idle-unloads after `IDLE_UNLOAD_SECONDS` (default 180) — a long generation is never reaped mid-job.

## Model & env

- `nari-labs/Dia-1.6B-0626` (auto-downloads via `HF_HOME` on first run).
- Voice refs at `~/models/voice-refs/{leo_ref,sarah_ref}.wav` (+ transcripts).
- `DIA_MODEL_CHECKPOINT`, `DIA_DEVICE` (mps), `DIA_DTYPE` (float16), `DIA_LEO_REF_AUDIO/TEXT`, `DIA_SARAH_REF_AUDIO/TEXT`, `IDLE_UNLOAD_SECONDS`, `KEEP_WARM`, `HUB_TOKEN`.

> Fast fixed voices → **kokoro** (`:8765`). Instruct-driven/cloning, live → **omnivoice** (`:8770`).
