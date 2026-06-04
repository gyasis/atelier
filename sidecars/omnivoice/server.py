"""
omnivoice-local — OmniVoice (k2-fsa, Apache 2.0) on Linux RTX 2060 CUDA.

Multi-speaker zero-shot TTS via Diffusion Language Model. Beats Dia 1.6B by
40x on Apple Silicon per Gemini deep_research 2026-05-24, expected ~0.05x
RTF on CUDA. Sidecar mirrors the atelier pattern documented in
~/Documents/code/atelier/docs/SIDECAR_PATTERN.md:
  GET /healthz, /readyz, /voices
  POST /tts, /admin/unload

Endpoint diffs vs Kokoro:
  - /tts accepts optional `ref_audio` (path or base64) for voice cloning
  - voices list is dynamic (model-defined, not preset)
  - default port 18770 (matches Mac convention with +10000 offset)

Env vars:
  OMNIVOICE_MODEL          default "k2-fsa/OmniVoice" (HF hub id)
  OMNIVOICE_DEVICE         default "cuda" (or "cpu" / "mps")
  OMNIVOICE_PORT           default 18770
  IDLE_UNLOAD_SECONDS      default 240 (4 min)
  KEEP_WARM                default "false" (NEW model — let it unload while we evaluate)
  HUB_TOKEN                optional bearer token
"""
import asyncio
import gc
import io
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from omnivoice import OmniVoice, OmniVoiceGenerationConfig

MODEL_ID = os.environ.get("OMNIVOICE_MODEL", "k2-fsa/OmniVoice")
def _auto_device() -> str:
    if torch.cuda.is_available(): return "cuda"
    if torch.backends.mps.is_available(): return "mps"
    return "cpu"
DEVICE = os.environ.get("OMNIVOICE_DEVICE", _auto_device())
HUB_TOKEN = os.environ.get("HUB_TOKEN")
IDLE_UNLOAD_SECONDS = int(os.environ.get("IDLE_UNLOAD_SECONDS", "240"))
KEEP_WARM = os.environ.get("KEEP_WARM", "false").lower() in ("1", "true", "yes")
IDLE_TICK_SECONDS = 30
DTYPE = torch.float16 if DEVICE != "cpu" else torch.float32

_model: OmniVoice | None = None
_warmed: bool = False
_sem = asyncio.Semaphore(1)
_last_request_at = time.monotonic()
_unload_task: asyncio.Task | None = None
_idle_unloaded_at: float | None = None
# Job-aware state (memory governor): _active = inferences in flight (busy when >0),
# _waiting = callers blocked on the semaphore (queue depth). The idle-watcher must
# never unload while _active>0 or _waiting>0, regardless of the idle timer.
_active: int = 0
_waiting: int = 0


async def _load_and_warm():
    global _model, _warmed, _idle_unloaded_at
    if _model is not None and _warmed:
        return
    t0 = time.perf_counter()
    print(f"[omnivoice-local] loading {MODEL_ID} on {DEVICE} ({DTYPE})")
    _model = await asyncio.to_thread(
        lambda: OmniVoice.from_pretrained(MODEL_ID, device_map=DEVICE, dtype=DTYPE, load_asr=False)
    )
    print(f"[omnivoice-local] model loaded in {time.perf_counter()-t0:.2f}s")
    try:
        await asyncio.to_thread(_model.generate, text="warmup", language="en")
        _warmed = True
        _idle_unloaded_at = None
        print(f"[omnivoice-local] warmup OK in {time.perf_counter()-t0:.2f}s")
    except Exception as e:
        print(f"[omnivoice-local] warmup failed: {e}")
        _warmed = False


async def _unload_model():
    global _model, _warmed, _idle_unloaded_at
    if _model is None:
        return
    print(f"[omnivoice-local] idle-unload — freeing model from {DEVICE}")
    _model = None
    _warmed = False
    _idle_unloaded_at = time.monotonic()
    gc.collect()
    if DEVICE == "cuda" and torch.cuda.is_available():
        try: torch.cuda.empty_cache()
        except Exception: pass


