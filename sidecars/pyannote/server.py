"""
pyannote-sidecar — speaker DIARIZATION for the Mac Studio inference hub.

Sibling of the whisper sidecar (:8766). Answers "who spoke when" so Chiron's
video-episode lesson can attribute each transcript line to a character. Runs
`pyannote/speaker-diarization-3.1` (gated — needs HF_TOKEN + accepted terms on
BOTH pyannote/speaker-diarization-3.1 AND pyannote/segmentation-3.0).

MPS on Apple Silicon (PYTORCH_ENABLE_MPS_FALLBACK=1), one pipeline resident,
idle-unload + hard RSS reclaim (R-AG4b: MPS python sidecars leak; exit 42 →
launchd KeepAlive{SuccessfulExit:false} relaunches cold).

INPUT (pick one):
    file=@clip.wav      multipart upload
    path=/Users/…/a.wav a file already on the Mac
    url=https://…/a.mp3 a link the sidecar pulls down
  optional: num_speakers | min_speakers | max_speakers (ints)

OUTPUT:  {"segments":[{"start":s,"end":s,"speaker":"SPEAKER_00"}], "num_speakers":n, "duration":s}

Endpoints: GET /healthz /readyz /agent · POST /admin/unload · POST /diarize
Env: HF_TOKEN (required) · PYANNOTE_MODEL (default pyannote/speaker-diarization-3.1)
     KEEP_WARM · IDLE_UNLOAD_S (default 900) · RECLAIM_THRESHOLD_GB (default 2.0)
"""
import os, sys, time, tempfile, threading, subprocess, urllib.request
from contextlib import asynccontextmanager
from pathlib import Path

# Secrets live in the Mac's ~/dev/.env (R-MAC8) — load it so HF_TOKEN isn't
# duplicated into the launchd plist.
try:
    from dotenv import load_dotenv
    load_dotenv(Path.home() / "dev" / ".env", override=False)
except Exception:
    pass

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import JSONResponse
import uvicorn

MODEL = os.getenv("PYANNOTE_MODEL", "pyannote/speaker-diarization-community-1")  # pyannote.audio 4.x latest

def _resolve_hf_token():
    t = (os.getenv("HF_TOKEN") or os.getenv("HUGGINGFACE_TOKEN")
         or os.getenv("HUGGINGFACE_HUB_TOKEN"))
    if t:
        return t.strip()
    # Fall back to the standard `huggingface-cli login` cached token.
    for p in (Path.home() / ".cache" / "huggingface" / "token",
              Path.home() / ".huggingface" / "token"):
        try:
            if p.is_file():
                v = p.read_text().strip()
                if v:
                    return v
        except Exception:
            pass
    return None

HF_TOKEN = _resolve_hf_token()
KEEP_WARM = os.getenv("KEEP_WARM", "false").lower() in ("1", "true", "yes")
# pyannote is a BURSTY BATCH workload (diarize an episode, then idle for hours). Short
# cooldown + full self-reclaim: after COOLDOWN idle, self-restart to cold/~0 RSS (MPS won't
# release any other way — R-AG4b). Keeps it below the governor's 1.5GB autoheal FLOOR so the
# governor never needs to kickstart it, and frees the memory when unused. Back-to-back
# episodes still reuse the warm model within the cooldown window.
IDLE_UNLOAD_S = float(os.getenv("IDLE_UNLOAD_S", "180"))          # 3-min cooldown (was 900)
RECLAIM_GB = float(os.getenv("RECLAIM_THRESHOLD_GB", "0.8"))       # reclaim after any real use (was 2.0)

@asynccontextmanager
async def _lifespan(app):
    """Start the idle-unload watcher — under UVICORN, not just `python server.py`.

    This used to live ONLY in the `if __name__ == "__main__"` block below. launchd runs
    `python -m uvicorn server:app`, which IMPORTS this file (__name__ == "server"), so that
    block never executed: the watcher thread never started and IDLE_UNLOAD_S was dead
    config. pyannote loaded on the first diarize and then stayed resident forever — found
    warm holding ~2 GB with the machine already swapping. A lifespan runs on BOTH paths.
    """
    threading.Thread(target=_idle_watch, daemon=True).start()
    print(f"[pyannote] idle-watcher started (unload after {IDLE_UNLOAD_S}s idle, "
          f"keep_warm={KEEP_WARM})", flush=True)
    if KEEP_WARM and HF_TOKEN:
        try:
            _load()
        except Exception as e:
            print(f"[pyannote] warm preload failed: {e}", file=sys.stderr, flush=True)
    yield


app = FastAPI(title="pyannote-sidecar", lifespan=_lifespan)
_pipeline = None
_device = None
_lock = threading.Lock()
_last_used = time.time()
_busy = False


def _rss_gb() -> float:
    try:
        import resource
        # macOS ru_maxrss is bytes
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e9
    except Exception:
        return 0.0


