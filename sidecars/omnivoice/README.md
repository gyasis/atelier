# omnivoice-sidecar — primary instruct-driven TTS

The hub's **primary** speech engine: [OmniVoice](https://github.com/k2-fsa/OmniVoice) (k2-fsa, Diffusion LM, PyTorch MPS). Port **8770**. Zero-shot multi-speaker with **plain-language style control** (`instruct`), voice cloning from a reference clip, and post-synth pitch shift.

## Use

```sh
# instruct-driven delivery
curl -s http://192.168.0.159:8770/tts -H 'content-type: application/json' \
  -d '{"text":"Welcome back.","instruct":"warm, slow, deep male"}' --output out.wav

# clone a voice from a 3–10s reference
curl -s http://192.168.0.159:8770/tts -H 'content-type: application/json' \
  -d '{"text":"...","ref_audio":"/abs/ref.wav","ref_text":"transcript of ref"}' --output out.wav
```

## Endpoints

| Endpoint | Purpose |
|---|---|
| `GET /healthz` · `GET /readyz` | liveness / warm-cold-busy + queue depth |
| `GET /agent` | machine-readable manifest for agents |
| `POST /tts` | see params below → `audio/wav` (24 kHz) |
| `POST /admin/unload[?force=true]` | drop the model to free memory |

**`/tts` params:** `text`, `language`, `instruct` (accent/tone/emotion), `ref_audio` + `ref_text` (cloning), `speed`, `num_step` (diffusion steps — quality vs speed), `guidance_scale`, `class_temperature` (prosodic variation), `pitch_semitones` (−12..+12, tempo-preserving).

Single-flight; idle-unloads after `IDLE_UNLOAD_SECONDS` (default 240). Auto-detects device (cuda/mps/cpu).

## Model & env

- `k2-fsa/OmniVoice` (auto-downloads via `HF_HOME` on first start).
- `OMNIVOICE_MODEL`, `OMNIVOICE_DEVICE`, `IDLE_UNLOAD_SECONDS`, `KEEP_WARM`, `HUB_TOKEN`.

> Fastest fixed voices → **kokoro** (`:8765`). Expressive `[S1]/[S2]` dialogue cloning → **dia** (`:8769`).
