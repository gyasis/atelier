"""maisi-sidecar — Atelier CLOUD-PASSTHROUGH sidecar for 3D CT (MONAI MAISI on Modal).
Local LAN endpoint; forwards /generate to the deployed Modal A100 app 'atelier-maisi'.
$0 when idle (Modal scales to zero). First of the 'passthrough cloud provider' sidecars.
"""
import asyncio, os, time
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

MODAL_APP = os.environ.get("MAISI_MODAL_APP", "atelier-maisi")
MODAL_FN = os.environ.get("MAISI_MODAL_FN", "generate")
PORT = int(os.environ.get("MAISI_PORT", "8775"))
HUB_TOKEN = os.environ.get("HUB_TOKEN")
_last_request_at = time.monotonic()
_sem = asyncio.Semaphore(1)

def _auth(req: Request):
    if HUB_TOKEN and req.headers.get("authorization") != f"Bearer {HUB_TOKEN}":
        raise HTTPException(401, "bad token")

def _modal_reachable() -> bool:
    try:
        import modal
        modal.Function.from_name(MODAL_APP, MODAL_FN)  # lazy resolve
        return True
    except Exception:
        return False

app = FastAPI(title="maisi-sidecar")

class GenReq(BaseModel):
    prompt: str = "abdomen CT, normal"
    body_region: str = "abdomen"
    study_uid: str | None = None
    series_uid: str | None = None

@app.get("/healthz")
def healthz():
    return {"ok": True}

@app.get("/readyz")
def readyz():
    # cloud-passthrough: compute is REMOTE (Modal A100), $0 idle, scales to zero
    return {
        "ok": True,
        "state": "remote",
        "warmed": True,
        "model": "MONAI-MAISI-3D-CT",
        "configured_model": "MONAI-MAISI-3D-CT",
        "device": "A100 (modal cloud)",
        "backend": "modal",
        "passthrough": True,
        "remote_app": MODAL_APP,
        "remote_reachable": _modal_reachable(),
        "voices": None,
        "idle_seconds": round(time.monotonic() - _last_request_at, 1),
        "idle_unload_seconds": 0,
        "keep_warm": False,
        "last_unload_ago_s": None,
    }

@app.post("/generate")
async def generate(req: GenReq, request: Request):
    global _last_request_at
    _auth(request)
    async with _sem:
        _last_request_at = time.monotonic()
        import modal
        fn = modal.Function.from_name(MODAL_APP, MODAL_FN)
        result = await asyncio.to_thread(fn.remote, req.dict())
        _last_request_at = time.monotonic()
    return {"ok": True, "backend": "modal", "via": "maisi-sidecar", "result": result}

@app.post("/admin/unload")
async def admin_unload(request: Request):
    _auth(request)
    return {"ok": True, "note": "remote (Modal) scales to zero automatically"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
