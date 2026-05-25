"""
voice-clone-sidecar — Dia 1.6B multi-speaker dialogue TTS on Mac Studio MPS.

v2 (2026-05-24): voice cloning baked in. Server preloads LEO + SARAH
reference audio (5-10 sec each) at startup. Every generation prepends
the reference transcripts and passes the audio as a prompt so the
output locks onto our canonical voices instead of rolling random ones.

Endpoints:
    GET  /healthz       liveness
    GET  /readyz        503 until model is warmed
    POST /tts           text -> audio/wav (cloned voices)

Env vars:
    DIA_MODEL_CHECKPOINT   default "nari-labs/Dia-1.6B-0626"
    DIA_DEVICE             default "mps"
    DIA_DTYPE              default "float16"
    DIA_LEO_REF_AUDIO      default "/Users/gyasisutton/models/voice-refs/leo_ref.wav"
    DIA_LEO_REF_TEXT       transcript of the LEO ref clip (used as [S1] prefix)
    DIA_SARAH_REF_AUDIO    default "/Users/gyasisutton/models/voice-refs/sarah_ref.wav"
    DIA_SARAH_REF_TEXT     transcript of the SARAH ref clip (used as [S2] prefix)
    HUB_TOKEN              optional bearer token
"""

import asyncio
import gc
import io
import os
import time
from contextlib import asynccontextmanager

import numpy as np
import soundfile as sf
import torch
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from transformers import AutoProcessor, DiaForConditionalGeneration

MODEL_CHECKPOINT = os.environ.get("DIA_MODEL_CHECKPOINT", "nari-labs/Dia-1.6B-0626")
DEVICE = os.environ.get("DIA_DEVICE", "mps")
DTYPE_STR = os.environ.get("DIA_DTYPE", "float16")
HUB_TOKEN = os.environ.get("HUB_TOKEN")
# Constitutional principle (added 2026-05-24): idle-loaded models on a shared
# inference hub waste precious unified memory. Default: unload after 3 min
# of no requests. Override per-instance with KEEP_WARM=true (e.g. for live
# pipelines where cold-start latency is unacceptable).
IDLE_UNLOAD_SECONDS = int(os.environ.get("IDLE_UNLOAD_SECONDS", "180"))
KEEP_WARM = os.environ.get("KEEP_WARM", "false").lower() in ("1", "true", "yes")
IDLE_TICK_SECONDS = 30

LEO_REF_AUDIO = os.environ.get(
    "DIA_LEO_REF_AUDIO", "/Users/gyasisutton/models/voice-refs/leo_ref.wav"
)
LEO_REF_TEXT = os.environ.get(
    "DIA_LEO_REF_TEXT",
    "from NPR. A couple hundred years ago, some countries suddenly got rich.",
)
SARAH_REF_AUDIO = os.environ.get(
    "DIA_SARAH_REF_AUDIO", "/Users/gyasisutton/models/voice-refs/sarah_ref.wav"
)
SARAH_REF_TEXT = os.environ.get(
    "DIA_SARAH_REF_TEXT",
    "Hi, my name is Sarah, and Im here to keep Leo honest about the projects. Lets get into it.",
)

DTYPE_MAP = {
    "float32": torch.float32,
    "float16": torch.float16,
    "bfloat16": torch.bfloat16,
}

_processor = None
_model = None
_warmed = False
_sem = asyncio.Semaphore(1)
_leo_audio = None
_sarah_audio = None
_clone_prefix_text = ""  # "[S1] <leo ref> [S2] <sarah ref> " — prepended to every gen
_clone_audio = None       # numpy array of concatenated leo+sarah refs
_last_request_at = time.monotonic()
_unload_task: asyncio.Task | None = None
_idle_unloaded_at: float | None = None  # timestamp of most recent unload (for /readyz reporting)


def _normalize_script(text: str) -> str:
    t = text.strip()
    if "[S1]" not in t and "[S2]" not in t:
        return f"[S1] {t}"
    return t


def _load_audio_mono(path: str, target_sr: int = 44_100) -> np.ndarray:
    """Load WAV/MP3 as float32 mono numpy array at target_sr."""
    data, sr = sf.read(path, dtype="float32", always_2d=False)
    if data.ndim > 1:
        data = data.mean(axis=1)
    if sr != target_sr:
        import scipy.signal as sps
        n_samples = int(round(len(data) * target_sr / sr))
        data = sps.resample(data, n_samples).astype(np.float32)
    return data


