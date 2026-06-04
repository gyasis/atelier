"""
whisper-sidecar — ASR (speech-to-text) for the Mac Studio inference hub.

Runs Whisper via `mlx-whisper` (Apple MLX). On an M1 Max the turbo model hits
RTF ~40-50x (12 min of audio transcribed in ~14-18s), 30-50% faster than
whisper.cpp with no C++ build chain. See docs/ARCHITECTURE.md §5.2.

MODEL — chosen per request, not hardcoded. Pass `model=` with an alias or a
full HF repo. The sidecar keeps ONE model resident and hot-swaps when a request
asks for a different one (memory courtesy — large-v3 ~3 GB vs turbo ~1.6 GB).
The governor learns a separate ETA per model, so a caller can trade speed
(turbo) against accuracy (large) on real numbers.
    model=turbo     -> mlx-community/whisper-large-v3-turbo   (default, fast)
    model=large     -> mlx-community/whisper-large-v3         (slower, accurate)
    model=accurate  -> mlx-community/whisper-large-v3
    model=<hf/repo> -> any mlx-community whisper repo
Default when omitted: $WHISPER_MODEL_REPO.

INPUT — three ways audio can arrive (pick exactly one per request):
    file=@clip.m4a      multipart upload (the audio bytes themselves)
    url=https://…/a.mp3 a link the sidecar pulls down itself
    path=/Users/…/a.wav a file already on the Mac (no copy, no upload)

OUTPUT — where the transcript goes:
    response body          always returned inline (json | text | srt | vtt | verbose_json)
    save=true              ALSO written to disk under WHISPER_OUTPUT_DIR, named by
                           the sha256 of the audio bytes (matches the gateway's
                           transcript cache key in ARCHITECTURE.md §4) — or to an
                           explicit output_path if given.
    x-content-sha256       header on every response — the cache key for that audio.

Endpoints:
    GET  /healthz              liveness
    GET  /readyz               status (warm/cold/busy), resident model, queue depth
    GET  /models               available aliases + which is loaded
    GET  /agent                self-describing manifest for AI agents (methods + how-to)
    POST /admin/unload         force-unload the model (memory-governor courtesy)
    POST /transcribe           sync; for clips under ~a few minutes
    POST /structure            LLM: detect format + reformat any transcript text
    POST /summarize            LLM: summarize any text at a 0–1 strength
    POST /transcribe/batch     async; returns {job_id} for long audio
    GET  /jobs/<id>            job status snapshot
    GET  /jobs/<id>/stream     SSE progress: queued -> running -> done/error
    GET  /jobs/<id>/result     the transcript once done
    DELETE /jobs/<id>          cancel a job (only effective before it starts running)

Env vars:
    WHISPER_MODEL_REPO    default model when a request omits `model`,
                          default "mlx-community/whisper-large-v3-turbo"
    WHISPER_OUTPUT_DIR    where save=true writes, default ~/outputs/transcripts
    WHISPER_MAX_PULL_MB   cap on url-pulled audio, default 512
    HF_HOME               model cache (shared hub cache)
    HUB_TOKEN             optional bearer token; if set, work endpoints require it
    IDLE_UNLOAD_SECONDS   unload after N idle seconds, default 300
    KEEP_WARM             "true" to disable idle-unload (default false — ASR is bursty)
"""

import asyncio
import gc
import hashlib
import json
import math
import os
import secrets
import statistics
import subprocess
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

import mlx_whisper

DEFAULT_MODEL = os.environ.get("WHISPER_MODEL_REPO", "mlx-community/whisper-large-v3-turbo")
# Caller-selectable models — the request decides which one Atelier loads.
# Aliases resolve to a LOCAL on-disk dir when WHISPER_{TURBO,LARGE}_PATH is set
# (preferred — mlx-whisper then loads from disk with zero network). Falls back to
# the HF repo name otherwise. Point these at ~/models/whisper/<dir> so nothing
# ever re-downloads.
_TURBO = os.environ.get("WHISPER_TURBO_PATH", "mlx-community/whisper-large-v3-turbo")
_LARGE = os.environ.get("WHISPER_LARGE_PATH", "mlx-community/whisper-large-v3-mlx")
MODEL_ALIASES = {
    "turbo": _TURBO,
    "large-turbo": _TURBO,
    "large": _LARGE,
    "large-v3": _LARGE,
    "accurate": _LARGE,
}
OUTPUT_DIR = Path(os.environ.get("WHISPER_OUTPUT_DIR", str(Path.home() / "outputs/transcripts")))
MAX_PULL_BYTES = int(os.environ.get("WHISPER_MAX_PULL_MB", "512")) * 1024 * 1024
HUB_TOKEN = os.environ.get("HUB_TOKEN")
# Optional LLM post-processing (structure detection + summarization) runs against
# the hub's Ollama. Off unless a request asks for it — it wakes a ~20 GB model.
# The model is also per-request selectable (llm_model=), defaulting here.
LLM_URL = os.environ.get("WHISPER_LLM_URL", "http://127.0.0.1:11434")
LLM_MODEL = os.environ.get("WHISPER_LLM_MODEL", "qwen3:32b")
LLM_TIMEOUT = float(os.environ.get("WHISPER_LLM_TIMEOUT", "300"))
# Constitutional idle-unload (see kokoro/dia): a multi-GB model squatting on
# unified memory while idle starves video/LLM jobs. Default unload after 5 min.
# KEEP_WARM=true opts out (e.g. a long batch transcription session).
IDLE_UNLOAD_SECONDS = int(os.environ.get("IDLE_UNLOAD_SECONDS", "300"))
KEEP_WARM = os.environ.get("KEEP_WARM", "false").lower() in ("1", "true", "yes")
IDLE_TICK_SECONDS = 30

# mlx-whisper loads via an lru_cache'd loader; we track load state ourselves
# since that cache is opaque. _loaded_model = the HF repo currently resident
# (None when unloaded). Exactly one model is kept resident at a time.
_loaded_model: str | None = None
_sem = asyncio.Semaphore(1)            # single-flight: MLX is single-stream on the GPU
_last_request_at = time.monotonic()
_unload_task: asyncio.Task | None = None
_idle_unloaded_at: float | None = None
# Job-aware state (memory governor): _active = transcriptions in flight (busy when
# >0), _waiting = callers queued on the semaphore. The idle-watcher must NEVER
# unload while _active>0 or _waiting>0 — a long batch must not be reaped mid-job.
_active: int = 0
_waiting: int = 0

