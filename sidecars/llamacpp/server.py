"""
llamacpp-sidecar — governed, MULTI-MODEL wrapper around llama.cpp's `llama-server` (Metal).

llama-server speaks the OpenAI API and Metal-accelerates GGUF models, but it has no
Atelier contract (lifecycle /readyz, /admin/unload, idle-unload, /agent), it doesn't
free memory when idle, and it serves only ONE model per process. This wrapper turns it
into a governed "llama-swap": it keeps a REGISTRY of GGUF aliases and swaps the
llama-server child to whichever model a request asks for — one model resident at a time,
so the governor accounts for it as a single named tenant and can idle-evict / force-stop
it like every other sidecar. NOTHING loads a model outside this wrapper → no dashboard
blind spots (a raw llama-server/llama-cli would be invisible + un-evictable to the
governor; that is the exact gap this closes).

    client → :8771/v1/chat/completions {model:"agents-a1-q4"}   (Atelier-fronted OpenAI API)
             :8771/healthz /readyz /agent /models /admin/unload
             └─ ensures the RIGHT child is loaded (swaps if a different alias is asked),
                then proxies → 127.0.0.1:18771 (llama-server child, Metal/GGUF)
    idle → wrapper stops the child → ~all the model memory is freed.

The governor sees the currently-loaded alias via /readyz `model`, measures the child's
real RSS (process-subtree walk), and can unload it via /admin/unload. Swapping stops the
old child before starting the new one, so total resident never holds two models at once.

Model registry (alias → gguf + args), in priority order (later overrides earlier):
  1. the legacy single-model env (LLAMACPP_MODEL / LLAMACPP_ALIAS / LLAMACPP_ARGS) —
     always registered if set, so an existing single-model plist keeps working unchanged.
  2. a JSON config file (LLAMACPP_MODELS_CONFIG, default ~/.atelier/llamacpp-models.json):
       {"default":"gemma4-12b",
        "models":{"agents-a1-q4":{"path":"~/…/Agents-A1-Q4_K_M.gguf",
                                  "mmproj":"~/…/Agents-A1-mmproj.gguf",
                                  "args":"--ctx-size 8192 -ngl 99 --jinja"}, …}}
     `mmproj` is optional (vision projector). `args` may be a string or a list.

Endpoints:
    GET  /healthz          liveness (wrapper only; never starts a child)
    GET  /readyz           lifecycle warm/cold/busy, loaded alias, + available_models[]
    GET  /agent            self-describing manifest for AI agents
    GET  /models           full registry menu (loaded flag per alias) — OpenAI list shape
    POST /admin/unload     stop the child to free memory (refuses busy w/o ?force)
    ANY  /v1/{path}        OpenAI proxy — loads/swaps to the requested `model`, then forwards

Env vars:
    LLAMACPP_BIN           path to llama-server (default: search PATH + Homebrew)
    LLAMACPP_MODEL         path to a .gguf (legacy single-model; optional if config used)
    LLAMACPP_ALIAS         alias for the legacy model (default: file stem)
    LLAMACPP_MMPROJ        optional mmproj for the legacy model
    LLAMACPP_ARGS          default llama-server args, e.g. "--ctx-size 8192 -ngl 99 --jinja"
    LLAMACPP_MODELS_CONFIG registry JSON path (default ~/.atelier/llamacpp-models.json)
    LLAMACPP_PORT          public wrapper port (default 8771)
    LLAMACPP_CHILD_PORT    internal llama-server port (default 18771)
    LLAMACPP_START_TIMEOUT seconds to wait for a model to load (default 300 — big MoE is slow)
    IDLE_UNLOAD_SECONDS    stop the child after N idle seconds (default 600)
    KEEP_WARM              "true" to disable idle-unload (default false)
    HUB_TOKEN              optional bearer token (OpenAI clients pass it as api_key)
"""

import asyncio
import json
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
CHILD_PORT = int(os.environ.get("LLAMACPP_CHILD_PORT", "18771"))
DEFAULT_ARGS = (os.environ.get("LLAMACPP_ARGS", "") or "").split()
START_TIMEOUT = float(os.environ.get("LLAMACPP_START_TIMEOUT", "300"))
HUB_TOKEN = os.environ.get("HUB_TOKEN")
MODELS_CONFIG = os.environ.get("LLAMACPP_MODELS_CONFIG",
                               str(Path.home() / ".atelier" / "llamacpp-models.json"))
