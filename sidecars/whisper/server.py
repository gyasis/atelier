"""
whisper-sidecar — ASR (speech-to-text) for the Mac Studio inference hub.

Runs `whisper-large-v3-turbo` via `mlx-whisper` (Apple MLX). On an M1 Max this
hits RTF ~40-50x (12 min of audio transcribed in ~14-18s), 30-50% faster than
whisper.cpp with no C++ build chain. See docs/ARCHITECTURE.md §5.2.

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
    GET  /readyz               status (warm/cold/busy), job + queue depth
    POST /admin/unload         force-unload the model (memory-governor courtesy)
    POST /transcribe           sync; for clips under ~a few minutes
    POST /transcribe/batch     async; returns {job_id} for long audio
    GET  /jobs/<id>            job status snapshot
    GET  /jobs/<id>/stream     SSE progress: queued -> running -> done/error
    GET  /jobs/<id>/result     the transcript once done
    DELETE /jobs/<id>          cancel a job (only effective before it starts running)

Env vars:
    WHISPER_MODEL_REPO    HF repo, default "mlx-community/whisper-large-v3-turbo"
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
import os
import secrets
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

import mlx_whisper

MODEL_REPO = os.environ.get("WHISPER_MODEL_REPO", "mlx-community/whisper-large-v3-turbo")
OUTPUT_DIR = Path(os.environ.get("WHISPER_OUTPUT_DIR", str(Path.home() / "outputs/transcripts")))
MAX_PULL_BYTES = int(os.environ.get("WHISPER_MAX_PULL_MB", "512")) * 1024 * 1024
HUB_TOKEN = os.environ.get("HUB_TOKEN")
# Constitutional idle-unload (see kokoro/dia): a 1.6 GB model squatting on
# unified memory while idle starves video/LLM jobs. Default unload after 5 min.
# KEEP_WARM=true opts out (e.g. a long batch transcription session).
IDLE_UNLOAD_SECONDS = int(os.environ.get("IDLE_UNLOAD_SECONDS", "300"))
KEEP_WARM = os.environ.get("KEEP_WARM", "false").lower() in ("1", "true", "yes")
IDLE_TICK_SECONDS = 30

# mlx-whisper loads via an lru_cache'd loader; we track load state ourselves
# since that cache is opaque. _loaded flips on warm, off on unload.
_loaded = False
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


async def _load_and_warm() -> None:
    """Cold-load the model into mlx-whisper's loader cache + warm it. Idempotent."""
    global _loaded, _idle_unloaded_at
    if _loaded:
        return
    t0 = time.perf_counter()
    print(f"[whisper] loading {MODEL_REPO}", flush=True)
    try:
        # Populate the loader's lru_cache so the first /transcribe is warm.
        await asyncio.to_thread(mlx_whisper.load_models.load_model, MODEL_REPO)
        _loaded = True
        _idle_unloaded_at = None
        print(f"[whisper] model warm in {time.perf_counter()-t0:.1f}s", flush=True)
    except Exception as e:
        _loaded = False
        print(f"[whisper] load failed: {e}", flush=True)


async def _unload_model() -> None:
    """Drop the model from memory by clearing mlx-whisper's loader cache."""
    global _loaded, _idle_unloaded_at
    if not _loaded:
        return
    print("[whisper] idle-unload — clearing model cache", flush=True)
    try:
        # The loader is @lru_cache'd; clearing it drops the last model reference.
        if hasattr(mlx_whisper.load_models.load_model, "cache_clear"):
            mlx_whisper.load_models.load_model.cache_clear()
    except Exception as e:
        print(f"[whisper] cache_clear failed: {e}", flush=True)
    _loaded = False
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
        if not _loaded:
            continue
        if _active == 0 and _waiting == 0 and time.monotonic() - _last_request_at > IDLE_UNLOAD_SECONDS:
            async with _sem:
                if _loaded and _active == 0:
                    await _unload_model()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _unload_task
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    async with _sem:
        await _load_and_warm()
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


def _transcribe_sync(audio_path: str, language: str | None,
                     initial_prompt: str | None, word_timestamps: bool) -> dict:
    """Blocking mlx-whisper call. Runs in a thread off the event loop."""
    return mlx_whisper.transcribe(
        audio_path,
        path_or_hf_repo=MODEL_REPO,
        language=language,
        initial_prompt=initial_prompt,
        word_timestamps=word_timestamps,
    )


async def _run_transcription(audio_path: str, language: str | None,
                             initial_prompt: str | None, word_timestamps: bool,
                             audio_bytes: int):
    """Cold-load if needed, then transcribe under the single-flight semaphore.
    Returns (result, elapsed_seconds) and emits the [asr] telemetry line the
    governor tails."""
    global _last_request_at, _active, _waiting
    if not _loaded:
        print("[whisper] cold-load triggered by request", flush=True)
        async with _sem:
            await _load_and_warm()
        if not _loaded:
            raise HTTPException(503, "cold-load failed; see server logs")
    t0 = time.perf_counter()
    _waiting += 1
    async with _sem:
        _waiting -= 1
        _active += 1
        try:
            result = await asyncio.to_thread(
                _transcribe_sync, audio_path, language, initial_prompt, word_timestamps
            )
        except Exception as e:
            raise HTTPException(500, f"transcription failed: {e}")
        finally:
            _active -= 1
            _last_request_at = time.monotonic()
    elapsed = time.perf_counter() - t0
    # Telemetry line the governor ingests (one observable pane). audio_s is the
    # natural ASR ETA unit (seconds of audio -> seconds of compute).
    segs = result.get("segments", [])
    audio_s = segs[-1]["end"] if segs else 0.0
    chars = len(result.get("text", ""))
    rtf = (audio_s / elapsed) if elapsed > 0 else 0.0
    print(f"[asr] audio_s={audio_s:.1f} chars={chars} bytes={audio_bytes} "
          f"{elapsed:.2f}s rtf={rtf:.1f}x lang={result.get('language')}", flush=True)
    return result, elapsed