async def _idle_watcher():
    if KEEP_WARM:
        print(f"[omnivoice-local] idle-watcher disabled (KEEP_WARM=true)")
        return
    print(f"[omnivoice-local] idle-watcher active (unload after {IDLE_UNLOAD_SECONDS}s idle)")
    while True:
        await asyncio.sleep(IDLE_TICK_SECONDS)
        if _model is None:
            continue
        # Busy-aware: never reap a model that's working or has queued work,
        # no matter how long the idle timer has run (protects long renders).
        if _active == 0 and _waiting == 0 and time.monotonic() - _last_request_at > IDLE_UNLOAD_SECONDS:
            async with _sem:
                if _model is not None and _active == 0:
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
    return {"ok": True, "service": "omnivoice-local", "version": "1.0", "device": DEVICE}


@app.get("/readyz")
def readyz():
    loaded = _warmed and _model is not None
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
        "model": MODEL_ID if _model else None,
        "device": DEVICE,
        "dtype": str(DTYPE).replace("torch.", ""),
        "idle_seconds": round(time.monotonic() - _last_request_at, 1),
        "idle_unload_seconds": IDLE_UNLOAD_SECONDS,
        "keep_warm": KEEP_WARM,
        "last_unload_ago_s": round(time.monotonic() - _idle_unloaded_at, 1) if _idle_unloaded_at else None,
    }


@app.post("/admin/unload")
async def admin_unload(request: Request):
    """Unload the model. Refuses while busy unless ?force=true (the governor's
    human-gated preempt). force still waits for the in-flight op to release the
    semaphore — it can't kill a thread mid-generate, only unload right after."""
    _check_auth(request)
    force = request.query_params.get("force", "").lower() in ("1", "true", "yes")
    if _active > 0 and not force:
        return {"unloaded": False, "refused": "busy", "active_jobs": _active}
    was_loaded = _model is not None
    if was_loaded:
        async with _sem:
            await _unload_model()
    return {"unloaded": was_loaded, "forced": force, "model": MODEL_ID, "device": DEVICE}


@app.get("/agent")
def agent(request: Request):
    """Self-describing manifest for AI agents — methods, params, how-to."""
    _check_auth(request)
    auth = ("send header `Authorization: Bearer <HUB_TOKEN>` on every request"
            if HUB_TOKEN else "none required (HUB_TOKEN not set)")
    return {
        "service": "omnivoice",
        "role": "TTS — PRIMARY; instruct-driven accent/pitch/tone + zero-shot cloning",
        "summary": "The natural-voice engine. Control delivery with a plain-language "
                   "`instruct` prompt (accent, tone, emotion), clone a voice from a "
                   "3–10s reference clip, or shift pitch — all per request.",
        "auth": auth,
        "voices": {"mode": "zero-shot / instruct-driven (no preset list)",
                   "cloning": "pass ref_audio (+ ref_text) with a 3–10s clip"},
        "methods": [
            {"name": "tts", "http": "POST /tts", "encoding": "application/json",
             "params": {"text": "1–5000 chars", "language": "e.g. en",
                        "instruct": "plain-language style, e.g. 'British accent, bright feminine tone'",
                        "ref_audio": "path to a 3–10s clip to clone", "ref_text": "transcript of ref_audio",
                        "speed": "0.5–2.0", "num_step": "8–128 diffusion steps (higher=smoother,slower)",
                        "guidance_scale": "1–5", "class_temperature": "0–1.5 prosodic variation",
                        "pitch_semitones": "-12..+12"},
             "returns": "audio/wav (24kHz); header x-synth-seconds",
             "example": "curl -s $URL/tts -H 'content-type: application/json' "
                        "-d '{\"text\":\"Welcome\",\"instruct\":\"warm, slow, deep male\"}' --output out.wav"},
            {"name": "readyz", "http": "GET /readyz", "returns": "warm/cold/busy, queue depth"},
        ],
        "recipes": [
            {"goal": "Natural narration with a style", "do": "POST /tts {text, instruct:'calm documentary narrator'}"},
            {"goal": "Clone a voice", "do": "POST /tts {text, ref_audio:'/abs/ref.wav', ref_text:'…'}"},
            {"goal": "Brighter/higher voice", "do": "POST /tts {text, pitch_semitones:3}"},
        ],
        "instructions": (
            "1) POST /tts {text}. Add `instruct` for accent/tone/emotion — this engine's superpower.\n"
            "2) To clone, pass ref_audio (3–10s) and ref_text.\n"
            "3) num_step trades quality vs speed; pitch_semitones nudges brightness.\n"
            "4) Fastest fixed-voice TTS → kokoro. [S1]/[S2] dialogue cloning → dia."
        ),
        "openapi": "/openapi.json",
    }


