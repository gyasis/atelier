#!/usr/bin/env python3
"""Atelier rerank sidecar — cross-encoder reranking on MPS, offloaded off the driver box.

Serves paperlake's exact contract:  POST /rerank {model, query, passages} -> {scores:[...]}
(scores are relevance logits, higher = more relevant; the caller sorts descending).

Governed like the other MPS python sidecars: lazy load, idle-unload, and the R-AG4b
RSS self-reclaim (empty_cache; if RSS stays > threshold, exit 42 so launchd KeepAlive
{SuccessfulExit:false} relaunches cold — the only reliable Metal RSS reclaim).
"""
import os, gc, time, threading, subprocess, sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel
import uvicorn

MODEL_NAME = os.environ.get("RERANK_MODEL", "BAAI/bge-reranker-v2-m3")
PORT = int(os.environ.get("RERANK_PORT", "8778"))
DEVICE = os.environ.get("RERANK_DEVICE", "mps")
MAX_LENGTH = int(os.environ.get("RERANK_MAX_LENGTH", "512"))
IDLE_UNLOAD_S = int(os.environ.get("RERANK_IDLE_UNLOAD_S", "600"))
RECLAIM_THRESHOLD_GB = float(os.environ.get("RECLAIM_THRESHOLD_GB", "1.5"))

@asynccontextmanager
async def _lifespan(app):
    """Start the idle-unload watcher from the APP, not from `__main__`.

    This works today only because launchd runs `python server.py`. The moment anyone
    switches the plist to `-m uvicorn server:app` (as most sidecars here use), the module
    is imported, `__main__` never runs, and IDLE_UNLOAD silently becomes dead config —
    which is exactly how pyannote and audio-llm ended up resident forever. A lifespan
    fires on both launch paths, so the timer can't be disarmed by a plist edit."""
    threading.Thread(target=_idle_watch, daemon=True).start()
    print(f"[rerank] idle-watcher started (unload after {IDLE_UNLOAD_S}s idle)", flush=True)
    yield


app = FastAPI(title="atelier-rerank", version="1.0", lifespan=_lifespan)
_lock = threading.Lock()
_model = None
_last_used = time.time()
_warmed = False


def _rss_gb() -> float:
    try:
        import psutil
        return psutil.Process().memory_info().rss / (1024 ** 3)
    except Exception:
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(os.getpid())],
                             capture_output=True, text=True).stdout.strip()
        return (int(out) / (1024 ** 2)) if out else 0.0


def _empty_cache():
    try:
        import torch
        if hasattr(torch, "mps"):
            torch.mps.empty_cache()
    except Exception:
        pass


def _get_model():
    global _model, _warmed
    with _lock:
        if _model is None:
            from sentence_transformers import CrossEncoder
            _model = CrossEncoder(MODEL_NAME, device=DEVICE, max_length=MAX_LENGTH)
            _warmed = True
        return _model


def _unload(reason: str) -> dict:
    """Free the model; if Metal won't return the RSS, self-restart (R-AG4b)."""
    global _model, _warmed
    with _lock:
        _model = None
        _warmed = False
    gc.collect()
    _empty_cache()
    rss = _rss_gb()
    if rss > RECLAIM_THRESHOLD_GB:
        # graceful soft-unload didn't reclaim on Metal → hard restart
        threading.Thread(target=lambda: (time.sleep(0.3), os._exit(42)), daemon=True).start()
        return {"unloaded": True, "reclaim": "process-restart", "rss_gb": round(rss, 2), "reason": reason}
    return {"unloaded": True, "reclaim": "empty_cache", "rss_gb": round(rss, 2), "reason": reason}


def _idle_watch():
    while True:
        time.sleep(30)
        if _model is not None and (time.time() - _last_used) > IDLE_UNLOAD_S:
            _unload("idle")


class RerankReq(BaseModel):
    query: str
    passages: list[str]
    model: str | None = None


@app.post("/rerank")
def rerank(req: RerankReq):
    global _last_used
    _last_used = time.time()
    if not req.passages:
        return {"scores": [], "model": MODEL_NAME, "device": DEVICE}
    m = _get_model()
    scores = m.predict([(req.query, p) for p in req.passages])
    _last_used = time.time()
    return {"scores": [float(x) for x in scores], "model": MODEL_NAME, "device": DEVICE}


@app.get("/healthz")
def healthz():
    return {"ok": True, "service": "rerank", "model": MODEL_NAME}


@app.get("/readyz")
def readyz():
    return {"ok": True, "state": "warm" if _model is not None else "cold",
            "warmed": _warmed, "model": MODEL_NAME, "device": DEVICE,
            "idle_seconds": round(time.time() - _last_used, 1),
            "idle_unload_seconds": IDLE_UNLOAD_S, "rss_gb": round(_rss_gb(), 2)}


@app.get("/v1/models")
def models():
    return {"data": [{"id": MODEL_NAME, "object": "model"}]}


@app.get("/agent")
def agent():
    """Self-describing usage manifest (governor /agent?expand aggregates this)."""
    return {
        "service": "rerank",
        "role": "Rerank — cross-encoder reranking (query, passages -> relevance scores)",
        "device": DEVICE,
        "model": MODEL_NAME,
        "endpoints": {
            "POST /rerank": "{query:str, passages:[str], model?:str} -> "
                            "{scores:[float], model, device} (higher = more relevant)",
            "GET /v1/models": "the served cross-encoder id",
        },
        "notes": "governed MPS tenant; scores align 1:1 with the input passages order.",
    }


@app.post("/admin/unload")
def admin_unload():
    return _unload("admin")


if __name__ == "__main__":
    # Watcher now starts in _lifespan (uvicorn.run fires it too) — starting it here as
    # well would run two watcher threads.
    print(f"[rerank] serving {MODEL_NAME} on :{PORT} device={DEVICE}", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