# Batch job registry. Jobs are in-process only (lost on restart) — fine for v1;
# the gateway owns durable caching by sha256. id -> job dict.
_jobs: dict[str, dict] = {}


def _resolve_model(name: str | None) -> str:
    """Map a caller's `model` (alias or full repo) to an HF repo. None -> default."""
    if not name:
        return DEFAULT_MODEL
    return MODEL_ALIASES.get(name.strip().lower(), name.strip())


# ---------- model lifecycle ----------
def _mlx_clear_cache() -> None:
    """Best-effort free of MLX's Metal buffer cache across mlx versions."""
    try:
        import mlx.core as mx
        if hasattr(mx, "clear_cache"):
            mx.clear_cache()
        elif hasattr(mx, "metal") and hasattr(mx.metal, "clear_cache"):
            mx.metal.clear_cache()
    except Exception:
        pass


def _gpu_mem_gb() -> float | None:
    """Real resident model memory (GB) from MLX's Metal allocator — accurate on
    Apple Silicon UMA where ps RSS undercounts wired GPU memory. active + cache =
    what this process actually holds; ~0 once the model idle-unloads. The APIs
    moved between MLX releases (mx.metal.* → mx.*), so probe both."""
    try:
        import mlx.core as mx
        metal = getattr(mx, "metal", None)
        get_active = getattr(mx, "get_active_memory", None) or getattr(metal, "get_active_memory", None)
        get_cache = getattr(mx, "get_cache_memory", None) or getattr(metal, "get_cache_memory", None)
        if get_active is None:
            return None
        total = get_active() + (get_cache() if get_cache else 0)
        return round(total / 1e9, 2)
    except Exception:
        return None


async def _load_and_warm(repo: str = DEFAULT_MODEL) -> None:
    """Cold-load `repo` into mlx-whisper's loader cache + warm it. Idempotent
    for the same repo. Caller must hold _sem."""
    global _loaded_model, _idle_unloaded_at
    if _loaded_model == repo:
        return
    t0 = time.perf_counter()
    print(f"[whisper] loading {repo}", flush=True)
    try:
        await asyncio.to_thread(mlx_whisper.load_models.load_model, repo)
        _loaded_model = repo
        _idle_unloaded_at = None
        print(f"[whisper] model warm in {time.perf_counter()-t0:.1f}s ({repo})", flush=True)
    except Exception as e:
        _loaded_model = None
        print(f"[whisper] load failed for {repo}: {e}", flush=True)


async def _unload_model() -> None:
    """Drop the resident model by clearing mlx-whisper's loader cache. Caller
    must hold _sem."""
    global _loaded_model, _idle_unloaded_at
    if _loaded_model is None:
        return
    print(f"[whisper] idle-unload — clearing model cache ({_loaded_model})", flush=True)
    try:
        # The loader is @lru_cache'd; clearing it drops every cached model.
        if hasattr(mlx_whisper.load_models.load_model, "cache_clear"):
            mlx_whisper.load_models.load_model.cache_clear()
    except Exception as e:
        print(f"[whisper] cache_clear failed: {e}", flush=True)
    _loaded_model = None
    _idle_unloaded_at = time.monotonic()
    gc.collect()
    _mlx_clear_cache()


async def _idle_watcher() -> None:
    if KEEP_WARM:
        print("[whisper] idle-watcher disabled (KEEP_WARM=true)", flush=True)
        return
    print(f"[whisper] idle-watcher active (unload after {IDLE_UNLOAD_SECONDS}s idle)", flush=True)
    while True:
        await asyncio.sleep(IDLE_TICK_SECONDS)
        if _loaded_model is None:
            continue
        if _active == 0 and _waiting == 0 and time.monotonic() - _last_request_at > IDLE_UNLOAD_SECONDS:
            async with _sem:
                if _loaded_model is not None and _active == 0:
                    await _unload_model()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _unload_task
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    async with _sem:
        await _load_and_warm(DEFAULT_MODEL)
    _unload_task = asyncio.create_task(_idle_watcher())
    yield
    if _unload_task and not _unload_task.done():
        _unload_task.cancel()
        try:
            await _unload_task
        except asyncio.CancelledError:
            pass


app = FastAPI(lifespan=lifespan)


def _check_auth(request: Request) -> None:
    if not HUB_TOKEN:
        return
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer ") or auth[7:] != HUB_TOKEN:
        raise HTTPException(401, "invalid bearer token")


# ---------- source resolution (file | url | path) ----------
async def _resolve_source(
    file: UploadFile | None, url: str | None, path: str | None
) -> tuple[str, bytes, bool]:
    """Turn whichever input arrived into (local_path, raw_bytes, is_temp).

    Exactly one of file/url/path must be provided. raw_bytes is what we sha256
    for the cache key + output naming. is_temp marks files we must clean up.
    """
    provided = [k for k, v in (("file", file), ("url", url), ("path", path)) if v]
    if len(provided) == 0:
        raise HTTPException(400, "provide exactly one of: file (upload), url (link), path (local file)")
    if len(provided) > 1:
        raise HTTPException(400, f"provide only one source, got: {', '.join(provided)}")

    if file is not None:
        data = await file.read()
        if not data:
            raise HTTPException(400, "uploaded file is empty")
        suffix = Path(file.filename or "audio").suffix or ".bin"
        fd, tmp = tempfile.mkstemp(prefix="whisper-up-", suffix=suffix)
        os.write(fd, data)
        os.close(fd)
        return tmp, data, True

    if url is not None:
        try:
            chunks = []
            total = 0
            async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
                async with client.stream("GET", url) as resp:
                    resp.raise_for_status()
                    suffix = Path(httpx.URL(url).path).suffix or ".bin"
                    fd, tmp = tempfile.mkstemp(prefix="whisper-pull-", suffix=suffix)
                    with os.fdopen(fd, "wb") as out:
                        async for chunk in resp.aiter_bytes():
                            total += len(chunk)
                            if total > MAX_PULL_BYTES:
                                out.close()
                                os.unlink(tmp)
                                raise HTTPException(413, f"pulled audio exceeds {MAX_PULL_BYTES // (1024*1024)} MB cap")
                            out.write(chunk)
                            chunks.append(chunk)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(502, f"could not pull url: {e}")
        return tmp, b"".join(chunks), True

    # path: a file already on the Mac — no copy.
    p = Path(path).expanduser()
    if not p.is_file():
        raise HTTPException(400, f"path not found on host: {p}")
    return str(p), p.read_bytes(), False