async def _load_processor_and_model_and_warm() -> None:
    """Cold-load Dia: processor + model + warmup. Idempotent; if already
    loaded, skips. Used both at startup and after idle-unload."""
    global _processor, _model, _warmed, _idle_unloaded_at
    if _model is not None and _warmed:
        return
    t0 = time.perf_counter()
    print(f"[dia] loading processor + model from {MODEL_CHECKPOINT} on {DEVICE}/{DTYPE_STR}")
    if _processor is None:
        _processor = await asyncio.to_thread(AutoProcessor.from_pretrained, MODEL_CHECKPOINT)
    torch_dtype = DTYPE_MAP.get(DTYPE_STR, torch.float16)
    _model = await asyncio.to_thread(
        lambda: DiaForConditionalGeneration.from_pretrained(
            MODEL_CHECKPOINT, dtype=torch_dtype
        ).to(DEVICE)
    )
    print(f"[dia] model loaded in {time.perf_counter()-t0:.1f}s")
    # Warmup (semaphore acquired by caller OR not needed during startup)
    try:
        warm_text = ["[S1] Warming up. [S2] Ready."]
        inputs = await asyncio.to_thread(
            lambda: _processor(text=warm_text, padding=True, return_tensors="pt").to(DEVICE)
        )
        with torch.no_grad():
            _ = await asyncio.to_thread(
                lambda: _model.generate(
                    **inputs, max_new_tokens=256, guidance_scale=3.0,
                    temperature=1.0, top_p=0.9, top_k=45,
                )
            )
        _warmed = True
        _idle_unloaded_at = None
        print(f"[dia] warmup OK in {time.perf_counter()-t0:.1f}s total, cloning={'on' if _clone_audio is not None else 'off'}")
    except Exception as e:
        print(f"[dia] warmup failed: {e}")
        _warmed = False


async def _unload_model() -> None:
    """Drop the model from MPS memory. Voice-clone reference audio is kept
    (it's ~100KB, negligible). Processor is also kept since it's lightweight
    and avoids re-downloading on next load."""
    global _model, _warmed, _idle_unloaded_at
    if _model is None:
        return
    print(f"[dia] idle-unload — freeing model from {DEVICE}")
    _model = None
    _warmed = False
    _idle_unloaded_at = time.monotonic()
    gc.collect()
    if DEVICE == "mps" and hasattr(torch.mps, "empty_cache"):
        try: torch.mps.empty_cache()
        except Exception: pass
    elif DEVICE == "cuda" and hasattr(torch.cuda, "empty_cache"):
        try: torch.cuda.empty_cache()
        except Exception: pass


async def _idle_watcher() -> None:
    """Background tick that unloads the model when idle for too long.
    Skipped entirely when KEEP_WARM=true (manual override)."""
    if KEEP_WARM:
        print(f"[dia] idle-watcher disabled (KEEP_WARM=true)")
        return
    print(f"[dia] idle-watcher active (unload after {IDLE_UNLOAD_SECONDS}s idle)")
    while True:
        await asyncio.sleep(IDLE_TICK_SECONDS)
        if _model is None:
            continue
        idle = time.monotonic() - _last_request_at
        if idle > IDLE_UNLOAD_SECONDS:
            async with _sem:
                if _model is not None:  # re-check inside sem
                    await _unload_model()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _leo_audio, _sarah_audio, _clone_prefix_text, _clone_audio, _unload_task

    # Load reference audio for voice cloning (kept resident — only ~100KB).
    try:
        _leo_audio = _load_audio_mono(LEO_REF_AUDIO)
        _sarah_audio = _load_audio_mono(SARAH_REF_AUDIO)
        _clone_audio = np.concatenate([_leo_audio, _sarah_audio]).astype(np.float32)
        _clone_prefix_text = f"[S1] {LEO_REF_TEXT.strip()} [S2] {SARAH_REF_TEXT.strip()} "
        print(
            f"[dia] voice clone refs loaded: "
            f"LEO {len(_leo_audio)/44100:.2f}s, SARAH {len(_sarah_audio)/44100:.2f}s, "
            f"total prompt {len(_clone_audio)/44100:.2f}s"
        )
    except Exception as e:
        print(f"[dia] WARNING: could not load voice clone refs ({e}). Falling back to no cloning.")
        _leo_audio = _sarah_audio = _clone_audio = None
        _clone_prefix_text = ""

    # Cold-load the model at startup (we'd be cold otherwise).
    async with _sem:
        await _load_processor_and_model_and_warm()

    # Kick off the idle watcher (no-op if KEEP_WARM=true)
    _unload_task = asyncio.create_task(_idle_watcher())

    yield

    # Clean shutdown
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
    return {
        "ok": True,
        "service": "dia",
        "model": MODEL_CHECKPOINT,
        "voice_cloning": _clone_audio is not None,
        "version": "2.0",
    }