# LLMs are the heaviest tenants — idle-unload sooner so a forgotten chat model
# doesn't squat 8–38 GB. KEEP_WARM=true for an always-hot coding assistant.
IDLE_UNLOAD_SECONDS = int(os.environ.get("IDLE_UNLOAD_SECONDS", "600"))
KEEP_WARM = os.environ.get("KEEP_WARM", "false").lower() in ("1", "true", "yes")
IDLE_TICK_SECONDS = 30
CHILD_BASE = f"http://127.0.0.1:{CHILD_PORT}"


# ---------- model registry (alias → {path, args, mmproj}) ----------
def _load_registry() -> tuple[dict, str | None]:
    """Build the alias→spec registry. Legacy env model first (back-compat), then the
    JSON config (adds/overrides). Returns (registry, default_alias)."""
    reg: dict[str, dict] = {}
    default: str | None = None
    env_model = os.environ.get("LLAMACPP_MODEL", "")
    if env_model:
        alias = os.environ.get("LLAMACPP_ALIAS") or Path(env_model).stem
        reg[alias] = {"path": os.path.expanduser(env_model), "args": list(DEFAULT_ARGS),
                      "mmproj": os.path.expanduser(os.environ["LLAMACPP_MMPROJ"])
                      if os.environ.get("LLAMACPP_MMPROJ") else None}
        default = alias
    cfg = Path(MODELS_CONFIG).expanduser()
    if cfg.is_file():
        try:
            d = json.loads(cfg.read_text())
            for alias, spec in (d.get("models") or {}).items():
                args = spec.get("args")
                if isinstance(args, str):
                    args = args.split()
                reg[alias] = {"path": os.path.expanduser(spec["path"]),
                              "args": list(args) if args else list(DEFAULT_ARGS),
                              "mmproj": os.path.expanduser(spec["mmproj"]) if spec.get("mmproj") else None}
            if d.get("default"):
                default = d["default"]
        except Exception as e:
            print(f"[llamacpp] registry config error ({cfg}): {e}", flush=True)
    if default not in reg:
        default = next(iter(reg), None)
    return reg, default


REGISTRY, DEFAULT_ALIAS = _load_registry()

_proc: subprocess.Popen | None = None
_current_alias: str | None = None   # which registry alias the live child serves
_warmed: bool = False
_start_lock = asyncio.Lock()        # serializes child start/stop/swap; proxying stays concurrent
_last_request_at = time.monotonic()
_unload_task: asyncio.Task | None = None
_idle_unloaded_at: float | None = None
# Job-aware state: _active = in-flight proxied requests (busy when >0). The idle
# watcher — and a model SWAP — must never stop the child while _active>0.
_active: int = 0


def _child_alive() -> bool:
    return _proc is not None and _proc.poll() is None


async def _child_healthy(client: httpx.AsyncClient) -> bool:
    try:
        r = await client.get(f"{CHILD_BASE}/health", timeout=2)
        return r.status_code == 200
    except Exception:
        return False


