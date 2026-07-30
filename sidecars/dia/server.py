"""
voice-clone-sidecar — Dia 1.6B multi-speaker dialogue TTS on Mac Studio MPS.

GOVERNED via the shared lifecycle framework (sidecars/_common/lifecycle.py) — it inherits the
3-law constitution automatically: no memory leaks (MPS self-restart reclaim), never unload
mid-generation, and a request queue. This file holds ONLY Dia's model + voice-clone refs +
endpoints; all lifecycle, memory, busy-guard, queue, /healthz, /readyz, /admin/unload come
from GovernedSidecar.

Voice-clone reference audio (LEO=[S1], SARAH=[S2], optional GYASI=[S3]) is loaded ONCE at
process start (module-level, independent of the model's load/idle-unload cycle) — it's tiny
(~100KB) and must NOT be dropped/reloaded along with the heavy model on idle-unload/restart.

Endpoints:
    POST /tts           text -> audio/wav (cloned voices)
    GET  /agent         self-describing manifest for AI agents
    GET  /healthz /readyz · POST /admin/unload   (from the framework)

Env vars:
    DIA_MODEL_CHECKPOINT   default "nari-labs/Dia-1.6B-0626"
    DIA_DEVICE             default auto-detected (mps on this box)
    DIA_DTYPE              default "float16"
    DIA_LEO_REF_AUDIO / DIA_LEO_REF_TEXT       LEO [S1] voice-clone reference
    DIA_SARAH_REF_AUDIO / DIA_SARAH_REF_TEXT   SARAH [S2] voice-clone reference
    DIA_GYASI_REF_AUDIO / DIA_GYASI_REF_TEXT   optional GYASI [S3] voice-clone reference
    HUB_TOKEN               optional bearer token
    IDLE_UNLOAD_SECONDS (180) · KEEP_WARM · RECLAIM_THRESHOLD_GB · MAX_GEN_HANG_S ·
    DIA_MAX_CONCURRENCY
"""

import io
import os
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

import numpy as np
import soundfile as sf
import torch
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from transformers import AutoProcessor, DiaForConditionalGeneration

from lifecycle import GovernedSidecar

MODEL_CHECKPOINT = os.environ.get("DIA_MODEL_CHECKPOINT", "nari-labs/Dia-1.6B-0626")
DTYPE_STR = os.environ.get("DIA_DTYPE", "float16")
PORT = int(os.environ.get("DIA_PORT", "8769"))

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
# GYASI [S3] — optional third canonical voice. Only loaded if DIA_GYASI_REF_AUDIO is set.
GYASI_REF_AUDIO = os.environ.get("DIA_GYASI_REF_AUDIO", "")
GYASI_REF_TEXT = os.environ.get("DIA_GYASI_REF_TEXT", "")

DTYPE_MAP = {
    "float32": torch.float32,
    "float16": torch.float16,
    "bfloat16": torch.bfloat16,
}

# ---- voice-clone reference state (loaded once at startup; independent of model lifecycle) ----
_leo_audio = None
_sarah_audio = None
_gyasi_audio = None       # optional [S3] ref (None unless DIA_GYASI_REF_AUDIO is set)
_clone_audio = None       # numpy array of concatenated leo+sarah(+gyasi) refs
_speaker_refs = {}        # {"S1": (ref_text, ref_audio), ...} — for per-request prompt selection


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