@app.get("/readyz")
def readyz():
    # warm = model is in memory and warmed up; cold = unloaded due to idle.
    # Both are OK states — cold just means next /tts pays cold-load latency
    # (~10s). Returning 200 in cold state so callers know the sidecar is
    # reachable; they choose whether to pay cold-load cost or pick a different
    # engine.
    state = "warm" if _warmed and _model is not None else "cold"
    idle_seconds = round(time.monotonic() - _last_request_at, 1)
    return {
        "ok": True,
        "state": state,
        "warmed": _warmed,
        "model": MODEL_CHECKPOINT,
        "device": DEVICE,
        "dtype": DTYPE_STR,
        "voice_cloning": _clone_audio is not None,
        "leo_ref_seconds": float(len(_leo_audio) / 44100) if _leo_audio is not None else None,
        "sarah_ref_seconds": float(len(_sarah_audio) / 44100) if _sarah_audio is not None else None,
        "idle_seconds": idle_seconds,
        "idle_unload_seconds": IDLE_UNLOAD_SECONDS,
        "keep_warm": KEEP_WARM,
        "last_unload_ago_s": round(time.monotonic() - _idle_unloaded_at, 1) if _idle_unloaded_at else None,
    }


@app.post("/admin/unload")
async def admin_unload(request: Request):
    """Force-unload the model NOW. Used by atelier-status CLI and by peer
    sidecars before they load (memory courtesy). Idempotent — returns 200
    whether or not a model was actually loaded."""
    _check_auth(request)
    was_loaded = _model is not None
    if was_loaded:
        async with _sem:
            await _unload_model()
    return {"unloaded": was_loaded, "model": MODEL_CHECKPOINT, "device": DEVICE}


class TtsReq(BaseModel):
    text: str = Field(..., min_length=1, max_length=8000)
    max_new_tokens: int = Field(3072, ge=128, le=4096)
    guidance_scale: float = Field(4.0, ge=1.0, le=10.0)
    temperature: float = Field(1.8, ge=0.5, le=2.5)
    top_p: float = Field(0.90, ge=0.1, le=1.0)
    top_k: int = Field(45, ge=1, le=200)
    use_voice_clone: bool = True


@app.post("/tts")
async def tts(req: TtsReq, request: Request):
    global _last_request_at
    _check_auth(request)

    user_text = _normalize_script(req.text)
    use_clone = req.use_voice_clone and _clone_audio is not None

    # Cold-load if needed (model was unloaded due to idle). Pays ~10s
    # cold-load latency, then warm for IDLE_UNLOAD_SECONDS again.
    if _model is None or not _warmed:
        print(f"[dia] cold-load triggered by /tts (was unloaded {round(time.monotonic() - _idle_unloaded_at, 1) if _idle_unloaded_at else '?'}s ago)")
        async with _sem:
            await _load_processor_and_model_and_warm()
        if not _warmed:
            raise HTTPException(503, "cold-load failed; see server logs")
    _last_request_at = time.monotonic()

    if use_clone:
        full_text = _clone_prefix_text + user_text
        prompt_audio = _clone_audio
    else:
        full_text = user_text
        prompt_audio = None

    t0 = time.perf_counter()
    async with _sem:
        try:
            def _prep_inputs():
                if prompt_audio is not None:
                    return _processor(
                        text=[full_text], audio=[prompt_audio],
                        padding=True, return_tensors="pt"
                    ).to(DEVICE)
                return _processor(
                    text=[full_text], padding=True, return_tensors="pt"
                ).to(DEVICE)
            inputs = await asyncio.to_thread(_prep_inputs)
            with torch.no_grad():
                outputs = await asyncio.to_thread(
                    lambda: _model.generate(
                        **inputs,
                        max_new_tokens=req.max_new_tokens,
                        guidance_scale=req.guidance_scale,
                        temperature=req.temperature,
                        top_p=req.top_p,
                        top_k=req.top_k,
                    )
                )
            decoded = await asyncio.to_thread(lambda: _processor.batch_decode(outputs))
        except Exception as e:
            raise HTTPException(500, f"synthesis failed: {e}")

    elapsed = time.perf_counter() - t0
    audio = decoded[0]
    if isinstance(audio, torch.Tensor):
        audio = audio.detach().cpu().numpy()
    audio_np = np.asarray(audio).squeeze()

    # When cloning, the output prepends the reference audio. Crop it.
    # Each audio second is ~44_100 samples at Dia's native rate.
    if use_clone and _clone_audio is not None:
        prefix_samples = len(_clone_audio)
        if len(audio_np) > prefix_samples:
            audio_np = audio_np[prefix_samples:]

    sample_rate = 44_100
    buf = io.BytesIO()
    sf.write(buf, audio_np, sample_rate, format="WAV", subtype="PCM_16")
    buf.seek(0)
    print(f"[tts] chars={len(req.text)} {elapsed:.2f}s clone={use_clone}")
    return Response(
        content=buf.read(),
        media_type="audio/wav",
        headers={
            "x-synth-seconds": f"{elapsed:.3f}",
            "x-engine": "dia-1.6b",
            "x-voice-clone": "on" if use_clone else "off",
        },
    )
