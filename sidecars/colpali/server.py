"""
colpali-sidecar — ColPali (late-interaction visual document retrieval) on the atelier hub.

GOVERNED via the shared lifecycle framework (sidecars/_common/lifecycle.py) — it inherits the
3-law constitution automatically: no memory leaks (MPS self-restart reclaim), never unload while
scoring, and a request queue. This file holds ONLY ColPali's model + endpoints; all lifecycle,
memory, busy-guard, queue, /healthz, /readyz, /admin/unload come from GovernedSidecar.

  POST /score          — query text + candidate page images → MaxSim late-interaction scores
  POST /embed_images   — page images → multi-vector embeddings (fp16 arrays) for storage
  POST /embed_queries  — query texts → multi-vector query embeddings
  GET  /healthz /readyz · POST /admin/unload   (from the framework)

Env: COLPALI_MODEL (default vidore/colpali-v1.3) · COLPALI_DEVICE · COLPALI_PORT (8779) ·
     IDLE_UNLOAD_SECONDS (180) · KEEP_WARM · HUB_TOKEN · RECLAIM_THRESHOLD_GB · COLPALI_MAX_CONCURRENCY
"""
import base64
import io
import os
from types import SimpleNamespace

import torch
from fastapi import FastAPI, HTTPException, Request
from PIL import Image

from lifecycle import GovernedSidecar

MODEL_ID = os.environ.get("COLPALI_MODEL", "vidore/colpali-v1.3")
PORT = int(os.environ.get("COLPALI_PORT", "8779"))


def _load():
    """Cold-load ColPali + its processor onto the device. Returns the handle the framework
    holds; dropping it (on unload) releases both refs so gc + empty_cache can reclaim."""
    from colpali_engine.models import ColPali, ColPaliProcessor   # lazy — heavy import
    dev = sc.device
    dtype = torch.float16 if dev != "cpu" else torch.float32
    print(f"[colpali] loading {MODEL_ID} on {dev} ({dtype})", flush=True)
    model = ColPali.from_pretrained(MODEL_ID, torch_dtype=dtype, device_map=dev).eval()
    processor = ColPaliProcessor.from_pretrained(MODEL_ID)
    return SimpleNamespace(model=model, processor=processor)


sc = GovernedSidecar(
    "colpali",
    role="visual doc retrieval (ColPali, MaxSim late-interaction)",
    load_fn=_load,
    model_name=MODEL_ID,
    idle_unload_s=int(os.environ.get("IDLE_UNLOAD_SECONDS", "180")),
    # single-flight on unified memory; framework reads COLPALI_MAX_CONCURRENCY to override
)

app = FastAPI(lifespan=sc.lifespan())
sc.attach(app)   # GET /healthz /readyz + POST /admin/unload


@app.get("/agent")
def agent():
    """Self-describing usage manifest (governor /agent?expand aggregates this)."""
    return {
        "service": "colpali",
        "role": "Visual-document retrieval — ColPali MaxSim late-interaction scoring + multi-vector embeddings",
        "device": sc.device,
        "model": MODEL_ID,
        "endpoints": {
            "POST /score": "{query:str, images:[base64|path]} -> {scores:[float]} "
                           "(MaxSim late interaction; score page/figure crops on demand)",
            "POST /embed_images": "{images:[base64|path]} -> {embeddings:[multi-vector]} (fp16 visual)",
            "POST /embed_queries": "{queries:[str]} -> {embeddings:[multi-vector]}",
        },
        "notes": "governed MPS tenant; images are base64 so the driver (Linux) and sidecar (Mac) can differ.",
    }


def _pil(img: str) -> Image.Image:
    """Accept a base64 PNG/JPEG or an absolute path."""
    if os.path.isabs(img) and os.path.exists(img):
        return Image.open(img).convert("RGB")
    return Image.open(io.BytesIO(base64.b64decode(img))).convert("RGB")


@torch.no_grad()
def _embed_images(h, pils):
    batch = h.processor.process_images(pils).to(h.model.device)
    return h.model(**batch)                # (n, patches, dim) multi-vector, on device


@torch.no_grad()
def _embed_queries(h, texts):
    batch = h.processor.process_queries(texts).to(h.model.device)
    return h.model(**batch)


@app.post("/score")
async def score(request: Request):
    """{query: str, images: [base64|path]} → {scores: [float]} via ColPali MaxSim late interaction."""
    sc.check_auth(request)
    body = await request.json()
    query = body.get("query")
    images = body.get("images") or []
    if not query or not images:
        raise HTTPException(400, "need 'query' (str) and 'images' (non-empty list)")

    def _work(h):
        pils = [_pil(i) for i in images]
        img_emb = _embed_images(h, pils)
        q_emb = _embed_queries(h, [query])
        scores = h.processor.score_multi_vector(q_emb, img_emb)   # (1, n) MaxSim
        return [float(x) for x in scores[0].tolist()]

    async with sc.job() as h:               # queue slot + busy guard + lazy load
        result = await sc.run(_work, h)     # blocking model call off the event loop
    return {"query": query, "scores": result, "model": MODEL_ID}


@app.post("/embed_images")
async def embed_images(request: Request):
    """{images: [base64|path]} → {embeddings: [ [ [float,…], … ], … ]} multi-vector (fp16 → list)."""
    sc.check_auth(request)
    body = await request.json()
    images = body.get("images") or []
    if not images:
        raise HTTPException(400, "need 'images' (non-empty list)")

    def _work(h):
        emb = _embed_images(h, [_pil(i) for i in images])         # (n, patches, dim)
        return [e.to(torch.float32).cpu().numpy().tolist() for e in emb], int(emb.shape[-1])

    async with sc.job() as h:
        out, dim = await sc.run(_work, h)
    return {"embeddings": out, "dim": dim, "model": MODEL_ID}


@app.post("/embed_queries")
async def embed_queries(request: Request):
    """{queries: [str]} → {embeddings: [...]} multi-vector query embeddings."""
    sc.check_auth(request)
    body = await request.json()
    queries = body.get("queries") or []
    if not queries:
        raise HTTPException(400, "need 'queries' (non-empty list)")

    def _work(h):
        emb = _embed_queries(h, queries)
        return [e.to(torch.float32).cpu().numpy().tolist() for e in emb], int(emb.shape[-1])

    async with sc.job() as h:
        out, dim = await sc.run(_work, h)
    return {"embeddings": out, "dim": dim, "model": MODEL_ID}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
