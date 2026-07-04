"""
colpali-sidecar — ColPali (late-interaction visual document retrieval) on the atelier hub.

Follows ~/Documents/code/atelier/docs/SIDECAR_PATTERN.md:
  GET  /healthz, /readyz
  POST /score          — query text + candidate page images → MaxSim late-interaction scores
  POST /embed_images   — page images → multi-vector embeddings (base64 float16 arrays) for storage
  POST /embed_queries  — query texts → multi-vector query embeddings
  POST /admin/unload   — force-unload NOW (unified-memory eviction)

ColPali is MULTI-VECTOR (a set of patch embeddings per page) scored by late interaction (MaxSim),
NOT a single dense vector — so the primary use is /score: rank candidate pages against a query.

Env:
  COLPALI_MODEL          default "vidore/colpali-v1.3" (public HF id; PaliGemma-based)
  COLPALI_DEVICE         default auto (mps on Apple Silicon)
  COLPALI_PORT           default 8779
  IDLE_UNLOAD_SECONDS    default 180
  KEEP_WARM              default false
  HUB_TOKEN              optional bearer for /score + /admin/unload
"""
import asyncio
import base64
import gc
import io
import os
import time
from contextlib import asynccontextmanager

import numpy as np
import torch
from fastapi import FastAPI, HTTPException, Request
from PIL import Image

MODEL_ID = os.environ.get("COLPALI_MODEL", "vidore/colpali-v1.3")


def _auto_device() -> str:
    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


DEVICE = os.environ.get("COLPALI_DEVICE", _auto_device())
PORT = int(os.environ.get("COLPALI_PORT", "8779"))
IDLE_UNLOAD_SECONDS = int(os.environ.get("IDLE_UNLOAD_SECONDS", "180"))
KEEP_WARM = os.environ.get("KEEP_WARM", "false").lower() in ("1", "true", "yes")
HUB_TOKEN = os.environ.get("HUB_TOKEN")
DTYPE = torch.float16 if DEVICE != "cpu" else torch.float32
IDLE_TICK_SECONDS = 30

_model = None
_processor = None
_warmed = False
_sem = asyncio.Semaphore(1)               # single-flight inference (unified-memory safety)
_last_request_at = time.monotonic()
_idle_unloaded_at = None
_unload_task = None


async def _load_and_warm():
    """Idempotent cold-load of ColPali + processor onto the device."""
    global _model, _processor, _warmed
    if _model is not None:
        return
    from colpali_engine.models import ColPali, ColPaliProcessor  # lazy — heavy
    print(f"[colpali] loading {MODEL_ID} on {DEVICE} ({DTYPE})", flush=True)
    _model = await asyncio.to_thread(
        lambda: ColPali.from_pretrained(MODEL_ID, torch_dtype=DTYPE, device_map=DEVICE).eval())
    _processor = await asyncio.to_thread(lambda: ColPaliProcessor.from_pretrained(MODEL_ID))
    _warmed = True
    print("[colpali] ready", flush=True)


async def _unload_model():
    """del model + gc + empty the MPS/CUDA cache — the allocator otherwise keeps it resident."""
    global _model, _processor, _warmed, _idle_unloaded_at
    if _model is None:
        return
    _model = None
    _processor = None
    _warmed = False
    gc.collect()
    if DEVICE == "mps":
        torch.mps.empty_cache()
    elif DEVICE == "cuda":
        torch.cuda.empty_cache()
    _idle_unloaded_at = time.monotonic()
    print("[colpali] unloaded", flush=True)


async def _idle_watcher():
    global _unload_task
    while True:
        await asyncio.sleep(IDLE_TICK_SECONDS)
        if KEEP_WARM or _model is None:
            continue
        if time.monotonic() - _last_request_at > IDLE_UNLOAD_SECONDS and _sem._value == 1:
            async with _sem:
                await _unload_model()


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_idle_watcher())
    if KEEP_WARM:
        await _load_and_warm()
    yield
    task.cancel()
    await _unload_model()


app = FastAPI(lifespan=lifespan)


def _auth(request: Request):
    if HUB_TOKEN and request.headers.get("authorization") != f"Bearer {HUB_TOKEN}":
        raise HTTPException(401, "bad or missing bearer token")


def _pil(img: str) -> Image.Image:
    """Accept a base64 PNG/JPEG or an absolute path."""
    if os.path.isabs(img) and os.path.exists(img):
        return Image.open(img).convert("RGB")
    return Image.open(io.BytesIO(base64.b64decode(img))).convert("RGB")


@torch.no_grad()
def _embed_images(pils):
    batch = _processor.process_images(pils).to(_model.device)
    return _model(**batch)                # (n, patches, dim) multi-vector, on device


@torch.no_grad()
def _embed_queries(texts):
    batch = _processor.process_queries(texts).to(_model.device)
    return _model(**batch)


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/readyz")
async def readyz():
    return {
        "ok": True,
        "state": "warm" if _warmed else "cold",
        "warmed": _warmed,
        "model": MODEL_ID if _warmed else None,
        "device": DEVICE,
        "voices": None,
        "idle_seconds": round(time.monotonic() - _last_request_at, 1),
        "idle_unload_seconds": IDLE_UNLOAD_SECONDS,
        "keep_warm": KEEP_WARM,
        "last_unload_ago_s": (round(time.monotonic() - _idle_unloaded_at, 1)
                              if _idle_unloaded_at else None),
    }


@app.post("/score")
async def score(request: Request):
    """{query: str, images: [base64|path]} → {scores: [float]} via ColPali MaxSim late interaction."""
    _auth(request)
    global _last_request_at
    body = await request.json()
    query = body.get("query")
    images = body.get("images") or []
    if not query or not images:
        raise HTTPException(400, "need 'query' (str) and 'images' (non-empty list)")
    async with _sem:
        await _load_and_warm()
        _last_request_at = time.monotonic()
        pils = [_pil(i) for i in images]
        img_emb = _embed_images(pils)
        q_emb = _embed_queries([query])
        scores = _processor.score_multi_vector(q_emb, img_emb)  # (1, n) MaxSim
        _last_request_at = time.monotonic()
        return {"query": query, "scores": [float(x) for x in scores[0].tolist()],
                "model": MODEL_ID}


@app.post("/embed_images")
async def embed_images(request: Request):
    """{images: [base64|path]} → {embeddings: [ [ [float,…], … ], … ]} multi-vector (fp16 → list)."""
    _auth(request)
    global _last_request_at
    body = await request.json()
    images = body.get("images") or []
    if not images:
        raise HTTPException(400, "need 'images' (non-empty list)")
    async with _sem:
        await _load_and_warm()
        _last_request_at = time.monotonic()
        emb = _embed_images([_pil(i) for i in images])          # (n, patches, dim)
        out = [e.to(torch.float32).cpu().numpy().tolist() for e in emb]
        _last_request_at = time.monotonic()
        return {"embeddings": out, "dim": int(emb.shape[-1]), "model": MODEL_ID}


@app.post("/admin/unload")
async def admin_unload(request: Request):
    _auth(request)
    async with _sem:
        await _unload_model()
    return {"ok": True, "unloaded": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
