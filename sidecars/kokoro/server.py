"""
kokoro-sidecar — server-side TTS for the Mac Studio inference hub.

Replaces in-browser kokoro-js (which froze the page on WASM and downloaded
~325MB to every visitor). GOVERNED via the shared lifecycle framework
(sidecars/_common/lifecycle.py) — it inherits the 3-law constitution
automatically: no memory leaks, never unload while working, and a request
queue (single-flight — CoreML/ONNX inference is single-stream and
serializing avoids contention). This file holds ONLY Kokoro's model +
endpoints; all lifecycle, memory, busy-guard, queue, /healthz, /readyz,
/admin/unload come from GovernedSidecar.

Endpoints:
    GET  /healthz       liveness (from the framework)
    GET  /readyz        rich lifecycle/observability state (from the framework)
    GET  /voices        list available voice IDs
    POST /tts           text -> audio/wav
    POST /admin/unload  free the model now (from the framework)
    GET  /agent         self-describing manifest for AI agents

Env vars:
    KOKORO_MODEL_PATH     path to kokoro-v1.0.fp16.onnx
    KOKORO_VOICES_PATH    path to voices-v1.0.bin
    KOKORO_DEFAULT_VOICE  default voice id (default am_michael)
    HUB_TOKEN             bearer token; if set, /tts /voices /agent require Authorization header
    IDLE_UNLOAD_SECONDS   idle-unload window (default 300 — kokoro's own default, not the
                          framework's 600, to match its prior behavior)
    KEEP_WARM             kokoro defaults this to true (unlike other sidecars) — it serves
                          the live podcast pipeline and every cold-load adds ~2s latency the
                          user feels per chapter. Set KEEP_WARM=false to opt into memory-saving
                          idle-unloads (overnight / multi-model contention scenarios).
    KOKORO_MAX_CONCURRENCY  override the single-flight queue width (framework default 1)
"""
import io
import os
import time

import numpy as np
import soundfile as sf
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from kokoro_onnx import Kokoro

from lifecycle import GovernedSidecar

MODEL_PATH = os.environ.get(
    "KOKORO_MODEL_PATH", "/Users/gyasisutton/models/kokoro/kokoro-v1.0.fp16.onnx"
)
VOICES_PATH = os.environ.get(
    "KOKORO_VOICES_PATH", "/Users/gyasisutton/models/kokoro/voices-v1.0.bin"
)
DEFAULT_VOICE = os.environ.get("KOKORO_DEFAULT_VOICE", "am_michael")

# Kokoro's own default is KEEP_WARM=true (unlike the framework's false default) — preserve
# that by resolving the env var here (with a "true" default) and passing it explicitly.
_KEEP_WARM_DEFAULT = os.environ.get("KEEP_WARM", "true").lower() in ("1", "true", "yes")

# Populated once by _load(); survives idle-unload/restart-of-model (kokoro's original
# behavior never cleared the voice list just because the model was unloaded).
_voices_cache: list[str] = []


def _load():
    """Cold-load Kokoro (ONNX + CoreML EP) + warm it up with a throwaway line. Returns the
    handle the framework holds; dropping it (on unload) releases the ref so gc can reclaim.
    kokoro-onnx runs on ONNX Runtime (not torch/MPS), so device is labeled 'coreml' — no
    special MPS/MLX cache-clear or restart-reclaim applies (matches the prior gc.collect()
    -only unload)."""
    global _voices_cache
    print(f"[kokoro] loading {os.path.basename(MODEL_PATH)}", flush=True)
    kokoro = Kokoro(MODEL_PATH, VOICES_PATH)
    try:
        _voices_cache = sorted(list(kokoro.get_voices()))
    except Exception:
        _voices_cache = _voices_cache or []
    try:
        kokoro.create("hello", voice=DEFAULT_VOICE, speed=1.0, lang="en-us")
    except Exception as e:
        print(f"[kokoro] warmup failed: {e}", flush=True)
    return kokoro