# ---------- audio pre-processing (ffmpeg leveling for quiet/uneven audio) ----------
# Whisper does its own internal scaling, but genuinely quiet or wildly uneven
# recordings still transcribe better after loudness leveling. These run via
# ffmpeg BEFORE the model (CPU work, off the GPU lock). Opt-in per request.
NORMALIZE_FILTERS = {
    # EBU R128 broadcast loudness — consistent target level, great default.
    "loudnorm": "loudnorm=I=-16:TP=-1.5:LRA=11",
    # Dynamic normalizer — lifts quiet passages, smooths level swings.
    "dynaudnorm": "dynaudnorm=f=150:g=15",
    # ffmpeg's purpose-built speech leveler.
    "speechnorm": "speechnorm=e=12.5:r=0.0001:l=1",
    # Speech cleanup chain: cut low rumble -> dynamic-normalize -> peak-limit.
    "speech": "highpass=f=80,dynaudnorm=f=150:g=15,alimiter=limit=0.95",
}


def _ffmpeg_preprocess(in_path: str, normalize: str | None, gain_db: float | None) -> tuple[str, bool]:
    """Apply optional gain + a normalization filter via ffmpeg, writing a 16 kHz
    mono wav (whisper's native input). Returns (path, is_temp). No-op (returns the
    input untouched) when neither is requested."""
    filters: list[str] = []
    if gain_db:
        filters.append(f"volume={gain_db}dB")
    if normalize:
        mode = "speech" if normalize.strip().lower() in ("true", "1", "yes", "auto") else normalize.strip().lower()
        flt = NORMALIZE_FILTERS.get(mode)
        if flt is None:
            raise HTTPException(400, f"unknown normalize mode '{normalize}'; choose one of "
                                     f"{sorted(NORMALIZE_FILTERS)} or true/auto")
        filters.append(flt)
    if not filters:
        return in_path, False
    fd, out = tempfile.mkstemp(prefix="whisper-norm-", suffix=".wav")
    os.close(fd)
    cmd = ["ffmpeg", "-y", "-i", in_path, "-af", ",".join(filters),
           "-ar", "16000", "-ac", "1", out]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        try:
            os.unlink(out)
        except OSError:
            pass
        raise HTTPException(500, f"ffmpeg preprocess failed: {proc.stderr.decode(errors='replace')[-300:]}")
    return out, True


# ---------- transcript formatting + output ----------
def _ts(seconds: float, sep: str) -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def _to_srt(segments: list[dict]) -> str:
    out = []
    for i, seg in enumerate(segments, 1):
        out.append(str(i))
        out.append(f"{_ts(seg['start'], ',')} --> {_ts(seg['end'], ',')}")
        out.append(seg["text"].strip())
        out.append("")
    return "\n".join(out)


def _to_vtt(segments: list[dict]) -> str:
    out = ["WEBVTT", ""]
    for seg in segments:
        out.append(f"{_ts(seg['start'], '.')} --> {_ts(seg['end'], '.')}")
        out.append(seg["text"].strip())
        out.append("")
    return "\n".join(out)


def _trim_segments(segments: list[dict]) -> list[dict]:
    """Keep the fields a caller actually uses; drop tokens/logprobs noise."""
    return [
        {"id": s.get("id"), "start": s.get("start"), "end": s.get("end"),
         "text": s.get("text", "").strip()}
        for s in segments
    ]


def _format_body(result: dict, fmt: str):
    """Return (payload, media_type) for the requested response_format."""
    segs = result.get("segments", [])
    if fmt == "text":
        return result.get("text", "").strip(), "text/plain"
    if fmt == "srt":
        return _to_srt(segs), "application/x-subrip"
    if fmt == "vtt":
        return _to_vtt(segs), "text/vtt"
    if fmt == "verbose_json":
        return result, "application/json"
    # default "json": the lean {text, segments, language} shape from ARCHITECTURE §5.2
    return {"text": result.get("text", "").strip(),
            "language": result.get("language"),
            "segments": _trim_segments(segs)}, "application/json"


def _save_output(result: dict, sha: str, fmt: str, output_path: str | None) -> str:
    """Write the transcript to disk; return the path. Default name = sha256 of
    the audio (the gateway's cache key), so re-transcribing the same bytes maps
    to the same file."""
    ext = {"text": "txt", "srt": "srt", "vtt": "vtt"}.get(fmt, "json")
    if output_path:
        dest = Path(output_path).expanduser()
        dest.parent.mkdir(parents=True, exist_ok=True)
    else:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        dest = OUTPUT_DIR / f"{sha}.{ext}"
    body, _ = _format_body(result, fmt)
    if isinstance(body, (dict, list)):
        dest.write_text(json.dumps(body, ensure_ascii=False, indent=2))
    else:
        dest.write_text(body)
    return str(dest)


def _transcribe_sync(audio_path: str, repo: str, language: str | None,
                     initial_prompt: str | None, word_timestamps: bool) -> dict:
    """Blocking mlx-whisper call. Runs in a thread off the event loop. `repo` is
    already resident (pre-loaded under the semaphore), so transcribe's internal
    loader hits the cache."""
    return mlx_whisper.transcribe(
        audio_path,
        path_or_hf_repo=repo,
        language=language,
        initial_prompt=initial_prompt,
        word_timestamps=word_timestamps,
    )