# ---------- HTTP: health ----------
@app.get("/healthz")
def healthz():
    return {"ok": True, "service": "whisper", "model": MODEL_REPO, "version": "1.0"}


@app.get("/readyz")
def readyz():
    state = "warm" if _loaded else "cold"
    lifecycle = "cold" if not _loaded else ("busy" if _active > 0 else "idle")
    return {
        "ok": True,
        "state": state,
        "lifecycle": lifecycle,
        "busy": _active > 0,
        "active_jobs": _active,
        "queue_depth": _waiting,
        "warmed": _loaded,
        "model": MODEL_REPO,
        "device": "mps",
        "batch_jobs": len(_jobs),
        "idle_seconds": round(time.monotonic() - _last_request_at, 1),
        "idle_unload_seconds": IDLE_UNLOAD_SECONDS,
        "keep_warm": KEEP_WARM,
        "last_unload_ago_s": round(time.monotonic() - _idle_unloaded_at, 1) if _idle_unloaded_at else None,
    }


@app.post("/admin/unload")
async def admin_unload(request: Request):
    """Force-unload now. Refuses while busy unless ?force=true (governor preempt)."""
    _check_auth(request)
    force = request.query_params.get("force", "").lower() in ("1", "true", "yes")
    if _active > 0 and not force:
        return {"unloaded": False, "refused": "busy", "active_jobs": _active}
    was_loaded = _loaded
    if was_loaded:
        async with _sem:
            await _unload_model()
    return {"unloaded": was_loaded, "forced": force, "model": MODEL_REPO}


# ---------- HTTP: sync transcribe ----------
@app.post("/transcribe")
async def transcribe(
    request: Request,
    file: UploadFile | None = File(default=None),
    url: str | None = Form(default=None),
    path: str | None = Form(default=None),
    language: str | None = Form(default=None),
    initial_prompt: str | None = Form(default=None),
    word_timestamps: bool = Form(default=False),
    response_format: str = Form(default="json"),
    save: bool = Form(default=False),
    output_path: str | None = Form(default=None),
):
    """Synchronous transcription for short clips (under ~a few minutes).

    Source — exactly one of: file (multipart upload), url (link to pull),
    path (file already on the Mac).
    Output — response body in response_format (json|text|srt|vtt|verbose_json);
    set save=true to also persist it under WHISPER_OUTPUT_DIR (or output_path).
    """
    _check_auth(request)
    fmt = response_format.lower()
    if fmt not in ("json", "text", "srt", "vtt", "verbose_json"):
        raise HTTPException(400, f"unknown response_format: {response_format}")

    audio_path, raw, is_temp = await _resolve_source(file, url, path)
    sha = hashlib.sha256(raw).hexdigest()
    try:
        result, elapsed = await _run_transcription(
            audio_path, language, initial_prompt, word_timestamps, len(raw)
        )
    finally:
        if is_temp:
            try:
                os.unlink(audio_path)
            except OSError:
                pass

    headers = {"x-content-sha256": sha, "x-transcribe-seconds": f"{elapsed:.3f}",
               "x-model": MODEL_REPO}
    if save:
        headers["x-saved-path"] = _save_output(result, sha, fmt, output_path)

    body, media_type = _format_body(result, fmt)
    if isinstance(body, (dict, list)):
        return JSONResponse(content=body, headers=headers)
    return PlainTextResponse(content=body, media_type=media_type, headers=headers)


# ---------- HTTP: async batch ----------
class BatchReq(BaseModel):
    url: str | None = None
    path: str | None = None
    language: str | None = None
    initial_prompt: str | None = None
    word_timestamps: bool = False
    response_format: str = Field(default="json")
    save: bool = True
    output_path: str | None = None


async def _run_job(job_id: str, req: BatchReq) -> None:
    job = _jobs[job_id]
    if job.get("cancel"):
        job.update(status="cancelled", finished_at=time.time())
        return
    job.update(status="running", started_at=time.time())
    try:
        audio_path, raw, is_temp = await _resolve_source(None, req.url, req.path)
        sha = hashlib.sha256(raw).hexdigest()
        job["sha256"] = sha
        try:
            result, elapsed = await _run_transcription(
                audio_path, req.language, req.initial_prompt, req.word_timestamps, len(raw)
            )
        finally:
            if is_temp:
                try:
                    os.unlink(audio_path)
                except OSError:
                    pass
        fmt = req.response_format.lower()
        saved = _save_output(result, sha, fmt, req.output_path) if req.save else None
        body, _ = _format_body(result, fmt)
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
    through the sync endpoint). save defaults to true — long jobs persist by
    sha256 so the result survives a client disconnect."""
    _check_auth(request)
    if not (req.url or req.path):
        raise HTTPException(400, "batch requires url or path")
    if req.url and req.path:
        raise HTTPException(400, "provide only one of url or path")
    job_id = secrets.token_hex(8)
    _jobs[job_id] = {"id": job_id, "status": "queued", "created_at": time.time(),
                     "source": req.url or req.path, "cancel": False}
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
    return {"job_id": job_id, "sha256": job.get("sha256"),
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
