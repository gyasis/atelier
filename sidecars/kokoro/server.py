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
import gc
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
# Constitutional idle-unload (2026-05-24). Kokoro defaults to KEEP_WARM=true
# because it serves the live podcast pipeline — every cold-load adds ~2s
# latency that the user feels per chapter. Set KEEP_WARM=false to opt into
# memory-saving unloads (overnight / multi-model contention scenarios).
IDLE_UNLOAD_SECONDS = int(os.environ.get("IDLE_UNLOAD_SECONDS", "300"))
KEEP_WARM = os.environ.get("KEEP_WARM", "true").lower() in ("1", "true", "yes")
IDLE_TICK_SECONDS = 30

_kokoro: Kokoro | None = None
_warmed: bool = False
_sem = asyncio.Semaphore(1)
_voices: list[str] = []
_last_request_at = time.monotonic()
_unload_task: asyncio.Task | None = None
_idle_unloaded_at: float | None = None
# Job-aware state (memory governor): _active = inferences in flight (busy when >0),
# _waiting = callers queued on the single-flight semaphore. The idle-watcher must
# never unload while _active>0 or _waiting>0.
_active: int = 0
_waiting: int = 0


async def _load_and_warm():
    """Cold-load Kokoro + warmup. Idempotent."""
    global _kokoro, _warmed, _voices, _idle_unloaded_at
    if _kokoro is not None and _warmed:
        return
    t0 = time.perf_counter()
    _kokoro = Kokoro(MODEL_PATH, VOICES_PATH)
    try:
        _voices = sorted(list(_kokoro.get_voices()))
    except Exception:
        _voices = []
    print(f"[kokoro] model loaded in {time.perf_counter()-t0:.2f}s, voices={len(_voices)}")
    try:
        await asyncio.to_thread(_kokoro.create, "hello", voice=DEFAULT_VOICE, speed=1.0, lang="en-us")
        _warmed = True
        _idle_unloaded_at = None
        print(f"[kokoro] warmup OK in {time.perf_counter()-t0:.2f}s")
    except Exception as e:
        print(f"[kokoro] warmup failed: {e}")


async def _unload_model():
    """Drop Kokoro model (small ~80 MB ONNX, but still — hub courtesy)."""
    global _kokoro, _warmed, _idle_unloaded_at
    if _kokoro is None:
        return
    print(f"[kokoro] idle-unload")
    _kokoro = None
    _warmed = False
    _idle_unloaded_at = time.monotonic()
    gc.collect()


async def _idle_watcher():
    if KEEP_WARM:
        print(f"[kokoro] idle-watcher disabled (KEEP_WARM=true)")
        return
    print(f"[kokoro] idle-watcher active (unload after {IDLE_UNLOAD_SECONDS}s idle)")
    while True:
        await asyncio.sleep(IDLE_TICK_SECONDS)
        if _kokoro is None:
            continue
        # Busy-aware: never reap while working or with queued work.
        if _active == 0 and _waiting == 0 and time.monotonic() - _last_request_at > IDLE_UNLOAD_SECONDS:
            async with _sem:
                if _kokoro is not None and _active == 0:
                    await _unload_model()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _unload_task
    async with _sem:
        await _load_and_warm()
    _unload_task = asyncio.create_task(_idle_watcher())
    yield
    if _unload_task and not _unload_task.done():
        _unload_task.cancel()
        try: await _unload_task
        except asyncio.CancelledError: pass


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
    loaded = _warmed and _kokoro is not None
    state = "warm" if loaded else "cold"
    lifecycle = "cold" if not loaded else ("busy" if _active > 0 else "idle")
    return {
        "ok": True,
        "state": state,
        "lifecycle": lifecycle,
        "busy": _active > 0,
        "active_jobs": _active,
        "queue_depth": _waiting,
        "warmed": _warmed,
        "models_loaded": [os.path.basename(MODEL_PATH)] if _kokoro else [],
        "device": "coreml/mps",
        "voices": len(_voices),
        "idle_seconds": round(time.monotonic() - _last_request_at, 1),
        "idle_unload_seconds": IDLE_UNLOAD_SECONDS,
        "keep_warm": KEEP_WARM,
        "last_unload_ago_s": round(time.monotonic() - _idle_unloaded_at, 1) if _idle_unloaded_at else None,
    }


@app.get("/voices")
def voices(request: Request):
    _check_auth(request)
    return {"voices": _voices, "default": DEFAULT_VOICE}


@app.post("/admin/unload")
async def admin_unload(request: Request):
    """Unload the model. Refuses while busy unless ?force=true (governor preempt)."""
    _check_auth(request)
    force = request.query_params.get("force", "").lower() in ("1", "true", "yes")
    if _active > 0 and not force:
        return {"unloaded": False, "refused": "busy", "active_jobs": _active}
    was_loaded = _kokoro is not None
    if was_loaded:
        async with _sem:
            await _unload_model()
    return {"unloaded": was_loaded, "forced": force, "model": os.path.basename(MODEL_PATH)}


class TtsReq(BaseModel):
    text: str = Field(..., min_length=1, max_length=5000)
    voice: str = DEFAULT_VOICE
    speed: float = Field(1.0, ge=0.5, le=2.0)
    lang: str = "en-us"


@app.post("/tts")
async def tts(req: TtsReq, request: Request):
    global _last_request_at, _active, _waiting
    _check_auth(request)
    if not req.text.strip():
        raise HTTPException(400, "empty text")
    # Cold-load if previously idle-unloaded
    if _kokoro is None or not _warmed:
        print(f"[kokoro] cold-load triggered by /tts")
        async with _sem:
            await _load_and_warm()
        if not _warmed:
            raise HTTPException(503, "cold-load failed")
    t0 = time.perf_counter()
    _waiting += 1
    async with _sem:
        _waiting -= 1
        _active += 1
        try:
            samples, sample_rate = await asyncio.to_thread(
                _kokoro.create, req.text, voice=req.voice, speed=req.speed, lang=req.lang
            )
        except Exception as e:
            raise HTTPException(500, f"synthesis failed: {e}")
        finally:
            _active -= 1
            _last_request_at = time.monotonic()
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