async def _run_transcription(audio_path: str, repo: str, language: str | None,
                             initial_prompt: str | None, word_timestamps: bool,
                             audio_bytes: int):
    """Ensure `repo` is the resident model (hot-swap if needed), then transcribe —
    all under the single-flight semaphore so a swap can't race a job. Returns
    (result, elapsed_seconds) and emits the [asr] telemetry line the governor
    tails (tagged with the model, so ETAs are learned per model)."""
    global _last_request_at, _active, _waiting
    t0 = time.perf_counter()
    _waiting += 1
    async with _sem:
        _waiting -= 1
        _active += 1
        try:
            # The request decides which model is loaded. Swap if a different one
            # is resident; cold-load if nothing is.
            if _loaded_model != repo:
                if _loaded_model is not None:
                    await _unload_model()
                await _load_and_warm(repo)
                if _loaded_model != repo:
                    raise HTTPException(503, f"could not load model {repo}; see server logs")
            result = await asyncio.to_thread(
                _transcribe_sync, audio_path, repo, language, initial_prompt, word_timestamps
            )
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(500, f"transcription failed: {e}")
        finally:
            _active -= 1
            _last_request_at = time.monotonic()
    elapsed = time.perf_counter() - t0
    # Telemetry line the governor ingests (one observable pane). Tagged with the
    # model so the predictor learns turbo vs large separately. audio_s is the
    # natural ASR ETA unit (seconds of audio -> seconds of compute).
    segs = result.get("segments", [])
    audio_s = segs[-1]["end"] if segs else 0.0
    chars = len(result.get("text", ""))
    rtf = (audio_s / elapsed) if elapsed > 0 else 0.0
    short = repo.split("/")[-1]
    print(f"[asr] model={short} audio_s={audio_s:.1f} chars={chars} bytes={audio_bytes} "
          f"{elapsed:.2f}s rtf={rtf:.1f}x lang={result.get('language')}", flush=True)
    return result, elapsed


# ---------- LLM post-processing (structure + summarize) ----------
# These run AFTER transcription, OUTSIDE the whisper semaphore, so a slow LLM
# call never blocks another transcription. They call the hub's Ollama; the
# governor's Ollama log tailer picks the call up automatically. Opt-in only.
import re as _re

_THINK = _re.compile(r"<think>.*?</think>", _re.DOTALL | _re.IGNORECASE)


def _strip_think(s: str) -> str:
    """Drop <think>…</think> reasoning blocks some models (qwen3) emit."""
    return _THINK.sub("", s).strip()


def _extract_json(s: str) -> dict | None:
    """Best-effort: pull the first {...} object out of an LLM reply."""
    i, j = s.find("{"), s.rfind("}")
    if i == -1 or j <= i:
        return None
    try:
        return json.loads(s[i:j + 1])
    except Exception:
        return None


async def _llm_chat(system: str, user: str, model: str) -> str:
    """One non-streaming Ollama chat turn. Raises 502 if the LLM is unreachable
    so the caller learns post-processing failed (the transcript itself is fine)."""
    payload = {
        "model": model, "stream": False,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "options": {"temperature": 0.2},
    }
    try:
        async with httpx.AsyncClient(timeout=LLM_TIMEOUT) as client:
            r = await client.post(f"{LLM_URL}/api/chat", json=payload)
            r.raise_for_status()
            content = r.json().get("message", {}).get("content", "")
    except Exception as e:
        raise HTTPException(502, f"LLM post-processing failed ({model} @ {LLM_URL}): {e}")
    return _strip_think(content).strip()


def _analyze_signals(segments: list[dict]) -> dict:
    """Cheap acoustic/textual cues that hint at the audio's structure — fed to
    the LLM so type detection isn't blind to pauses and question density."""
    n = len(segments)
    questions = sum(1 for s in segments if s.get("text", "").strip().endswith("?"))
    long_pauses, max_gap = 0, 0.0
    for a, b in zip(segments, segments[1:]):
        gap = (b.get("start", 0.0) or 0.0) - (a.get("end", 0.0) or 0.0)
        if gap > 2.0:
            long_pauses += 1
        max_gap = max(max_gap, gap)
    dur = segments[-1].get("end", 0.0) if segments else 0.0
    return {"segments": n, "questions": questions, "long_pauses": long_pauses,
            "max_gap_s": round(max_gap, 1), "duration_s": round(dur, 1)}


async def _structure_text(text: str, signals: dict, hint: str | None, model: str) -> dict:
    """Detect the transcript's format and reformat it for that format WITHOUT
    summarizing — restructure only (speaker turns, Q/A, sections, paragraphs)."""
    system = (
        "You are a transcript editor. Given a spoken-audio transcript, (1) detect "
        "its format and (2) reformat it cleanly for that format. Do NOT summarize, "
        "shorten, or drop content — only restructure (speaker labels, Q/A pairs, "
        "section headers, paragraph breaks) and fix obvious transcription artifacts. "
        "Respond with ONLY a JSON object: {\"type\": one of "
        "[interview, monologue, lecture, news_report, conversation, other], "
        "\"formatted\": the restructured transcript as markdown}."
    )
    user = (f"Signals (cues from timing/text): {json.dumps(signals)}\n"
            f"Caller hint: {hint or 'none — detect it'}\n\nTranscript:\n{text}")
    raw = await _llm_chat(system, user, model)
    data = _extract_json(raw) or {}
    return {"detected_type": data.get("type", "other"),
            "structured": data.get("formatted", raw),
            "structure_model": model}


async def _summarize_text(text: str, weight: float, model: str) -> dict:
    """Summarize at an intensity set by weight (0=verbatim cleanup, 1=core ideas
    only). Keeps substantive ideas/decisions/specifics; drops filler."""
    weight = max(0.0, min(1.0, weight))
    if weight < 0.34:
        intensity = ("Light touch: keep nearly all substantive content and the "
                     "original structure; only remove filler words, false starts, "
                     "and verbatim repetition.")
    elif weight < 0.67:
        intensity = ("Moderate: condense to the key points as structured bullets / "
                     "short paragraphs, roughly half the length.")
    else:
        intensity = ("Aggressive: distill to a tight executive summary of only the "
                     "core ideas, decisions, and takeaways.")
    system = (
        "You summarize spoken-audio transcripts. Capture substantive ideas, "
        "decisions, facts, and conclusions; preserve key specifics (names, numbers, "
        "dates). Drop filler, hedging, small talk, and repetition — signal, not "
        "fluff. Output clean markdown."
    )
    user = (f"Summarization strength = {weight:.2f} (0=verbatim cleanup, 1=core "
            f"ideas only).\n{intensity}\n\nTranscript:\n{text}")
    return {"summary": await _llm_chat(system, user, model),
            "summary_weight": round(weight, 2), "summary_model": model}