async def _start_child(alias: str) -> None:
    """Spawn llama-server for `alias` and wait until its model is loaded. If a DIFFERENT
    model is currently loaded, stop it first (the swap). Caller holds _start_lock."""
    global _proc, _current_alias, _warmed, _idle_unloaded_at
    if _child_alive() and _warmed and _current_alias == alias:
        return
    spec = REGISTRY.get(alias)
    if not spec:
        raise HTTPException(404, f"unknown model '{alias}' (available: {sorted(REGISTRY)})")
    path = Path(spec["path"]).expanduser()
    if not path.is_file():
        raise HTTPException(503, f"gguf not found for '{alias}': {path}")
    if not (BIN and (Path(BIN).is_file() or shutil.which(BIN))):
        raise HTTPException(503, f"llama-server not found (set LLAMACPP_BIN; got '{BIN}')")
    # A swap: stop the currently-loaded (different) model before starting the new one,
    # so two models never sit resident at once (the whole point of governed swapping).
    if _child_alive():
        await _stop_child()
    cmd = [BIN, "-m", str(path), "--host", "127.0.0.1", "--port", str(CHILD_PORT),
           "--alias", alias, *spec["args"]]
    if spec.get("mmproj"):
        cmd += ["--mmproj", str(Path(spec["mmproj"]).expanduser())]
    t0 = time.perf_counter()
    print(f"[llamacpp] starting child ({alias}): {' '.join(cmd)}", flush=True)
    _proc = subprocess.Popen(cmd)   # inherit stdout/stderr so launchd captures llama-server logs
    _current_alias = alias
    async with httpx.AsyncClient() as client:
        deadline = time.monotonic() + START_TIMEOUT
        while time.monotonic() < deadline:
            if not _child_alive():
                _proc, _current_alias = None, None
                raise HTTPException(503, f"llama-server exited during startup of '{alias}' (see logs)")
            if await _child_healthy(client):
                _warmed = True
                _idle_unloaded_at = None
                print(f"[llamacpp] child ready in {time.perf_counter()-t0:.1f}s ({alias})", flush=True)
                return
            await asyncio.sleep(0.5)
    await _stop_child()
    raise HTTPException(503, f"'{alias}' did not become healthy within {START_TIMEOUT}s")


async def _stop_child() -> None:
    """Terminate llama-server, freeing its model memory. Caller holds _start_lock."""
    global _proc, _current_alias, _warmed, _idle_unloaded_at
    if _proc is None:
        return
    print(f"[llamacpp] stopping child ({_current_alias}) — idle/admin/swap", flush=True)
    try:
        _proc.terminate()
        try:
            await asyncio.to_thread(_proc.wait, 10)
        except Exception:
            _proc.kill()
    except Exception as e:
        print(f"[llamacpp] stop error: {e}", flush=True)
    _proc = None
    _current_alias = None
    _warmed = False
    _idle_unloaded_at = time.monotonic()


def _resolve_alias(requested: str | None) -> str:
    """Map a request's `model` to a registry alias. Empty/placeholder → the loaded model,
    else the default. An explicit UNKNOWN model is an error (never silently mis-serve)."""
    if requested and requested not in ("", "?"):
        if requested in REGISTRY:
            return requested
        raise HTTPException(404, f"unknown model '{requested}' (available: {sorted(REGISTRY)})")
    return _current_alias or DEFAULT_ALIAS


async def _ensure_started(alias: str) -> None:
    if _child_alive() and _warmed and _current_alias == alias:
        return
    async with _start_lock:
        if not (_child_alive() and _warmed and _current_alias == alias):
            # Guard: don't swap out a model that's mid-generation for a different one.
            if _child_alive() and _current_alias != alias and _active > 0:
                raise HTTPException(409, f"busy serving '{_current_alias}' ({_active} active) — "
                                         f"retry '{alias}' when it's idle")
            await _start_child(alias)


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
    # Lazy start — the child cold-starts on the first /v1 request (saves memory until a
    # model is actually used). KEEP_WARM pre-warms the default model.
    if KEEP_WARM and DEFAULT_ALIAS:
        try:
            await _ensure_started(DEFAULT_ALIAS)
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
            "models": sorted(REGISTRY), "default": DEFAULT_ALIAS, "version": "2.0-multimodel"}


