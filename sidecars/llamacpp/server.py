"""
llamacpp-sidecar — managed wrapper around llama.cpp's `llama-server` (Metal).

llama-server already speaks the OpenAI API and Metal-accelerates GGUF models, but
it has no Atelier contract (lifecycle /readyz, /admin/unload, idle-unload, /agent)
and doesn't free memory when idle. This wrapper SUBPROCESS-MANAGES llama-server on
an internal port, proxies the OpenAI `/v1/*` surface through to it, and adds the
hub contract so the governor can monitor + idle-evict it like every other sidecar.

    client → :8771/v1/chat/completions        (Atelier-fronted OpenAI API)
             :8771/healthz /readyz /agent /admin/unload
             └─ proxies → 127.0.0.1:18771      (llama-server child, Metal/GGUF)
    idle → wrapper stops the child → ~all the model memory is freed.

Independent by design: its own port, plist, and venv — run it or skip it without
touching any other sidecar.

Endpoints:
    GET  /healthz          liveness (wrapper only; never starts the child)
    GET  /readyz           lifecycle warm/cold/busy, child pid + model, queue depth
    GET  /agent            self-describing manifest for AI agents
    GET  /models           proxied list of served models
    POST /admin/unload     stop the child to free memory (refuses busy w/o ?force)
    ANY  /v1/{path}        OpenAI-compatible proxy (cold-starts the child on demand)

Env vars:
    LLAMACPP_BIN           path to llama-server (default: search PATH + Homebrew)
    LLAMACPP_MODEL         path to a .gguf model (required to serve)
    LLAMACPP_ALIAS         model name reported to OpenAI clients (default: file stem)
    LLAMACPP_PORT          public wrapper port (default 8771)
    LLAMACPP_CHILD_PORT    internal llama-server port (default 18771)
    LLAMACPP_ARGS          extra llama-server args, e.g. "--ctx-size 8192 -ngl 99"
    LLAMACPP_START_TIMEOUT seconds to wait for the model to load (default 180)
    IDLE_UNLOAD_SECONDS    stop the child after N idle seconds (default 600)
    KEEP_WARM              "true" to disable idle-unload (default false)
    HUB_TOKEN              optional bearer token (OpenAI clients pass it as api_key)
"""

import asyncio
import os
import shutil
import subprocess
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

BIN = os.environ.get("LLAMACPP_BIN") or shutil.which("llama-server") or "/opt/homebrew/bin/llama-server"
MODEL = os.environ.get("LLAMACPP_MODEL", "")
ALIAS = os.environ.get("LLAMACPP_ALIAS") or (Path(MODEL).stem if MODEL else "llamacpp")
CHILD_PORT = int(os.environ.get("LLAMACPP_CHILD_PORT", "18771"))
EXTRA_ARGS = (os.environ.get("LLAMACPP_ARGS", "") or "").split()
START_TIMEOUT = float(os.environ.get("LLAMACPP_START_TIMEOUT", "180"))
HUB_TOKEN = os.environ.get("HUB_TOKEN")
# LLMs are the heaviest tenants — idle-unload sooner so a forgotten chat model
# doesn't squat 8–30 GB. KEEP_WARM=true for an always-hot coding assistant.
IDLE_UNLOAD_SECONDS = int(os.environ.get("IDLE_UNLOAD_SECONDS", "600"))
KEEP_WARM = os.environ.get("KEEP_WARM", "false").lower() in ("1", "true", "yes")
IDLE_TICK_SECONDS = 30
CHILD_BASE = f"http://127.0.0.1:{CHILD_PORT}"

_proc: subprocess.Popen | None = None
_warmed: bool = False
_start_lock = asyncio.Lock()       # serializes child start/stop; proxying stays concurrent
_last_request_at = time.monotonic()
_unload_task: asyncio.Task | None = None
_idle_unloaded_at: float | None = None
# Job-aware state: _active = in-flight proxied requests (busy when >0). The idle
# watcher must never stop the child while _active>0 — would kill a live generation.
_active: int = 0


def _child_alive() -> bool:
    return _proc is not None and _proc.poll() is None


async def _child_healthy(client: httpx.AsyncClient) -> bool:
    try:
        r = await client.get(f"{CHILD_BASE}/health", timeout=2)
        return r.status_code == 200
    except Exception:
        return False