async def _postprocess(result: dict, structure: str | None, summarize: float | None,
                       llm_model: str | None) -> dict:
    """Run whichever post-processing the request asked for; return the extra
    fields to merge into the response. Empty dict if nothing requested."""
    model = llm_model or LLM_MODEL
    text = result.get("text", "")
    out: dict = {}
    if structure is not None:
        hint = None if structure.strip().lower() in ("auto", "true", "1", "") else structure
        out.update(await _structure_text(text, _analyze_signals(result.get("segments", [])), hint, model))
    if summarize is not None:
        out.update(await _summarize_text(text, float(summarize), model))
    return out


# ---------- transcription quality scoring (so an agent can self-correct) ----------
# Whisper emits per-segment confidence stats. We roll them up into a `quality`
# block with a `low_confidence` flag and an explicit `suggestion` — the signal an
# agent reads to decide "this transcript is bad, retry with normalize/gain/model".
# Thresholds follow whisper-community norms (avg_logprob<-1.0, no_speech>0.6,
# compression_ratio>2.4 ≈ repetition/hallucination).
def _suggest(low: bool, normalized: bool, gained: bool) -> str | None:
    if not low:
        return None
    if not normalized:
        return "low confidence — retry with normalize=speech (add gain_db=6 if the audio is very faint)"
    if not gained:
        return "still low after normalize — retry adding gain_db=6, or model=large for higher accuracy"
    return "still low after normalize+gain — try model=large, or the audio may be too degraded / non-speech"


def _quality(result: dict, *, normalized: bool, gained: bool) -> dict:
    segs = result.get("segments", [])
    if not segs:
        return {"low_confidence": True, "confidence": 0.0, "segments": 0,
                "reasons": ["no speech segments returned"],
                "avg_logprob": None, "min_logprob": None,
                "max_no_speech_prob": None, "max_compression_ratio": None,
                "suggestion": _suggest(True, normalized, gained)}
    logs = [s["avg_logprob"] for s in segs if s.get("avg_logprob") is not None]
    nsp = [s["no_speech_prob"] for s in segs if s.get("no_speech_prob") is not None]
    crs = [s["compression_ratio"] for s in segs if s.get("compression_ratio") is not None]
    avg_logprob = round(statistics.mean(logs), 3) if logs else None
    min_logprob = round(min(logs), 3) if logs else None
    max_nsp = round(max(nsp), 3) if nsp else None
    max_cr = round(max(crs), 3) if crs else None
    # Text density catches the "Thank you."/empty hallucination on noisy/near-silent
    # audio — those look confident by logprob but drop almost all the speech.
    audio_s = (segs[-1].get("end") or 0.0)
    chars = len(result.get("text", "").strip())
    density = round(chars / audio_s, 2) if audio_s > 0 else 0.0
    reasons = []
    if avg_logprob is not None and avg_logprob < -1.0:
        reasons.append(f"low avg_logprob {avg_logprob}")
    if max_nsp is not None and max_nsp > 0.6:
        reasons.append(f"high no_speech_prob {max_nsp}")
    if max_cr is not None and max_cr > 2.4:
        reasons.append(f"high compression_ratio {max_cr} (possible repetition/hallucination)")
    if audio_s > 3.0 and density < 3.0:
        reasons.append(f"low text density ({density} chars/s over {audio_s:.0f}s — likely dropped speech or hallucination)")
    low = bool(reasons)
    return {
        "low_confidence": low,
        # avg_logprob → 0..1 proxy (exp): ~-0.1 good ≈0.90, ~-1.0 weak ≈0.37.
        "confidence": round(math.exp(avg_logprob), 3) if avg_logprob is not None else None,
        "avg_logprob": avg_logprob, "min_logprob": min_logprob,
        "max_no_speech_prob": max_nsp, "max_compression_ratio": max_cr,
        "text_density_cps": density, "audio_seconds": round(audio_s, 1),
        "segments": len(segs),
        "reasons": reasons,
        "suggestion": _suggest(low, normalized, gained),
    }


# ---------- HTTP: health ----------
@app.get("/healthz")
def healthz():
    return {"ok": True, "service": "whisper", "default_model": DEFAULT_MODEL,
            "loaded_model": _loaded_model, "version": "1.1"}


@app.get("/readyz")
def readyz():
    state = "warm" if _loaded_model else "cold"
    lifecycle = "cold" if not _loaded_model else ("busy" if _active > 0 else "idle")
    return {
        "ok": True,
        "state": state,
        "lifecycle": lifecycle,
        "busy": _active > 0,
        "active_jobs": _active,
        "queue_depth": _waiting,
        "warmed": _loaded_model is not None,
        "model": _loaded_model or DEFAULT_MODEL,
        "loaded_model": _loaded_model,
        "default_model": DEFAULT_MODEL,
        "device": "mps",
        "batch_jobs": len(_jobs),
        "idle_seconds": round(time.monotonic() - _last_request_at, 1),
        "idle_unload_seconds": IDLE_UNLOAD_SECONDS,
        "keep_warm": KEEP_WARM,
        "last_unload_ago_s": round(time.monotonic() - _idle_unloaded_at, 1) if _idle_unloaded_at else None,
    }


@app.get("/models")
def models(request: Request):
    """What can be requested, and what's resident right now. Lets a caller (or
    the dashboard) decide which model to ask for."""
    _check_auth(request)
    return {
        "aliases": MODEL_ALIASES,
        "default": DEFAULT_MODEL,
        "loaded": _loaded_model,
        "note": "pass model=<alias|hf/repo> to /transcribe; the sidecar hot-swaps to it",
    }


