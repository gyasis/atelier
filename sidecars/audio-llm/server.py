"""
audio-llm-sidecar — MULTI-MODEL audio UNDERSTANDING for the Mac Studio hub (MLX).

Classifies/describes an audio clip with a switchable audio-language model. Primary use:
tell a CLEAN dialogue track from an AUDIO-DESCRIPTION / commentary / dubbed track (robust
when container titles are missing/wrong), and describe scene audio for lessons.

MULTI-MODEL: one model resident at a time, hot-swapped per request `model=<alias>` (like the
llamacpp lane). Weights cache to the Mac disk (HF_HOME) so switching is download-once.
  qwen2-audio          mlx-community/Qwen2-Audio-7B-Instruct-4bit   (works today; transcribes+understands)
  voxtral              mlx-community/Voxtral-Mini-3B-2507-bf16       (newest instruct audio Q&A; needs mistral-common)
  qwen3-omni-captioner mlx-community/Qwen3-Omni-30B-A3B-Captioner…   (best rich captioner; scene-desc, no prompt)
Override/add via env AUDIO_LLM_MODELS (json alias→repo) + AUDIO_LLM_DEFAULT.

Governed like pyannote: /healthz /readyz /agent /models /admin/unload + bursty COOLDOWN +
self-reclaim (R-AG4b MPS leak → idle self-restart to cold/~0).

INPUT (one of): file=@clip.wav | path=/Users/…/a.wav | url=https://…/a.mp3   (+ model=<alias>)
Endpoints:
  GET  /models    → {default, loaded, registry}
  POST /classify  → {track_type: clean_dialogue|audio_description|commentary|dubbed|music|other, raw, model}
  POST /describe  {prompt?} → {text, model}
Env: AUDIO_LLM_DEFAULT · AUDIO_LLM_MODELS(json) · IDLE_UNLOAD_S(180) · RECLAIM_THRESHOLD_GB(1.0) · KEEP_WARM
"""
import os, sys, time, json, tempfile, threading, subprocess, urllib.request
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import JSONResponse
import uvicorn

REGISTRY = {
    "qwen2-audio": "mlx-community/Qwen2-Audio-7B-Instruct-4bit",
    "voxtral": "mlx-community/Voxtral-Mini-3B-2507-bf16",
    "qwen3-omni-captioner": "mlx-community/Qwen3-Omni-30B-A3B-Captioner-4bit",
}
try:
    REGISTRY.update(json.loads(os.getenv("AUDIO_LLM_MODELS", "{}")))
except Exception:
    pass
DEFAULT = os.getenv("AUDIO_LLM_DEFAULT", "qwen2-audio")
KEEP_WARM = os.getenv("KEEP_WARM", "false").lower() in ("1", "true", "yes")
IDLE_UNLOAD_S = float(os.getenv("IDLE_UNLOAD_S", "180"))
RECLAIM_GB = float(os.getenv("RECLAIM_THRESHOLD_GB", "1.0"))

CLASSIFY_PROMPT = (
    "This is an audio clip from a TV episode's audio track. Classify it as exactly ONE of: "
    "clean_dialogue (original actors' dialogue + ambient, no narrator describing visuals) | "
    "audio_description (a narrator describes on-screen action/visuals between/over dialogue) | "
    "commentary (director/cast commentary) | dubbed (dialogue in a different language) | music. "
    "Answer with the category word first, then a short reason."
)
_CATS = ["audio_description", "clean_dialogue", "commentary", "dubbed", "music"]

app = FastAPI(title="audio-llm-sidecar")
_model = None
_alias = None
_lock = threading.Lock()
_last = time.time()
_busy = False


def _rss_gb():
    try:
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e9
    except Exception:
        return 0.0


def _load(alias):
    global _model, _alias
    if alias not in REGISTRY:
        raise HTTPException(400, f"unknown model '{alias}'. known: {list(REGISTRY)}")
    if _model is not None and _alias == alias:
        return _model
    if _model is not None:            # swap: drop the resident model first
        _drop()
    from mlx_audio.stt.utils import load_model
    _model = load_model(REGISTRY[alias]); _alias = alias
    return _model