def _load_voice_refs() -> None:
    """One-time load of voice-clone reference audio (~100KB total) — called once at process
    startup, OUTSIDE the framework's model load/unload cycle, so it survives idle-unload and
    reclaim-restarts of the (much heavier) Dia model without re-reading from disk each time."""
    global _leo_audio, _sarah_audio, _gyasi_audio, _clone_audio, _speaker_refs
    try:
        _leo_audio = _load_audio_mono(LEO_REF_AUDIO)
        _sarah_audio = _load_audio_mono(SARAH_REF_AUDIO)
        refs = [_leo_audio, _sarah_audio]
        _speaker_refs = {
            "S1": (LEO_REF_TEXT.strip(), _leo_audio),
            "S2": (SARAH_REF_TEXT.strip(), _sarah_audio),
        }
        # Optional [S3] GYASI voice — only if configured.
        if GYASI_REF_AUDIO:
            _gyasi_audio = _load_audio_mono(GYASI_REF_AUDIO)
            refs.append(_gyasi_audio)
            _speaker_refs["S3"] = (GYASI_REF_TEXT.strip(), _gyasi_audio)
        _clone_audio = np.concatenate(refs).astype(np.float32)
        _gyasi_s = f", GYASI {len(_gyasi_audio)/44100:.2f}s" if _gyasi_audio is not None else ""
        _speakers = "S1/S2/S3" if _gyasi_audio is not None else "S1/S2"
        print(
            f"[dia] voice clone refs loaded: "
            f"LEO {len(_leo_audio)/44100:.2f}s, SARAH {len(_sarah_audio)/44100:.2f}s{_gyasi_s}, "
            f"total prompt {len(_clone_audio)/44100:.2f}s, speakers={_speakers}",
            flush=True,
        )
    except Exception as e:
        print(f"[dia] WARNING: could not load voice clone refs ({e}). Falling back to no cloning.",
              flush=True)
        _leo_audio = _sarah_audio = _gyasi_audio = _clone_audio = None
        _speaker_refs = {}


def _load():
    """Cold-load Dia processor + model on sc.device, then warm up with a short real generation.
    Runs on the event-loop thread at load time (matches the colpali reference — load is rare and
    one-shot; blocking briefly is acceptable). Returns the opaque handle GovernedSidecar holds."""
    torch_dtype = DTYPE_MAP.get(DTYPE_STR, torch.float16)
    print(f"[dia] loading processor + model from {MODEL_CHECKPOINT} on {sc.device}/{DTYPE_STR}",
          flush=True)
    processor = AutoProcessor.from_pretrained(MODEL_CHECKPOINT)
    model = DiaForConditionalGeneration.from_pretrained(
        MODEL_CHECKPOINT, dtype=torch_dtype
    ).to(sc.device)
    # Warmup — a real short generation so the first /tts caller doesn't pay graph/kernel
    # compile cost. Any failure here raises and aborts the load (framework treats it as a
    # failed cold-load, which is correct — a model that can't warm up shouldn't be served).
    warm_text = ["[S1] Warming up. [S2] Ready."]
    inputs = processor(text=warm_text, padding=True, return_tensors="pt").to(sc.device)
    with torch.no_grad():
        model.generate(
            **inputs, max_new_tokens=256, guidance_scale=3.0,
            temperature=1.0, top_p=0.9, top_k=45,
        )
    return SimpleNamespace(processor=processor, model=model)


sc = GovernedSidecar(
    "dia",
    role="TTS — expressive voice cloning (Dia, batch)",
    load_fn=_load,
    model_name=MODEL_CHECKPOINT,
    idle_unload_s=int(os.environ.get("IDLE_UNLOAD_SECONDS", "180")),   # dia's existing default
    # Dia runs ~10x RTF and accepts up to 8000 chars of text — a legitimate batch job can run
    # far longer than the framework's 600s default hang ceiling. Give it real headroom so the
    # Law-2 hang-recovery net doesn't force-restart a genuinely long (but healthy) generation.
    max_gen_hang_s=float(os.environ.get("MAX_GEN_HANG_S", "1800")),
    # single-flight on unified memory; framework reads DIA_MAX_CONCURRENCY to override
)

