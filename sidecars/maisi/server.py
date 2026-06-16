"""maisi-sidecar — Atelier CLOUD-PASSTHROUGH sidecar for 3D CT (MONAI MAISI on Modal).
/readyz reports REAL backend reachability (deployed/unavailable, cached 30s) + log pointers."""
import asyncio, os, time
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

MODAL_APP = os.environ.get("MAISI_MODAL_APP", "atelier-maisi")
MODAL_FN = os.environ.get("MAISI_MODAL_FN", "generate")
PORT = int(os.environ.get("MAISI_PORT", "8775"))
HUB_TOKEN = os.environ.get("HUB_TOKEN")
LOG_OUT = os.path.expanduser("~/Library/Logs/maisi-sidecar.out.log")
LOG_ERR = os.path.expanduser("~/Library/Logs/maisi-sidecar.err.log")
_last_request_at = time.monotonic()
_sem = asyncio.Semaphore(1)
_backend_up = None; _backend_checked = 0.0; _backend_err = None

def _auth(req):
    if HUB_TOKEN and req.headers.get("authorization") != f"Bearer {HUB_TOKEN}":
        raise HTTPException(401, "bad token")

def _check_backend(ttl=30.0):
    """Real reachability: can we resolve the DEPLOYED Modal function? Cached ttl secs."""
    global _backend_up, _backend_checked, _backend_err
    now = time.monotonic()
    if _backend_up is not None and (now - _backend_checked) < ttl:
        return _backend_up
    try:
        import modal
        modal.Function.from_name(MODAL_APP, MODAL_FN)  # raises if app not deployed
        _backend_up, _backend_err = True, None
    except Exception as e:
        _backend_up, _backend_err = False, str(e)[:160]
    _backend_checked = now
    return _backend_up

app = FastAPI(title="maisi-sidecar")

class GenReq(BaseModel):
    prompt: str = "abdomen CT, normal anatomy"
    body_region: str = "abdomen"

@app.get("/healthz")
def healthz():
    return {"ok": True}

@app.get("/readyz")
def readyz():
    up = _check_backend()
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
        # REAL backend health — true if the deployed Modal app is reachable, else "power gone"
        "backend_status": "available" if up else "unavailable",
        "remote_reachable": bool(up),
        "backend_error": _backend_err,
        "last_backend_check_s": round(time.monotonic() - _backend_checked, 1),
        "logs": {"local_out": LOG_OUT, "local_err": LOG_ERR,
                 "remote_cmd": f"modal app logs {MODAL_APP}"},
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
    if not _check_backend(ttl=0):
        raise HTTPException(503, f"modal backend unavailable: {_backend_err}")
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