class TtsReq(BaseModel):
    text: str = Field(..., min_length=1, max_length=5000)
    language: str = "en"
    ref_audio: str | None = Field(None, description="optional path to reference audio for voice cloning (3-10s clip)")
    ref_text: str | None = Field(None, description="optional transcript of the reference audio")
    speed: float = Field(1.0, ge=0.5, le=2.0)
    # Quality knobs — defaults tuned for less-robotic output (vs OmniVoice's
    # speed-optimized 32/2.0/0.0). num_step is the big lever; class_temperature
    # adds natural prosodic variation (0.0 = deterministic = robotic).
    num_step: int = Field(48, ge=8, le=128, description="diffusion steps — higher = smoother, slower")
    guidance_scale: float = Field(2.0, ge=1.0, le=5.0)
    class_temperature: float = Field(0.3, ge=0.0, le=1.5, description="prosodic variation — 0=deterministic")
    # Style prompt — OmniVoice's text-conditioned control (accent, tone, emotion).
    instruct: str | None = Field(None, description="e.g. 'Speak with a British accent, bright feminine tone'")
    # Post-process pitch shift in semitones (librosa, tempo-preserving). +2..+4
    # makes a voice brighter/higher; negative lowers. 0 = no shift.
    pitch_semitones: float = Field(0.0, ge=-12.0, le=12.0)


@app.post("/tts")
async def tts(req: TtsReq, request: Request):
    global _last_request_at, _active, _waiting
    _check_auth(request)
    if not req.text.strip():
        raise HTTPException(400, "empty text")
    if _model is None or not _warmed:
        print(f"[omnivoice-local] cold-load triggered by /tts")
        async with _sem:
            await _load_and_warm()
        if not _warmed:
            raise HTTPException(503, "cold-load failed")
    t0 = time.perf_counter()
    _waiting += 1                      # queued (blocked on the single-flight sem)
    async with _sem:
        _waiting -= 1
        _active += 1                   # now busy — watcher won't reap us
        try:
            gen_cfg = OmniVoiceGenerationConfig(
                num_step=req.num_step,
                guidance_scale=req.guidance_scale,
                class_temperature=req.class_temperature,
            )
            gen_kwargs = {"text": req.text, "language": req.language, "speed": req.speed, "generation_config": gen_cfg}
            if req.ref_audio:
                gen_kwargs["ref_audio"] = req.ref_audio
            if req.ref_text:
                gen_kwargs["ref_text"] = req.ref_text
            if req.instruct:
                gen_kwargs["instruct"] = req.instruct
            audio_list = await asyncio.to_thread(_model.generate, **gen_kwargs)
        except Exception as e:
            raise HTTPException(500, f"synthesis failed: {e}")
        finally:
            _active -= 1                # no longer busy
            _last_request_at = time.monotonic()   # idle clock starts at job END
    elapsed = time.perf_counter() - t0
    # OmniVoice.generate() returns list[np.ndarray] — take the first (batch=1)
    samples = audio_list[0] if isinstance(audio_list, list) else audio_list
    # OmniVoice default sample rate is 24000 Hz (per the research spec)
    sample_rate = 24000
    # Optional tempo-preserving pitch shift (librosa). Lets SARAH be brighter
    # without re-cloning. Applied post-synth so it composes with any voice.
    if req.pitch_semitones != 0.0:
        try:
            import librosa
            samples = librosa.effects.pitch_shift(
                np.asarray(samples, dtype=np.float32), sr=sample_rate, n_steps=req.pitch_semitones
            )
        except Exception as e:
            print(f"[omnivoice] pitch shift failed ({e}) — returning unshifted")
    print(f"[tts] chars={len(req.text)} num_step={req.num_step} {elapsed:.2f}s rtf={elapsed/(len(samples)/sample_rate):.2f}x")
    buf = io.BytesIO()
    sf.write(buf, np.asarray(samples), sample_rate, format="WAV", subtype="PCM_16")
    buf.seek(0)
    return Response(
        content=buf.read(),
        media_type="audio/wav",
        headers={"x-synth-seconds": f"{elapsed:.3f}", "x-device": DEVICE},
    )