@app.get("/readyz")
def readyz():
    alive = _child_alive() and _warmed
    lifecycle = "cold" if not alive else ("busy" if _active > 0 else "idle")
    return {
        "ok": True,
        "state": "warm" if alive else "cold",
        "lifecycle": lifecycle,
        "busy": _active > 0,
        "active_jobs": _active,
        "queue_depth": 0,
        "warmed": _warmed,
        "engine": "llama.cpp",
        "model": _current_alias,                      # the governor reads this as the loaded tenant
        "model_path": REGISTRY.get(_current_alias, {}).get("path") if _current_alias else None,
        "available_models": sorted(REGISTRY),         # the full menu (loaded or not)
        "default_model": DEFAULT_ALIAS,
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
        return {"unloaded": False, "refused": "busy", "active_jobs": _active, "model": _current_alias}
    was = _child_alive()
    prev = _current_alias
    if was:
        async with _start_lock:
            await _stop_child()
    return {"unloaded": was, "forced": force, "model": prev}


@app.get("/models")
def models_menu(request: Request):
    """Full registry menu (OpenAI list shape) — every registered alias, with a `loaded`
    flag so the dashboard/clients see what's available AND what's currently resident."""
    _check_auth(request)
    data = [{"id": alias, "object": "model", "owned_by": "atelier-llamacpp",
             "loaded": (alias == _current_alias and _warmed),
             "has_vision": bool(REGISTRY[alias].get("mmproj"))}
            for alias in sorted(REGISTRY)]
    return {"object": "list", "data": data, "default": DEFAULT_ALIAS, "loaded": _current_alias}


@app.get("/agent")
def agent(request: Request):
    """Self-describing manifest for AI agents."""
    _check_auth(request)
    auth = ("send header `Authorization: Bearer <HUB_TOKEN>` (OpenAI clients: api_key=HUB_TOKEN)"
            if HUB_TOKEN else "none required (HUB_TOKEN not set)")
    return {
        "service": "llamacpp",
        "role": "LLM — llama.cpp/llama-server (Metal, GGUF), OpenAI-compatible, MULTI-MODEL",
        "summary": "Serves any registered GGUF via llama.cpp's Metal backend behind the OpenAI "
                   "API. Ask for a model by name in the `model` field; the wrapper loads/swaps "
                   "to it (one resident at a time), cold-starts on first call, and idle-unloads "
                   "to free memory. The governor accounts for the loaded model and can evict it.",
        "auth": auth,
        "openai_base_url": f"http://{os.environ.get('LLAMACPP_HOST_HINT', '<host>')}:{os.environ.get('LLAMACPP_PORT','8771')}/v1",
        "models": [{"alias": a, "path": REGISTRY[a]["path"],
                    "vision": bool(REGISTRY[a].get("mmproj")),
                    "loaded": (a == _current_alias and _warmed)} for a in sorted(REGISTRY)],
        "default_model": DEFAULT_ALIAS,
        "loaded_model": _current_alias,
        "methods": [
            {"name": "chat", "http": "POST /v1/chat/completions",
             "params": "{model:<alias>, messages[], temperature, max_tokens, stream, …}",
             "returns": "OpenAI chat completion (or SSE stream); loads/swaps to `model` first",
             "example": "curl -s $URL/v1/chat/completions -H 'content-type: application/json' "
                        "-d '{\"model\":\"agents-a1-q4\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}'"},
            {"name": "models", "http": "GET /models", "returns": "the registry menu + loaded flag"},
            {"name": "readyz", "http": "GET /readyz", "returns": "loaded alias + available_models[]"},
            {"name": "unload", "http": "POST /admin/unload", "returns": "stops the engine to free memory"},
        ],
        "instructions": (
            "1) Pick a model from GET /models, put its alias in `model`, POST /v1/chat/completions.\n"
            "2) Asking for a different model swaps the engine (unload old → load new); one resident "
            "at a time so memory stays bounded. First call to a cold model pays the load.\n"
            "3) GET /readyz to see which model is loaded; POST /admin/unload to free memory now.\n"
            "4) Prefer calling THROUGH the governor: POST :8799/llm/llamacpp/v1/chat/completions "
            "so the load is admitted + captured (never load a raw llama-server — it's invisible "
            "to the governor)."
        ),
        "openapi": "/openapi.json",
    }


@app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"])
async def proxy_v1(path: str, request: Request):
    """OpenAI-compatible proxy to the llama-server child. Reads `model` from the body,
    loads/swaps to that registered alias, streams the response, and tracks in-flight
    requests so idle-unload / a swap can't kill a live generation."""
    global _active, _last_request_at
    _check_auth(request)
    body = await request.body()
    # Resolve the requested model → alias, ensure the right child is loaded (may swap).
    requested = None
    parsed: dict = {}
    if body:
        try:
            parsed = json.loads(body)
            requested = parsed.get("model")
        except Exception:
            parsed = {}
    alias = _resolve_alias(requested)
    await _ensure_started(alias)
    # Normalize the body's `model` to the loaded alias so llama-server (which runs with
    # --alias <alias>) accepts it even when the caller sent "" / a placeholder.
    if parsed and parsed.get("model") != alias:
        parsed["model"] = alias
        body = json.dumps(parsed).encode()

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