async def _start_child() -> None:
    """Spawn llama-server and wait until its model is loaded. Caller holds _start_lock."""
    global _proc, _warmed, _idle_unloaded_at
    if _child_alive() and _warmed:
        return
    if not MODEL or not Path(MODEL).expanduser().is_file():
        raise HTTPException(503, f"no GGUF model configured (set LLAMACPP_MODEL; got '{MODEL}')")
    if not (BIN and (Path(BIN).is_file() or shutil.which(BIN))):
        raise HTTPException(503, f"llama-server not found (set LLAMACPP_BIN; got '{BIN}')")
    cmd = [BIN, "-m", str(Path(MODEL).expanduser()), "--host", "127.0.0.1",
           "--port", str(CHILD_PORT), "--alias", ALIAS, *EXTRA_ARGS]
    t0 = time.perf_counter()
    print(f"[llamacpp] starting child: {' '.join(cmd)}", flush=True)
    # Inherit stdout/stderr so launchd captures llama-server's logs too.
    _proc = subprocess.Popen(cmd)
    async with httpx.AsyncClient() as client:
        deadline = time.monotonic() + START_TIMEOUT
        while time.monotonic() < deadline:
            if not _child_alive():
                _proc = None
                raise HTTPException(503, "llama-server exited during startup (see logs)")
            if await _child_healthy(client):
                _warmed = True
                _idle_unloaded_at = None
                print(f"[llamacpp] child ready in {time.perf_counter()-t0:.1f}s ({ALIAS})", flush=True)
                return
            await asyncio.sleep(0.5)
    await _stop_child()
    raise HTTPException(503, f"llama-server did not become healthy within {START_TIMEOUT}s")


async def _stop_child() -> None:
    """Terminate llama-server, freeing its model memory. Caller holds _start_lock."""
    global _proc, _warmed, _idle_unloaded_at
    if _proc is None:
        return
    print("[llamacpp] stopping child (idle-unload / admin)", flush=True)
    try:
        _proc.terminate()
        try:
            await asyncio.to_thread(_proc.wait, 10)
        except Exception:
            _proc.kill()
    except Exception as e:
        print(f"[llamacpp] stop error: {e}", flush=True)
    _proc = None
    _warmed = False
    _idle_unloaded_at = time.monotonic()


async def _ensure_started() -> None:
    if _child_alive() and _warmed:
        return
    async with _start_lock:
        if not (_child_alive() and _warmed):
            await _start_child()


async def _idle_watcher() -> None:
    if KEEP_WARM:
        print("[llamacpp] idle-watcher disabled (KEEP_WARM=true)", flush=True)
        return
    print(f"[llamacpp] idle-watcher active (stop child after {IDLE_UNLOAD_SECONDS}s idle)", flush=True)
    while True:
        await asyncio.sleep(IDLE_TICK_SECONDS)
        if not _child_alive():
            continue
        if _active == 0 and time.monotonic() - _last_request_at > IDLE_UNLOAD_SECONDS:
            async with _start_lock:
                if _active == 0 and _child_alive():
                    await _stop_child()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _unload_task
    # Lazy start — the child cold-starts on the first /v1 request (saves memory
    # until the model is actually used). KEEP_WARM users can pre-warm via /v1/models.
    if KEEP_WARM and MODEL:
        try:
            await _ensure_started()
        except Exception as e:
            print(f"[llamacpp] pre-warm failed: {e}", flush=True)
    _unload_task = asyncio.create_task(_idle_watcher())
    yield
    if _unload_task and not _unload_task.done():
        _unload_task.cancel()
        try:
            await _unload_task
        except asyncio.CancelledError:
            pass
    async with _start_lock:
        await _stop_child()


app = FastAPI(lifespan=lifespan)


def _check_auth(request: Request) -> None:
    if not HUB_TOKEN:
        return
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer ") or auth[7:] != HUB_TOKEN:
        raise HTTPException(401, "invalid bearer token")


@app.get("/healthz")
def healthz():
    return {"ok": True, "service": "llamacpp", "engine": "llama.cpp/llama-server",
            "model": ALIAS, "version": "1.0"}


@app.get("/readyz")
def readyz():
    alive = _child_alive() and _warmed
    state = "warm" if alive else "cold"
    lifecycle = "cold" if not alive else ("busy" if _active > 0 else "idle")
    return {
        "ok": True,
        "state": state,
        "lifecycle": lifecycle,
        "busy": _active > 0,
        "active_jobs": _active,
        "queue_depth": 0,
        "warmed": _warmed,
        "engine": "llama.cpp",
        "model": ALIAS,
        "model_path": MODEL or None,
        "child_pid": _proc.pid if _child_alive() else None,
        "child_port": CHILD_PORT,
        "device": "metal",
        "idle_seconds": round(time.monotonic() - _last_request_at, 1),
        "idle_unload_seconds": IDLE_UNLOAD_SECONDS,
        "keep_warm": KEEP_WARM,
        "last_unload_ago_s": round(time.monotonic() - _idle_unloaded_at, 1) if _idle_unloaded_at else None,
    }