@app.get("/agent")
def agent(request: Request):
    """Self-describing manifest for AI agents. An agent fetches THIS first to
    learn — at runtime — every method, parameter, and recipe, without baked-in
    docs. Returns machine-readable `methods` plus a follow-along `instructions`
    block. (FastAPI also serves /openapi.json + /docs, but those are verbose;
    this is the curated, task-oriented version.)"""
    _check_auth(request)
    auth = ("send header `Authorization: Bearer <HUB_TOKEN>` on every request"
            if HUB_TOKEN else "none required (HUB_TOKEN not set)")
    return {
        "service": "whisper",
        "role": "ASR — speech-to-text, plus optional LLM structure/summarize",
        "summary": "Transcribe audio (file upload, URL, or local path). Pick the "
                   "model per request (fast turbo vs accurate large). Optionally "
                   "post-process the transcript: detect+reformat structure, or "
                   "summarize at a 0–1 strength.",
        "auth": auth,
        "models": {
            "aliases": MODEL_ALIASES,
            "loaded_now": _loaded_model,
            "default": DEFAULT_MODEL,
            "policy": "one model resident at a time; requesting a different one "
                      "hot-swaps (unload old, load new) — fast but adds ~1s.",
        },
        "input_modes": {
            "file": "multipart upload of the audio bytes (form field `file`)",
            "url": "a link the sidecar downloads itself (form/json field `url`)",
            "path": "absolute path to a file already on this host (no upload)",
            "rule": "supply EXACTLY ONE of file/url/path per request",
        },
        "methods": [
            {"name": "transcribe", "http": "POST /transcribe",
             "encoding": "multipart/form-data",
             "when": "clips up to ~a few minutes; want the result inline",
             "params": {
                 "file|url|path": "the audio source (exactly one)",
                 "model": "alias (turbo|large|accurate) or HF repo; default = turbo",
                 "language": "ISO code, else auto-detect",
                 "initial_prompt": "bias spelling/terminology",
                 "word_timestamps": "bool",
                 "response_format": "json | text | srt | vtt | verbose_json",
                 "save": "bool — also write to disk (sha256-named)",
                 "output_path": "explicit save destination",
                 "normalize": "loudnorm|dynaudnorm|speechnorm|speech (or true) — ffmpeg-level quiet/uneven audio before ASR",
                 "gain_db": "fixed dB boost (e.g. 6) applied before normalize",
                 "structure": "auto|interview|lecture|… → adds {detected_type, structured} (json only)",
                 "summarize": "0.0–1.0 → adds {summary, summary_weight} (json only)",
                 "llm_model": "override the Ollama model for structure/summarize",
             },
             "returns": "{text, language, segments[], quality{...}} (+ structured/summary if asked); "
                        "headers x-content-sha256, x-model, x-asr-confidence, x-asr-low-confidence",
             "example": "curl -s $URL/transcribe -F path=/abs/a.wav -F model=turbo -F summarize=0.7"},
            {"name": "transcribe_batch", "http": "POST /transcribe/batch",
             "encoding": "application/json",
             "when": "long audio (tens of minutes+); returns immediately",
             "params": "{url|path, model, response_format, save, structure, summarize, llm_model}",
             "returns": "{job_id} — then poll/stream the job",
             "example": "curl -s $URL/transcribe/batch -d '{\"path\":\"/abs/show.wav\",\"summarize\":0.5}'"},
            {"name": "job_status", "http": "GET /jobs/{id}", "returns": "status snapshot"},
            {"name": "job_stream", "http": "GET /jobs/{id}/stream",
             "returns": "SSE: status → heartbeat → result/error"},
            {"name": "job_result", "http": "GET /jobs/{id}/result", "returns": "transcript when done"},
            {"name": "job_cancel", "http": "DELETE /jobs/{id}",
             "returns": "cancels if not yet running"},
            {"name": "structure", "http": "POST /structure",
             "encoding": "application/json",
             "when": "reformat ANY transcript text you already have",
             "params": "{text, hint?: auto|interview|…, llm_model?}",
             "returns": "{detected_type, structured}"},
            {"name": "summarize", "http": "POST /summarize",
             "encoding": "application/json",
             "when": "summarize ANY text at a chosen strength",
             "params": "{text, weight: 0.0–1.0, llm_model?}",
             "returns": "{summary, summary_weight}"},
            {"name": "models", "http": "GET /models", "returns": "aliases + resident model"},
            {"name": "readyz", "http": "GET /readyz", "returns": "warm/cold/busy, queue depth"},
        ],
        "recipes": [
            {"goal": "Quick transcript of a voice memo",
             "do": "POST /transcribe with file=@memo.m4a (defaults: turbo, json)"},
            {"goal": "High-accuracy transcript of tricky audio",
             "do": "POST /transcribe with model=large"},
            {"goal": "Subtitle file",
             "do": "POST /transcribe with response_format=srt (or vtt), save=true"},
            {"goal": "Meeting notes from a 1h recording",
             "do": "POST /transcribe/batch {path, summarize:0.6}; poll /jobs/{id}; read summary"},
            {"goal": "Interview turned into a clean Q&A doc",
             "do": "POST /transcribe with structure=interview (or structure=auto)"},
            {"goal": "Quiet or uneven recording that transcribes poorly",
             "do": "POST /transcribe with normalize=speech (or normalize=loudnorm); add gain_db=6 if very faint"},
            {"goal": "Self-correct bad transcripts automatically",
             "do": "transcribe → if response.quality.low_confidence, re-POST with the param in "
                   "quality.suggestion (normalize=speech → gain_db=6 → model=large)"},
        ],
        "quality_signal": {
            "field": "every json response carries `quality`: {low_confidence, confidence (0–1), "
                     "avg_logprob, max_no_speech_prob, max_compression_ratio, reasons[], suggestion}",
            "how_to_use": "if quality.low_confidence is true, retry the SAME audio with the "
                          "parameter named in quality.suggestion. Escalate: normalize=speech → "
                          "+gain_db=6 → model=large. Stop when low_confidence clears or after model=large.",
            "headers": "non-json callers read x-asr-confidence + x-asr-low-confidence",
        },
        "instructions": (
            "1) Choose the input mode: upload (file), link (url), or local file (path) — exactly one.\n"
            "2) Choose a model: omit for fast `turbo`; set `model=large` when accuracy matters.\n"
            "3) Choose the output: response_format json|text|srt|vtt; add save=true to persist.\n"
            "4) Optionally post-process (json only): structure=auto to detect+reformat, "
            "summarize=0.0..1.0 to distill (higher = more aggressive).\n"
            "5) For long audio use /transcribe/batch and poll /jobs/{id} (or stream /jobs/{id}/stream).\n"
            "6) GET /models to see which model is loaded; the sidecar hot-swaps on demand.\n"
            "Notes: one model is resident at a time; post-processing calls a local LLM (slower, "
            "wakes a ~20GB model); audio is decoded via ffmpeg so most formats work."
        ),
        "openapi": "/openapi.json",
    }


