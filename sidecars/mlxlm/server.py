"""
mlxlm-sidecar — managed wrapper around Apple's `mlx_lm.server` (MLX-native LLM).

mlx_lm (github.com/ml-explore/mlx-lm) is Apple's official MLX LLM toolkit; its
`mlx_lm.server` exposes an OpenAI-compatible API and tracks the mlx-lm release
exactly (unlike FastMLX, which lagged mlx-lm's API and couldn't generate). This
is the MLX-native LLM path — typically faster than GGUF on Apple Silicon.

Like the llamacpp sidecar, this wrapper subprocess-manages the engine on an
internal port, proxies `/v1/*`, and adds the Atelier contract (lifecycle
/readyz, /admin/unload, idle-unload, /agent). mlx_lm.server loads ONE model at
startup (via --model), so cold-start = model load; "warm" = model loaded.

    client → :8773/v1/chat/completions        (Atelier-fronted OpenAI API)
             :8773/healthz /readyz /agent /admin/unload
             └─ proxies → 127.0.0.1:18773      (mlx_lm.server child, MLX/Metal)
    idle → wrapper stops the child → MLX model memory freed.

Independent: own port, plist, venv — run it or skip it without touching others.

Endpoints:
    GET  /healthz          liveness (wrapper only)
    GET  /readyz           lifecycle warm/cold/busy, child pid + model
    GET  /agent            self-describing manifest for AI agents
    GET  /models           proxied served-model list
    POST /admin/unload     stop the child to free memory (refuses busy w/o ?force)
    ANY  /v1/{path}        OpenAI-compatible proxy (cold-starts the child on demand)

Env vars:
    MLXLM_MODEL            mlx-community repo OR local model dir (required to serve)
    MLXLM_ALIAS            model name reported to clients (default: basename)
    MLXLM_PYTHON           python that has mlx-lm (default: this interpreter)
    MLXLM_PORT             public wrapper port (default 8773)
    MLXLM_CHILD_PORT       internal mlx_lm.server port (default 18773)
    MLXLM_ARGS             extra mlx_lm.server args
    MLXLM_START_TIMEOUT    seconds to wait for the model to load (default 180)
    IDLE_UNLOAD_SECONDS    stop the child after N idle seconds (default 600)
    KEEP_WARM              "true" to disable idle-unload (default false)
    HF_HUB_OFFLINE         set "1" if the model is a local dir (avoids HF network)
    HUB_TOKEN              optional bearer token (OpenAI clients pass it as api_key)
"""

import asyncio
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

import subprocess

PYTHON = os.environ.get("MLXLM_PYTHON") or sys.executable
MODEL = os.environ.get("MLXLM_MODEL", "")
ALIAS = os.environ.get("MLXLM_ALIAS") or (Path(MODEL).name if MODEL else "mlxlm")
CHILD_PORT = int(os.environ.get("MLXLM_CHILD_PORT", "18773"))
EXTRA_ARGS = (os.environ.get("MLXLM_ARGS", "") or "").split()
START_TIMEOUT = float(os.environ.get("MLXLM_START_TIMEOUT", "180"))
HUB_TOKEN = os.environ.get("HUB_TOKEN")
IDLE_UNLOAD_SECONDS = int(os.environ.get("IDLE_UNLOAD_SECONDS", "600"))
KEEP_WARM = os.environ.get("KEEP_WARM", "false").lower() in ("1", "true", "yes")
IDLE_TICK_SECONDS = 30
CHILD_BASE = f"http://127.0.0.1:{CHILD_PORT}"