_framework_lifespan = sc.lifespan()


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Compose: load voice-clone refs once (outside the model's lifecycle), then delegate
    model load/idle-watch/shutdown to the framework's own lifespan."""
    _load_voice_refs()
    async with _framework_lifespan(app):
        yield


app = FastAPI(lifespan=_lifespan)
sc.attach(app)   # GET /healthz /readyz + POST /admin/unload


@app.get("/agent")
def agent(request: Request):
    """Self-describing manifest for AI agents — methods, params, how-to."""
    sc.check_auth(request)
    auth = ("send header `Authorization: Bearer <HUB_TOKEN>` on every request"
            if sc.hub_token else "none required (HUB_TOKEN not set)")
    return {
        "service": "dia",
        "role": sc.role,
        "summary": "Generate expressive speech in cloned canonical voices "
                   "(LEO=[S1], SARAH=[S2]). ~10x realtime — best for overnight/batch "
                   "dialogue, not live synthesis.",
        "auth": auth,
        "voices": {"cloning": _clone_audio is not None,
                   "speakers": {"[S1]": "LEO", "[S2]": "SARAH"},
                   "note": "tag lines with [S1]/[S2]; untagged text defaults to [S1]"},
        "methods": [
            {"name": "tts", "http": "POST /tts", "encoding": "application/json",
             "params": {"text": "dialogue with [S1]/[S2] speaker tags",
                        "use_voice_clone": "bool (default true)",
                        "speed": "0.5–1.5 — pitch-preserved pace; <1.0 = slower/enunciated",
                        "emotion": "neutral|calm|measured|warm|expressive — nudges expressiveness",
                        "max_new_tokens": "128–4096", "guidance_scale": "1–10",
                        "temperature": "0.5–2.5", "top_p": "0.1–1.0", "top_k": "1–200"},
             "returns": "audio/wav (44.1kHz); headers x-engine, x-voice-clone",
             "example": "curl -s $URL/tts -H 'content-type: application/json' "
                        "-d '{\"text\":\"[S1] Welcome back. [S2] Glad to be here.\"}' --output out.wav"},
            {"name": "readyz", "http": "GET /readyz", "returns": "warm/cold/busy"},
        ],
        "recipes": [
            {"goal": "Two-host podcast banter", "do": "POST /tts {text:'[S1] … [S2] …'}"},
            {"goal": "Single narrator (LEO)", "do": "POST /tts {text:'your line'} (defaults to [S1])"},
            {"goal": "Slow, enunciated reading", "do": "POST /tts {text:'…', speed:0.85, emotion:'measured'}"},
            {"goal": "Warm/expressive delivery", "do": "POST /tts {text:'…', emotion:'warm'} (or 'expressive')"},
        ],
        "instructions": (
            "1) Write the script with [S1] (LEO) / [S2] (SARAH) speaker tags.\n"
            "2) POST /tts {text}; cloning is on by default so the voices stay canonical.\n"
            "3) ~10x realtime — prefer batch use. For fast/live TTS use kokoro or omnivoice."
        ),
        "openapi": "/openapi.json",
    }


# emotion preset → (temperature, guidance_scale) overrides (Dia's expressiveness
# levers). None = keep the request's value. Added for chiron passage slow-reads.
_EMOTION_PRESETS = {
    "neutral":    (None, None),
    "calm":       (1.2, 3.0),
    "measured":   (1.0, 3.0),   # most deliberate — pairs well with speed<1 for enunciation
    "warm":       (1.5, 3.5),
    "expressive": (2.0, 4.5),
}


def _apply_speed(audio_np, sr, speed):
    """Pitch-preserved time-stretch via ffmpeg atempo (speed<1.0 = slower)."""
    import subprocess, tempfile, os
    ff = "/opt/homebrew/bin/ffmpeg" if os.path.exists("/opt/homebrew/bin/ffmpeg") else "ffmpeg"
    in_path = out_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as fin:
            in_path = fin.name
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as fout:
            out_path = fout.name
        sf.write(in_path, audio_np, sr, subtype="PCM_16")
        subprocess.run([ff, "-y", "-loglevel", "error", "-i", in_path,
                        "-filter:a", f"atempo={speed:.3f}", out_path], check=True)
        out, _ = sf.read(out_path, dtype="float32")
        return np.asarray(out).squeeze()
    finally:
        for p in (in_path, out_path):
            if p:
                try:
                    os.remove(p)
                except OSError:
                    pass


class TtsReq(BaseModel):
    text: str = Field(..., min_length=1, max_length=8000)
    max_new_tokens: int = Field(3072, ge=128, le=4096)
    guidance_scale: float = Field(4.0, ge=1.0, le=10.0)
    temperature: float = Field(1.8, ge=0.5, le=2.5)
    top_p: float = Field(0.90, ge=0.1, le=1.0)
    top_k: int = Field(45, ge=1, le=200)
    use_voice_clone: bool = True
    # --- pacing + emotion knobs (chiron passage slow-reads) ---
    speed: float = Field(1.0, ge=0.5, le=1.5)   # <1.0 = slower/enunciated (pitch-preserved)
    emotion: str = Field("neutral")             # neutral|calm|measured|warm|expressive


@app.post("/tts")
async def tts(req: TtsReq, request: Request):
    sc.check_auth(request)

    user_text = _normalize_script(req.text)
    use_clone = req.use_voice_clone and _clone_audio is not None

    # Emotion preset nudges Dia's expressiveness levers (temperature + guidance).
    _emo_t, _emo_g = _EMOTION_PRESETS.get(req.emotion, (None, None))
    eff_temperature = _emo_t if _emo_t is not None else req.temperature
    eff_guidance = _emo_g if _emo_g is not None else req.guidance_scale

    if use_clone:
        # Build the clone prompt from ONLY the speakers actually referenced in the
        # text (e.g. "[S3] ..." → just GYASI). Prepending every speaker bloats the
        # audio prompt and strangles output length. No tag → all speakers (back-compat).
        used = [s for s in ("S1", "S2", "S3") if f"[{s}]" in user_text and s in _speaker_refs]
        if not used:
            used = list(_speaker_refs.keys())
        full_text = "".join(f"[{s}] {_speaker_refs[s][0]} " for s in used) + user_text
        prompt_audio = np.concatenate([_speaker_refs[s][1] for s in used]).astype(np.float32)
    else:
        full_text = user_text
        prompt_audio = None

    def _work(h):
        """Blocking: prep inputs, generate, decode. Runs in a worker thread via sc.run so the
        event loop stays responsive — mirrors the original's per-step asyncio.to_thread calls,
        just bundled into one offload (same pattern as the colpali reference)."""
        if prompt_audio is not None:
            inputs = h.processor(
                text=[full_text], audio=[prompt_audio], padding=True, return_tensors="pt"
            ).to(sc.device)
        else:
            inputs = h.processor(
                text=[full_text], padding=True, return_tensors="pt"
            ).to(sc.device)
        with torch.no_grad():
            outputs = h.model.generate(
                **inputs,
                max_new_tokens=req.max_new_tokens,
                guidance_scale=eff_guidance,
                temperature=eff_temperature,
                top_p=req.top_p,
                top_k=req.top_k,
            )
        return h.processor.batch_decode(outputs)

    t0 = time.perf_counter()
    try:
        async with sc.job() as h:              # queue slot + busy guard + lazy (re)load
            decoded = await sc.run(_work, h)   # blocking model call off the event loop
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"synthesis failed: {e}")
    elapsed = time.perf_counter() - t0

    audio = decoded[0]
    if isinstance(audio, torch.Tensor):
        audio = audio.detach().cpu().numpy()
    audio_np = np.asarray(audio).squeeze()

    # When cloning, the output prepends the reference audio. Crop it.
    # Each audio second is ~44_100 samples at Dia's native rate.
    if use_clone and prompt_audio is not None:
        prefix_samples = len(prompt_audio)
        if len(audio_np) > prefix_samples:
            audio_np = audio_np[prefix_samples:]

    sample_rate = 44_100
    # Optional pitch-preserved pacing (slow/enunciated reads). Never fatal.
    if abs(req.speed - 1.0) > 1e-3:
        try:
            audio_np = _apply_speed(audio_np, sample_rate, req.speed)
        except Exception as e:
            print(f"[tts] speed stretch failed ({e}); returning native-rate audio", flush=True)
    buf = io.BytesIO()
    sf.write(buf, audio_np, sample_rate, format="WAV", subtype="PCM_16")
    buf.seek(0)
    print(f"[tts] chars={len(req.text)} {elapsed:.2f}s clone={use_clone}", flush=True)
    return Response(
        content=buf.read(),
        media_type="audio/wav",
        headers={
            "x-synth-seconds": f"{elapsed:.3f}",
            "x-engine": "dia-1.6b",
            "x-voice-clone": "on" if use_clone else "off",
        },
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
