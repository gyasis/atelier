# kokoro-sidecar — fast fixed-voice TTS

Server-side TTS via [kokoro-onnx](https://github.com/thewh1teagle/kokoro-onnx) (CoreML/MPS). Port **8765**. The hub's **fastest** speech engine (~sub-second per line) — the low-latency fallback when you don't need cloning or expressive prosody.

## Use

```sh
curl -s http://192.168.0.159:8765/voices                         # list voice ids
curl -s http://192.168.0.159:8765/tts -H 'content-type: application/json' \
  -d '{"text":"Hello there","voice":"af_bella","speed":1.0}' --output out.wav
```

## Endpoints

| Endpoint | Purpose |
|---|---|
| `GET /healthz` · `GET /readyz` | liveness / warm-cold-busy + queue depth |
| `GET /voices` | available voice ids + default |
| `GET /agent` | machine-readable manifest for agents |
| `POST /tts` | `{text, voice, speed, lang}` → `audio/wav` |
| `POST /admin/unload[?force=true]` | drop the model to free memory |

Single-flight (`asyncio.Semaphore(1)` — CoreML EP is single-stream). Defaults to `KEEP_WARM=true` (serves the live podcast pipeline); set `KEEP_WARM=false` to idle-unload.

## Model & env

- `Kokoro-82M-v1.0-ONNX` q8 (~325 MB) at `~/models/kokoro/` (`kokoro-v1.0.fp16.onnx` + `voices-v1.0.bin`).
- `KOKORO_MODEL_PATH`, `KOKORO_VOICES_PATH`, `KOKORO_DEFAULT_VOICE` (default `am_michael`), `IDLE_UNLOAD_SECONDS`, `KEEP_WARM`, `HUB_TOKEN`.

> Need expressive/cloned voices → **dia** (`:8769`). Need instruct-driven accents → **omnivoice** (`:8770`).