sc = GovernedSidecar(
    "kokoro",
    role="TTS — fast, fixed-voice speech synthesis (low-latency fallback)",
    load_fn=_load,
    model_name=os.path.basename(MODEL_PATH),
    device="coreml",
    idle_unload_s=int(os.environ.get("IDLE_UNLOAD_SECONDS", "300")),
    keep_warm=_KEEP_WARM_DEFAULT,
    # single-flight on the ONNX/CoreML runtime; framework reads KOKORO_MAX_CONCURRENCY to override
)

app = FastAPI(lifespan=sc.lifespan())
sc.attach(app)   # GET /healthz /readyz + POST /admin/unload


@app.get("/voices")
def voices(request: Request):
    sc.check_auth(request)
    return {"voices": _voices_cache, "default": DEFAULT_VOICE}


@app.get("/agent")
def agent(request: Request):
    """Self-describing manifest for AI agents — methods, params, how-to."""
    sc.check_auth(request)
    auth = ("send header `Authorization: Bearer <HUB_TOKEN>` on every request"
            if sc.hub_token else "none required (HUB_TOKEN not set)")
    return {
        "service": "kokoro",
        "role": sc.role,
        "summary": "Turn text into speech with a preset voice. Fastest engine on "
                   "the hub (~sub-second per line) — use it when you want quick "
                   "audio and don't need cloning or expressive prosody.",
        "auth": auth,
        "voices": {"list_route": "GET /voices", "default": DEFAULT_VOICE, "count": len(_voices_cache)},
        "methods": [
            {"name": "tts", "http": "POST /tts", "encoding": "application/json",
             "params": {"text": "1–5000 chars", "voice": f"voice id (default {DEFAULT_VOICE}); GET /voices",
                        "speed": "0.5–2.0", "lang": "e.g. en-us"},
             "returns": "audio/wav (PCM16); header x-synth-seconds",
             "example": "curl -s $URL/tts -H 'content-type: application/json' "
                        "-d '{\"text\":\"Hello there\",\"voice\":\"af_bella\"}' --output out.wav"},
            {"name": "voices", "http": "GET /voices", "returns": "{voices[], default}"},
            {"name": "readyz", "http": "GET /readyz", "returns": "warm/cold/busy, queue depth"},
        ],
        "recipes": [
            {"goal": "Quick spoken line", "do": "POST /tts {text}"},
            {"goal": "Pick a specific voice", "do": "GET /voices, then POST /tts {text, voice}"},
        ],
        "instructions": (
            "1) GET /voices to see available voice ids.\n"
            "2) POST /tts {text, voice, speed, lang}; audio/wav comes back inline.\n"
            "3) Need cloning/expressive voices? use the dia sidecar. Need instruct-"
            "driven accents? use omnivoice."
        ),
        "openapi": "/openapi.json",
    }


class TtsReq(BaseModel):
    text: str = Field(..., min_length=1, max_length=5000)
    voice: str = DEFAULT_VOICE
    speed: float = Field(1.0, ge=0.5, le=2.0)
    lang: str = "en-us"


@app.post("/tts")
async def tts(req: TtsReq, request: Request):
    sc.check_auth(request)
    if not req.text.strip():
        raise HTTPException(400, "empty text")
    t0 = time.perf_counter()
    async with sc.job() as kokoro:              # queue slot + busy stamp + lazy load
        try:
            samples, sample_rate = await sc.run(
                kokoro.create, req.text, voice=req.voice, speed=req.speed, lang=req.lang
            )
        except Exception as e:
            raise HTTPException(500, f"synthesis failed: {e}")
    elapsed = time.perf_counter() - t0
    print(f"[tts] voice={req.voice} chars={len(req.text)} {elapsed:.2f}s", flush=True)
    buf = io.BytesIO()
    sf.write(buf, np.asarray(samples), sample_rate, format="WAV", subtype="PCM_16")
    buf.seek(0)
    return Response(
        content=buf.read(),
        media_type="audio/wav",
        headers={"x-synth-seconds": f"{elapsed:.3f}", "x-voice": req.voice},
    )