class StructureReq(BaseModel):
    text: str = Field(..., min_length=1)
    hint: str | None = None          # "auto" / a type hint like "interview"
    llm_model: str | None = None


class SummarizeReq(BaseModel):
    text: str = Field(..., min_length=1)
    weight: float = Field(0.5, ge=0.0, le=1.0)
    llm_model: str | None = None


@app.post("/structure")
async def structure_ep(req: StructureReq, request: Request):
    """Detect + reformat any transcript (no transcription step). Reusable on
    text you already have."""
    _check_auth(request)
    if not req.text.strip():
        raise HTTPException(400, "empty text")
    hint = None if (req.hint or "").strip().lower() in ("auto", "", "true", "1") else req.hint
    # No segment timing on a text-only call — analyze on text alone.
    signals = {"segments": None, "questions": req.text.count("?"), "note": "text-only call (no timing)"}
    return await _structure_text(req.text, signals, hint, req.llm_model or LLM_MODEL)


@app.post("/summarize")
async def summarize_ep(req: SummarizeReq, request: Request):
    """Summarize any text at a 0–1 strength. Reusable on text you already have."""
    _check_auth(request)
    if not req.text.strip():
        raise HTTPException(400, "empty text")
    return await _summarize_text(req.text, req.weight, req.llm_model or LLM_MODEL)


@app.post("/admin/unload")
async def admin_unload(request: Request):
    """Force-unload now. Refuses while busy unless ?force=true (governor preempt)."""
    _check_auth(request)
    force = request.query_params.get("force", "").lower() in ("1", "true", "yes")
    if _active > 0 and not force:
        return {"unloaded": False, "refused": "busy", "active_jobs": _active}
    was = _loaded_model
    if was is not None:
        async with _sem:
            await _unload_model()
    return {"unloaded": was is not None, "forced": force, "model": was}


# ---------- HTTP: sync transcribe ----------
@app.post("/transcribe")
async def transcribe(
    request: Request,
    file: UploadFile | None = File(default=None),
    url: str | None = Form(default=None),
    path: str | None = Form(default=None),
    model: str | None = Form(default=None),
    language: str | None = Form(default=None),
    initial_prompt: str | None = Form(default=None),
    word_timestamps: bool = Form(default=False),
    response_format: str = Form(default="json"),
    save: bool = Form(default=False),
    output_path: str | None = Form(default=None),
    normalize: str | None = Form(default=None),
    gain_db: float | None = Form(default=None),
    structure: str | None = Form(default=None),
    summarize: float | None = Form(default=None),
    llm_model: str | None = Form(default=None),
):
    """Synchronous transcription for short clips (under ~a few minutes).

    Model — `model` picks which whisper Atelier loads (alias turbo|large|accurate
    or a full HF repo); omitted = WHISPER_MODEL_REPO. The sidecar hot-swaps to it.
    Source — exactly one of: file (multipart upload), url (link to pull),
    path (file already on the Mac).
    Audio prep (optional, ffmpeg; helps quiet/uneven recordings transcribe):
      normalize=loudnorm|dynaudnorm|speechnorm|speech (or true→speech) → level it
      gain_db=<float>  fixed boost in dB (e.g. 6) applied before normalize.
    Output — response body in response_format (json|text|srt|vtt|verbose_json);
    set save=true to also persist it under WHISPER_OUTPUT_DIR (or output_path).
    Post-process (optional, LLM via Ollama; requires json/verbose_json):
      structure=auto|interview|lecture|… → adds {detected_type, structured}
      summarize=0.0..1.0 (light→aggressive) → adds {summary, summary_weight}
      llm_model=<ollama model>  overrides WHISPER_LLM_MODEL for this request.
    """
    _check_auth(request)
    fmt = response_format.lower()
    if fmt not in ("json", "text", "srt", "vtt", "verbose_json"):
        raise HTTPException(400, f"unknown response_format: {response_format}")
    if (structure is not None or summarize is not None) and fmt not in ("json", "verbose_json"):
        raise HTTPException(400, "structure/summarize require response_format=json or verbose_json")
    repo = _resolve_model(model)

    src_path, raw, is_temp = await _resolve_source(file, url, path)
    sha = hashlib.sha256(raw).hexdigest()   # cache key = ORIGINAL audio (pre-normalize)
    proc_path, is_proc = src_path, False
    try:
        if normalize or gain_db:
            proc_path, is_proc = await asyncio.to_thread(_ffmpeg_preprocess, src_path, normalize, gain_db)
        result, elapsed = await _run_transcription(
            proc_path, repo, language, initial_prompt, word_timestamps, len(raw)
        )
    finally:
        if is_proc and proc_path != src_path:
            try:
                os.unlink(proc_path)
            except OSError:
                pass
        if is_temp:
            try:
                os.unlink(src_path)
            except OSError:
                pass

    quality = _quality(result, normalized=bool(normalize), gained=bool(gain_db))
    headers = {"x-content-sha256": sha, "x-transcribe-seconds": f"{elapsed:.3f}",
               "x-model": repo,
               "x-asr-confidence": str(quality.get("confidence")),
               "x-asr-low-confidence": "true" if quality["low_confidence"] else "false"}
    if save:
        headers["x-saved-path"] = _save_output(result, sha, fmt, output_path)

    body, media_type = _format_body(result, fmt)
    # Quality block: lets an agent detect a bad transcript and retry with
    # normalize / gain_db / model. (json/verbose_json only — text/srt/vtt callers
    # read the x-asr-* headers instead.)
    if isinstance(body, dict):
        body["quality"] = quality
    # Optional LLM post-processing runs here — after the whisper semaphore is
    # released, so it never blocks another transcription.
    if (structure is not None or summarize is not None) and isinstance(body, dict):
        body.update(await _postprocess(result, structure, summarize, llm_model))

    if isinstance(body, (dict, list)):
        return JSONResponse(content=body, headers=headers)
    return PlainTextResponse(content=body, media_type=media_type, headers=headers)