_proc: subprocess.Popen | None = None
_warmed: bool = False
_start_lock = asyncio.Lock()
_last_request_at = time.monotonic()
_unload_task: asyncio.Task | None = None
_idle_unloaded_at: float | None = None
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
    """Spawn mlx_lm.server and wait until the model is loaded. Caller holds _start_lock."""
    global _proc, _warmed, _idle_unloaded_at
    if _child_alive() and _warmed:
        return
    if not MODEL:
        raise HTTPException(503, "no model configured (set MLXLM_MODEL to an mlx-community repo or local dir)")
    cmd = [PYTHON, "-m", "mlx_lm.server", "--model", MODEL,
           "--host", "127.0.0.1", "--port", str(CHILD_PORT), *EXTRA_ARGS]
    t0 = time.perf_counter()
    print(f"[mlxlm] starting child: {' '.join(cmd)}", flush=True)
    _proc = subprocess.Popen(cmd)
    async with httpx.AsyncClient() as client:
        deadline = time.monotonic() + START_TIMEOUT
        while time.monotonic() < deadline:
            if not _child_alive():
                _proc = None
                raise HTTPException(503, "mlx_lm.server exited during startup (see logs)")
            if await _child_healthy(client):
                _warmed = True
                _idle_unloaded_at = None
                print(f"[mlxlm] child ready in {time.perf_counter()-t0:.1f}s ({ALIAS})", flush=True)
                return
            await asyncio.sleep(0.5)
    await _stop_child()
    raise HTTPException(503, f"mlx_lm.server did not become healthy within {START_TIMEOUT}s")


async def _stop_child() -> None:
    """Terminate the engine, freeing its MLX model memory. Caller holds _start_lock."""
    global _proc, _warmed, _idle_unloaded_at
    if _proc is None:
        return
    print("[mlxlm] stopping child (idle-unload / admin)", flush=True)
    try:
        _proc.terminate()
        try:
            await asyncio.to_thread(_proc.wait, 10)
        except Exception:
            _proc.kill()
    except Exception as e:
        print(f"[mlxlm] stop error: {e}", flush=True)
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
        print("[mlxlm] idle-watcher disabled (KEEP_WARM=true)", flush=True)
        return
    print(f"[mlxlm] idle-watcher active (stop child after {IDLE_UNLOAD_SECONDS}s idle)", flush=True)
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
    if KEEP_WARM and MODEL:
        try:
            await _ensure_started()
        except Exception as e:
            print(f"[mlxlm] pre-warm failed: {e}", flush=True)
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
    return {"ok": True, "service": "mlxlm", "engine": "mlx_lm.server (MLX)",
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
        "engine": "mlx_lm",
        "model": ALIAS,
        "model_ref": MODEL or None,
        "child_pid": _proc.pid if _child_alive() else None,
        "child_port": CHILD_PORT,
        "device": "mps",
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
        "service": "mlxlm",
        "role": "LLM — Apple mlx_lm.server (MLX-native, Metal), OpenAI-compatible",
        "summary": "Serves an MLX model (mlx-community repo or local dir) behind the "
                   "OpenAI API via Apple's official mlx_lm.server. MLX-native — "
                   "typically faster than GGUF on Apple Silicon. The wrapper cold-starts "
                   "the engine on first call and idle-unloads it to free memory.",
        "auth": auth,
        "openai_base_url": f"http://<host>:{os.environ.get('MLXLM_PORT','8773')}/v1",
        "model": {"alias": ALIAS, "ref": MODEL or None, "format": "MLX safetensors",
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
            {"goal": "MLX-native OpenAI chat", "do": "POST /v1/chat/completions with your messages"},
            {"goal": "Free the memory now", "do": "POST /admin/unload"},
        ],
        "instructions": (
            "1) POST /v1/chat/completions like the OpenAI API (stream=true for SSE).\n"
            "2) The engine cold-starts on the first call (model load takes seconds) and "
            "idle-unloads after inactivity.\n"
            "3) GET /readyz for warm/cold; POST /admin/unload to free memory.\n"
            "4) This is the MLX-native path; for GGUF models use the llamacpp sidecar."
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
    """Transparent OpenAI-compatible proxy to the mlx_lm.server child. Cold-starts
    the child if needed, streams the response (stream=true SSE works), and tracks
    in-flight requests so idle-unload can't kill a live generation."""
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
