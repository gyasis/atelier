"""
kokoro-sidecar — server-side TTS for the Mac Studio inference hub.

Replaces in-browser kokoro-js (which froze the page on WASM and downloaded
~325MB to every visitor). Single-flight via asyncio.Semaphore — CoreML/MPS
inference is single-stream and serializing avoids contention.

Endpoints:
    GET  /healthz       liveness, returns immediately
    GET  /readyz        503 until model is warm, 200 once warmed
    GET  /voices        list available voice IDs
    POST /tts           text -> audio/wav

Env vars:
    KOKORO_MODEL_PATH   path to kokoro-v1.0.fp16.onnx
    KOKORO_VOICES_PATH  path to voices-v1.0.bin
    HUB_TOKEN           bearer token; if set, /tts requires Authorization header
"""

import asyncio
import io
import os
import time
from contextlib import asynccontextmanager

import numpy as np
import soundfile as sf
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from kokoro_onnx import Kokoro

MODEL_PATH = os.environ.get(
    "KOKORO_MODEL_PATH", "/Users/gyasisutton/models/kokoro/kokoro-v1.0.fp16.onnx"
)
VOICES_PATH = os.environ.get(
    "KOKORO_VOICES_PATH", "/Users/gyasisutton/models/kokoro/voices-v1.0.bin"
)
HUB_TOKEN = os.environ.get("HUB_TOKEN")
DEFAULT_VOICE = os.environ.get("KOKORO_DEFAULT_VOICE", "am_michael")

_kokoro: Kokoro | None = None
_warmed: bool = False
_sem = asyncio.Semaphore(1)
_voices: list[str] = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _kokoro, _warmed, _voices
    t0 = time.perf_counter()
    _kokoro = Kokoro(MODEL_PATH, VOICES_PATH)
    try:
        _voices = sorted(list(_kokoro.get_voices()))
    except Exception:
        _voices = []
    print(f"[kokoro] model loaded in {time.perf_counter()-t0:.2f}s, voices={len(_voices)}")
    try:
        async with _sem:
            await asyncio.to_thread(
                _kokoro.create, "hello", voice=DEFAULT_VOICE, speed=1.0, lang="en-us"
            )
        _warmed = True
        print(f"[kokoro] warmup OK in {time.perf_counter()-t0:.2f}s")
    except Exception as e:
        print(f"[kokoro] warmup failed: {e}")
    yield


app = FastAPI(lifespan=lifespan)


def _check_auth(request: Request) -> None:
    if not HUB_TOKEN:
        return
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer ") or auth[7:] != HUB_TOKEN:
        raise HTTPException(401, "invalid bearer token")


@app.get("/healthz")
def healthz():
    return {"ok": True, "service": "kokoro", "version": "1.0"}


@app.get("/readyz")
def readyz():
    if not _warmed:
        return JSONResponse({"ok": False, "warmed": False}, status_code=503)
    return {
        "ok": True,
        "models_loaded": [os.path.basename(MODEL_PATH)],
        "device": "coreml/mps",
        "voices": len(_voices),
    }


@app.get("/voices")
def voices(request: Request):
    _check_auth(request)
    return {"voices": _voices, "default": DEFAULT_VOICE}


class TtsReq(BaseModel):
    text: str = Field(..., min_length=1, max_length=5000)
    voice: str = DEFAULT_VOICE
    speed: float = Field(1.0, ge=0.5, le=2.0)
    lang: str = "en-us"


@app.post("/tts")
async def tts(req: TtsReq, request: Request):
    _check_auth(request)
    if not _warmed:
        raise HTTPException(503, "warming up")
    if not req.text.strip():
        raise HTTPException(400, "empty text")
    t0 = time.perf_counter()
    async with _sem:
        try:
            samples, sample_rate = await asyncio.to_thread(
                _kokoro.create, req.text, voice=req.voice, speed=req.speed, lang=req.lang
            )
        except Exception as e:
            raise HTTPException(500, f"synthesis failed: {e}")
    elapsed = time.perf_counter() - t0
    print(f"[tts] voice={req.voice} chars={len(req.text)} {elapsed:.2f}s")
    buf = io.BytesIO()
    sf.write(buf, np.asarray(samples), sample_rate, format="WAV", subtype="PCM_16")
    buf.seek(0)
    return Response(
        content=buf.read(),
        media_type="audio/wav",
        headers={"x-synth-seconds": f"{elapsed:.3f}", "x-voice": req.voice},
    )