@app.post("/admin/unload")
async def admin_unload(request: Request):
    """Stop the child to free memory. Refuses while busy unless ?force=true."""
    _check_auth(request)
    force = request.query_params.get("force", "").lower() in ("1", "true", "yes")
    if _active > 0 and not force:
        return {"unloaded": False, "refused": "busy", "active_jobs": _active}
    was = _child_alive()
    if was:
        async with _start_lock:
            await _stop_child()
    return {"unloaded": was, "forced": force, "model": ALIAS}


@app.get("/agent")
def agent(request: Request):
    """Self-describing manifest for AI agents."""
    _check_auth(request)
    auth = ("send header `Authorization: Bearer <HUB_TOKEN>` (OpenAI clients: api_key=HUB_TOKEN)"
            if HUB_TOKEN else "none required (HUB_TOKEN not set)")
    return {
        "service": "llamacpp",
        "role": "LLM — llama.cpp/llama-server (Metal, GGUF), OpenAI-compatible",
        "summary": "Serves a GGUF model via llama.cpp's Metal backend behind the "
                   "OpenAI API. Use it for stable, wide-architecture local inference. "
                   "The wrapper cold-starts the engine on first call and idle-unloads "
                   "it to free memory.",
        "auth": auth,
        "openai_base_url": f"http://{os.environ.get('LLAMACPP_HOST_HINT', '<host>')}:{os.environ.get('LLAMACPP_PORT','8771')}/v1",
        "model": {"alias": ALIAS, "path": MODEL or None, "format": "GGUF",
                  "loaded": _child_alive() and _warmed},
        "methods": [
            {"name": "chat", "http": "POST /v1/chat/completions",
             "encoding": "application/json (OpenAI schema; stream=true supported)",
             "params": "{model, messages[], temperature, max_tokens, stream, …}",
             "returns": "OpenAI chat completion (or SSE stream)",
             "example": "curl -s $URL/v1/chat/completions -H 'content-type: application/json' "
                        f"-d '{{\"model\":\"{ALIAS}\",\"messages\":[{{\"role\":\"user\",\"content\":\"hi\"}}]}}'"},
            {"name": "completions", "http": "POST /v1/completions", "returns": "OpenAI text completion"},
            {"name": "models", "http": "GET /v1/models", "returns": "served model list"},
            {"name": "readyz", "http": "GET /readyz", "returns": "warm/cold/busy + child pid"},
            {"name": "unload", "http": "POST /admin/unload", "returns": "stops the engine to free memory"},
        ],
        "recipes": [
            {"goal": "Local OpenAI-compatible chat", "do": "POST /v1/chat/completions with your messages"},
            {"goal": "Free the memory now", "do": "POST /admin/unload"},
        ],
        "instructions": (
            "1) Call POST /v1/chat/completions exactly like the OpenAI API (set stream=true for SSE).\n"
            "2) The engine cold-starts on the first call (model load can take seconds) and "
            "idle-unloads after inactivity — the next call pays the cold-start again.\n"
            "3) GET /readyz to see if it's warm; POST /admin/unload to free memory on demand.\n"
            "4) This is the GGUF/Metal path; for MLX-native models use the fastmlx sidecar."
        ),
        "openapi": "/openapi.json",
    }


@app.get("/models")
async def models(request: Request):
    _check_auth(request)
    await _ensure_started()
    async with httpx.AsyncClient() as client:
        try:
            r = await client.get(f"{CHILD_BASE}/v1/models", timeout=5)
            return r.json()
        except Exception as e:
            raise HTTPException(502, f"engine /v1/models failed: {e}")


@app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"])
async def proxy_v1(path: str, request: Request):
    """Transparent OpenAI-compatible proxy to the llama-server child. Cold-starts
    the child if needed, streams the response (so stream=true SSE works), and
    tracks in-flight requests so idle-unload can't kill a live generation."""
    global _active, _last_request_at
    _check_auth(request)
    await _ensure_started()
    body = await request.body()
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in ("host", "content-length", "authorization")}
    url = f"{CHILD_BASE}/v1/{path}"
    client = httpx.AsyncClient(timeout=None)
    _active += 1
    try:
        req = client.build_request(request.method, url, headers=headers,
                                   content=body, params=request.query_params)
        resp = await client.send(req, stream=True)
    except Exception as e:
        _active -= 1
        _last_request_at = time.monotonic()
        await client.aclose()
        raise HTTPException(502, f"engine proxy failed: {e}")

    async def streamer():
        global _active, _last_request_at
        try:
            async for chunk in resp.aiter_raw():
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()
            _active -= 1
            _last_request_at = time.monotonic()

    passthru = {k: v for k, v in resp.headers.items()
                if k.lower() not in ("content-length", "transfer-encoding", "connection")}
    return StreamingResponse(streamer(), status_code=resp.status_code,
                             headers=passthru, media_type=resp.headers.get("content-type"))