# ---------- HTTP: async batch ----------
class BatchReq(BaseModel):
    url: str | None = None
    path: str | None = None
    model: str | None = None
    language: str | None = None
    initial_prompt: str | None = None
    word_timestamps: bool = False
    response_format: str = Field(default="json")
    save: bool = True
    output_path: str | None = None
    normalize: str | None = None
    gain_db: float | None = None
    structure: str | None = None
    summarize: float | None = None
    llm_model: str | None = None


async def _run_job(job_id: str, req: BatchReq) -> None:
    job = _jobs[job_id]
    if job.get("cancel"):
        job.update(status="cancelled", finished_at=time.time())
        return
    job.update(status="running", started_at=time.time())
    try:
        repo = _resolve_model(req.model)
        job["model"] = repo
        src_path, raw, is_temp = await _resolve_source(None, req.url, req.path)
        sha = hashlib.sha256(raw).hexdigest()
        job["sha256"] = sha
        proc_path, is_proc = src_path, False
        try:
            if req.normalize or req.gain_db:
                proc_path, is_proc = await asyncio.to_thread(_ffmpeg_preprocess, src_path, req.normalize, req.gain_db)
            result, elapsed = await _run_transcription(
                proc_path, repo, req.language, req.initial_prompt, req.word_timestamps, len(raw)
            )
        finally:
            if is_proc and proc_path != src_path:
                try:
                    os.unlink(proc_path)
                except OSError:
                    pass
            if is_temp:
                try:
                    os.unlink(src_path)
                except OSError:
                    pass
        fmt = req.response_format.lower()
        body, _ = _format_body(result, fmt)
        if isinstance(body, dict):
            body["quality"] = _quality(result, normalized=bool(req.normalize), gained=bool(req.gain_db))
        # Optional LLM post-processing (structure / summarize) for long jobs.
        if (req.structure is not None or req.summarize is not None) and isinstance(body, dict):
            body.update(await _postprocess(result, req.structure, req.summarize, req.llm_model))
        saved = _save_output(result, sha, fmt, req.output_path) if req.save else None
        job.update(status="done", finished_at=time.time(), elapsed_s=round(elapsed, 3),
                   saved_path=saved, result=body)
    except HTTPException as e:
        job.update(status="error", finished_at=time.time(), error=f"{e.status_code}: {e.detail}")
    except Exception as e:
        job.update(status="error", finished_at=time.time(), error=str(e))


@app.post("/transcribe/batch")
async def transcribe_batch(req: BatchReq, request: Request):
    """Submit a long transcription. Returns {job_id} immediately; poll
    /jobs/<id> or stream /jobs/<id>/stream. Source is url or path (uploads go
    through the sync endpoint). `model` selects the whisper to load. save
    defaults to true — long jobs persist by sha256 so the result survives a
    client disconnect."""
    _check_auth(request)
    if not (req.url or req.path):
        raise HTTPException(400, "batch requires url or path")
    if req.url and req.path:
        raise HTTPException(400, "provide only one of url or path")
    job_id = secrets.token_hex(8)
    _jobs[job_id] = {"id": job_id, "status": "queued", "created_at": time.time(),
                     "source": req.url or req.path, "model": _resolve_model(req.model),
                     "cancel": False}
    asyncio.create_task(_run_job(job_id, req))
    return {"job_id": job_id, "status": "queued"}


def _job_public(job: dict) -> dict:
    """Job snapshot without the (potentially large) result payload."""
    return {k: v for k, v in job.items() if k not in ("result", "cancel")}


@app.get("/jobs/{job_id}")
def job_status(job_id: str, request: Request):
    _check_auth(request)
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    return _job_public(job)


@app.get("/jobs/{job_id}/result")
def job_result(job_id: str, request: Request):
    _check_auth(request)
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    if job["status"] != "done":
        raise HTTPException(409, f"job not done (status={job['status']})")
    return {"job_id": job_id, "sha256": job.get("sha256"), "model": job.get("model"),
            "saved_path": job.get("saved_path"), "result": job.get("result")}


@app.get("/jobs/{job_id}/stream")
async def job_stream(job_id: str, request: Request):
    """SSE progress for a batch job. Emits one event per status change plus a
    heartbeat with elapsed seconds, then a terminal event (done/error/cancelled)
    and closes. Real per-segment progress is a v2 enhancement — mlx-whisper has
    no streaming callback in the stable release."""
    _check_auth(request)
    if job_id not in _jobs:
        raise HTTPException(404, "no such job")

    async def gen():
        last = None
        while True:
            job = _jobs.get(job_id)
            if job is None:
                yield f"event: error\ndata: {json.dumps({'error': 'job evicted'})}\n\n"
                return
            status = job["status"]
            if status != last:
                yield f"event: status\ndata: {json.dumps(_job_public(job))}\n\n"
                last = status
            if status in ("done", "error", "cancelled"):
                if status == "done":
                    yield f"event: result\ndata: {json.dumps({'job_id': job_id, 'saved_path': job.get('saved_path'), 'elapsed_s': job.get('elapsed_s')})}\n\n"
                return
            elapsed = round(time.time() - job.get("started_at", job["created_at"]), 1)
            yield f"event: heartbeat\ndata: {json.dumps({'status': status, 'elapsed_s': elapsed})}\n\n"
            await asyncio.sleep(1.0)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.delete("/jobs/{job_id}")
def job_cancel(job_id: str, request: Request):
    """Cancel a job. Effective only before it starts running — mlx-whisper
    can't be interrupted mid-call, so a running job runs to completion."""
    _check_auth(request)
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    if job["status"] == "queued":
        job["cancel"] = True
        job["status"] = "cancelled"
        return {"cancelled": True, "job_id": job_id}
    return {"cancelled": False, "job_id": job_id, "reason": f"already {job['status']}"}