def _load():
    global _pipeline, _device
    if _pipeline is not None:
        return _pipeline
    if not HF_TOKEN:
        raise HTTPException(503, "HF_TOKEN not set — cannot load gated pyannote model. "
                                 "Put a HuggingFace token (terms accepted) in the Mac ~/dev/.env.")
    import torch
    from pyannote.audio import Pipeline
    tok = HF_TOKEN or True  # True => use the cached `huggingface-cli login` token
    try:
        pipe = Pipeline.from_pretrained(MODEL, token=tok)               # pyannote.audio 3.1+/4.x
    except TypeError:
        pipe = Pipeline.from_pretrained(MODEL, use_auth_token=HF_TOKEN)  # older API
    _device = "mps" if torch.backends.mps.is_available() else "cpu"
    try:
        pipe.to(torch.device(_device))
    except Exception:
        _device = "cpu"
        pipe.to(torch.device("cpu"))
    _pipeline = pipe
    return _pipeline


def _to_wav(src: str, dst: str):
    subprocess.run(["ffmpeg", "-y", "-i", src, "-vn", "-ac", "1", "-ar", "16000", dst],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _idle_watch():
    while True:
        time.sleep(60)
        if _pipeline is None or _busy or KEEP_WARM:
            continue
        if time.time() - _last_used > IDLE_UNLOAD_S:
            _unload(reason="idle")


def _unload(reason="admin") -> dict:
    global _pipeline, _device
    with _lock:
        _pipeline = None
        _device = None
    try:
        import torch, gc
        gc.collect()
        if torch.backends.mps.is_available():
            torch.mps.empty_cache()
    except Exception:
        pass
    rss = _rss_gb()
    # R-AG4b: MPS python sidecars don't return RSS to the OS on soft-unload → hard restart.
    if rss > RECLAIM_GB:
        # exit 42 → launchd KeepAlive{SuccessfulExit:false} relaunches cold.
        def _die():
            time.sleep(0.3)
            os._exit(42)
        threading.Thread(target=_die, daemon=True).start()
        return {"unloaded": True, "reason": reason, "reclaim": "process-restart", "rss_gb": round(rss, 2)}
    return {"unloaded": True, "reason": reason, "reclaim": "empty_cache", "rss_gb": round(rss, 2)}


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/readyz")
def readyz():
    return {
        "ok": True,
        "state": "warm" if _pipeline is not None else "cold",
        "busy": _busy,
        "model": MODEL,
        "device": _device,
        "hf_token": bool(HF_TOKEN),
        "idle_unload_s": IDLE_UNLOAD_S,
        "keep_warm": KEEP_WARM,
        "rss_gb": round(_rss_gb(), 2),
    }


@app.get("/agent")
def agent():
    return {
        "name": "pyannote-sidecar",
        "role": "Diarization — speaker diarization (pyannote.audio): who-spoke-when (MPS)",
        "purpose": "speaker diarization (who spoke when)",
        "summary": (
            "Splits an audio track into speaker turns: WHO spoke WHEN. It does NOT "
            "transcribe (pair it with the whisper sidecar) and it does NOT know anyone's "
            "name — it returns anonymous cluster labels SPEAKER_00, SPEAKER_01, … "
            "Also returns a voice EMBEDDING per speaker, which is how the same voice is "
            "matched across separate files (scenes, episodes)."
        ),
        "auth": "none required on the LAN",
        "model": MODEL,
        "input_modes": {
            "file": "multipart upload of the audio/video bytes (form field `file`)",
            "url":  "a link this sidecar downloads itself (form field `url`)",
            "path": "absolute path to a file already on THIS Mac (no upload). A path "
                    "that exists only on the calling machine gives 404 — upload instead.",
            "rule": "supply EXACTLY ONE of file/url/path per request",
            "formats": "anything ffmpeg can decode (mkv/mp4/opus/wav/…); it is converted "
                       "to wav internally, so extracting audio first is optional — but a "
                       "small mono 16 kHz opus uploads far faster than a multi-GB video",
        },
        "methods": [{
            "name": "diarize",
            "http": "POST /diarize",
            "encoding": "multipart/form-data",
            "params": {
                "file|url|path": "the audio source (exactly one)",
                "num_speakers": "EXACT speaker count, when you know it — most accurate",
                "min_speakers": "lower bound when the count is unknown",
                "max_speakers": "upper bound when the count is unknown",
                "note": "omit all three to let the model decide; 0 is treated as unset",
            },
            "output": {
                "segments": "[{start, end, speaker}] — seconds, sorted by start; one entry "
                            "per speaker TURN, not per sentence. Turns can be adjacent and "
                            "may overlap when people talk over each other.",
                "num_speakers": "how many distinct speakers were found",
                "speakers": "sorted list of the labels, e.g. ['SPEAKER_00','SPEAKER_01']",
                "duration": "end of the last segment, seconds",
                "embeddings": "{label: [float,…]} voice embedding per speaker. Compare with "
                              "cosine similarity to decide whether SPEAKER_00 in file A is "
                              "the same person as SPEAKER_03 in file B. May be {} on older "
                              "pyannote builds — treat as optional.",
            },
        }],
        "recipes": [
            {"goal": "Who spoke when in one file",
             "do": "POST /diarize with file=@audio.opus"},
            {"goal": "Two-hander interview / dialogue scene",
             "do": "POST /diarize with num_speakers=2 — pinning the count sharply improves labels"},
            {"goal": "Unknown cast size",
             "do": "POST /diarize with min_speakers=2 max_speakers=8 rather than leaving it open"},
            {"goal": "Attribute an existing transcript",
             "do": "diarize the same audio, then for each transcript cue assign the speaker "
                   "whose segment has the greatest time OVERLAP with that cue"},
            {"goal": "Same character across episodes",
             "do": "diarize each episode, then cosine-match the per-speaker `embeddings` to "
                   "link SPEAKER_xx across files; label once, reuse everywhere"},
            {"goal": "Turn labels into character names",
             "do": "diarization NEVER yields names. Map clusters to a cast list yourself, or "
                   "have an LLM infer from dialogue where characters address each other"},
        ],
        "instructions": [
            "1) Pick ONE input mode: upload (file), link (url), or a path on this Mac (path).",
            "2) Constrain the speaker count if you can — num_speakers when known, else "
            "min_speakers/max_speakers. Unconstrained runs over- or under-split more often.",
            "3) Expect SPEAKER_xx labels, never names. Naming is a separate step.",
            "4) To attribute a transcript, merge by maximum time overlap per cue.",
            "5) To follow a voice across files, cosine-compare `embeddings`.",
            "6) GET /readyz for state (cold|warm), model, hf_token and rss_gb; "
            "POST /admin/unload to free memory now.",
        ],
        "limits": {
            "concurrency": "ONE diarization at a time — the model is lock-guarded, so "
                           "concurrent posts queue rather than run in parallel",
            "cold_start": "the model loads on first use; a cold call pays that once",
            "idle_unload_s": IDLE_UNLOAD_S,
            "runtime": "roughly 0.1–0.3x realtime on MPS — budget minutes, not seconds, "
                       "for a feature-length file",
            "requires": "HF_TOKEN must be set (the model is gated on Hugging Face); "
                        "GET /readyz reports hf_token true/false",
            "not_this": "no transcription, no translation, no speaker NAMES, no "
                        "language detection — use the whisper sidecar for text",
        },
    }


@app.post("/admin/unload")
def admin_unload():
    return _unload(reason="admin")


@app.post("/diarize")
async def diarize(
    file: UploadFile = File(None),
    path: str = Form(None),
    url: str = Form(None),
    num_speakers: int = Form(None),
    min_speakers: int = Form(None),
    max_speakers: int = Form(None),
):
    global _last_used, _busy
    if not (file or path or url):
        raise HTTPException(400, "provide one of file= | path= | url=")

    with tempfile.TemporaryDirectory(prefix="pyannote_") as td:
        src = os.path.join(td, "in")
        if file is not None:
            with open(src, "wb") as f:
                f.write(await file.read())
        elif url:
            urllib.request.urlretrieve(url, src)
        else:
            if not os.path.isfile(path):
                raise HTTPException(404, f"path not found on mac: {path}")
            src = path
        wav = os.path.join(td, "audio.wav")
        try:
            _to_wav(src, wav)
        except subprocess.CalledProcessError:
            raise HTTPException(422, "ffmpeg failed to decode audio")

        kw = {k: v for k, v in
              {"num_speakers": num_speakers, "min_speakers": min_speakers, "max_speakers": max_speakers}.items()
              if v}
        with _lock:
            _busy = True
            try:
                pipe = _load()
                output = pipe(wav, **kw)
            finally:
                _busy = False
                _last_used = time.time()

    # pyannote.audio 4.x returns an output object (.speaker_diarization);
    # 3.x returns the Annotation directly. Handle both.
    diar = getattr(output, "speaker_diarization", output)
    segments = [{"start": round(t.start, 3), "end": round(t.end, 3), "speaker": spk}
                for t, _, spk in diar.itertracks(yield_label=True)]
    segments.sort(key=lambda s: s["start"])
    speakers = sorted({s["speaker"] for s in segments})
    dur = max((s["end"] for s in segments), default=0.0)

    # Per-speaker voice EMBEDDINGS (community-1: output.speaker_embeddings, rows aligned to
    # diar.labels()) — the cross-scene registry matches voices across scenes with these.
    emb = {}
    try:
        import numpy as np
        arr = getattr(output, "speaker_embeddings", None)
        if arr is not None:
            arr = np.asarray(arr)
            labels = list(diar.labels())
            for i, lab in enumerate(labels):
                if i < len(arr) and not np.isnan(arr[i]).any():
                    emb[lab] = [round(float(x), 5) for x in arr[i]]
    except Exception:
        emb = {}
    return JSONResponse({"segments": segments, "num_speakers": len(speakers),
                         "speakers": speakers, "duration": round(dur, 2), "embeddings": emb})


if __name__ == "__main__":
    # The watcher + warm preload now live in _lifespan, which uvicorn.run() fires too —
    # starting them here as well would run two watcher threads.
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8767")))