def _to_wav(src, dst):
    subprocess.run(["ffmpeg", "-y", "-i", src, "-vn", "-ac", "1", "-ar", "16000", dst],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _infer(wav, prompt, alias):
    from mlx_audio.stt.generate import generate_transcription
    with tempfile.TemporaryDirectory() as od:
        out = generate_transcription(model=_load(alias), audio=wav, text=prompt,
                                     output_path=os.path.join(od, "o"), verbose=False)
    return (getattr(out, "text", None) or str(out)).strip()


def _drop():
    global _model, _alias
    _model = None; _alias = None
    try:
        import mlx.core as mx, gc
        gc.collect(); mx.clear_cache()
    except Exception:
        pass


def _unload(reason="admin"):
    _drop()
    rss = _rss_gb()
    if rss > RECLAIM_GB:   # R-AG4b: MPS won't return RSS on soft-unload → hard restart reclaims
        threading.Thread(target=lambda: (time.sleep(0.3), os._exit(42)), daemon=True).start()
        return {"unloaded": True, "reason": reason, "reclaim": "process-restart", "rss_gb": round(rss, 2)}
    return {"unloaded": True, "reason": reason, "reclaim": "clear_cache", "rss_gb": round(rss, 2)}


def _idle_watch():
    while True:
        time.sleep(60)
        if _model is not None and not _busy and not KEEP_WARM and time.time() - _last > IDLE_UNLOAD_S:
            _unload("idle")


@app.get("/healthz")
def healthz(): return {"ok": True}

@app.get("/readyz")
def readyz():
    return {"ok": True, "state": "warm" if _model is not None else "cold", "busy": _busy,
            "loaded": _alias, "default": DEFAULT, "idle_unload_s": IDLE_UNLOAD_S,
            "keep_warm": KEEP_WARM, "rss_gb": round(_rss_gb(), 2)}

@app.get("/models")
def models(): return {"default": DEFAULT, "loaded": _alias, "registry": REGISTRY}

@app.get("/agent")
def agent():
    return {"name": "audio-llm-sidecar", "purpose": "multi-model audio classification/description (MLX)",
            "models": "GET /models", "classify": "POST /classify [model=] → {track_type, raw}",
            "describe": "POST /describe [model=] {prompt} → {text}", "default": DEFAULT, "registry": REGISTRY}

@app.post("/admin/unload")
def admin_unload(): return _unload("admin")


async def _read_audio(file, path, url, td):
    if file is not None:
        src = os.path.join(td, "in")
        with open(src, "wb") as f: f.write(await file.read())
    elif url:
        src = os.path.join(td, "in"); urllib.request.urlretrieve(url, src)
    elif path:
        if not os.path.isfile(path): raise HTTPException(404, f"path not found on mac: {path}")
        src = path
    else:
        raise HTTPException(400, "provide one of file= | path= | url=")
    wav = os.path.join(td, "audio.wav"); _to_wav(src, wav)
    return wav


@app.post("/classify")
async def classify(file: UploadFile = File(None), path: str = Form(None), url: str = Form(None),
                   model: str = Form(None)):
    global _last, _busy
    alias = model or DEFAULT
    with tempfile.TemporaryDirectory(prefix="audiollm_") as td:
        wav = await _read_audio(file, path, url, td)
        with _lock:
            _busy = True
            try: raw = _infer(wav, CLASSIFY_PROMPT, alias)
            finally: _busy = False; _last = time.time()
    low = raw.lower()
    track_type = next((c for c in _CATS if c.replace("_", " ") in low or c in low), "other")
    return JSONResponse({"track_type": track_type, "raw": raw, "model": alias})


@app.post("/describe")
async def describe(file: UploadFile = File(None), path: str = Form(None), url: str = Form(None),
                   model: str = Form(None),
                   prompt: str = Form("Describe what happens in this audio in one or two sentences.")):
    global _last, _busy
    alias = model or DEFAULT
    with tempfile.TemporaryDirectory(prefix="audiollm_") as td:
        wav = await _read_audio(file, path, url, td)
        with _lock:
            _busy = True
            try: text = _infer(wav, prompt, alias)
            finally: _busy = False; _last = time.time()
    return JSONResponse({"text": text, "model": alias})


if __name__ == "__main__":
    threading.Thread(target=_idle_watch, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8768")))
