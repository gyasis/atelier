"""maisi-sidecar — Atelier CLOUD-PASSTHROUGH sidecar for 3D CT (MONAI MAISI on Modal).
Auto-emits, per generated CT: <id>.nii.gz + <id>.png + <id>-viewer.html (self-contained NiiVue).
/readyz reports real backend reachability + log pointers."""
import asyncio, os, time, base64, datetime
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

MODAL_APP = os.environ.get("MAISI_MODAL_APP", "atelier-maisi")
MODAL_FN = os.environ.get("MAISI_MODAL_FN", "generate")
PORT = int(os.environ.get("MAISI_PORT", "8775"))
OUTDIR = os.path.expanduser(os.environ.get("MAISI_OUTPUT_DIR", "~/Documents/generated/maisi"))
HUB_TOKEN = os.environ.get("HUB_TOKEN")
LOG_OUT = os.path.expanduser("~/Library/Logs/maisi-sidecar.out.log")
LOG_ERR = os.path.expanduser("~/Library/Logs/maisi-sidecar.err.log")
_last_request_at = time.monotonic()
_sem = asyncio.Semaphore(1)
_backend_up = None; _backend_checked = 0.0; _backend_err = None

VIEWER = '''<!doctype html><html><head><meta charset="utf-8"><title>__TITLE__</title>
<style>html,body{margin:0;height:100%;background:#000;font-family:system-ui,sans-serif;color:#ddd}
#c{width:100vw;height:100vh;display:block}.bar{position:fixed;top:8px;left:8px;z-index:10;background:#000a;padding:8px 12px;border-radius:8px;font-size:13px;max-width:64vw}.bar b{color:#fff}
button{background:#333;color:#eee;border:1px solid #555;border-radius:5px;padding:3px 8px;margin-left:6px;cursor:pointer}</style></head><body>
<div class="bar"><b>__TITLE__</b> &middot; scroll=slice, right-drag=window.
<button onclick="win(-160,240)">soft tissue</button><button onclick="win(-1000,400)">lung</button><button onclick="win(-160,1040)">bone</button>
&middot; drop a .nii.gz/.dcm to load another. <span id="i"></span></div><canvas id="c"></canvas>
<script src="https://unpkg.com/@niivue/niivue@latest/dist/niivue.umd.js"></script><script>
const B64="__B64__";function buf(b){const s=atob(b),u=new Uint8Array(s.length);for(let i=0;i<s.length;i++)u[i]=s.charCodeAt(i);return u.buffer;}
const nv=new niivue.Niivue({backColor:[0,0,0,1],show3Dcrosshair:true});nv.attachTo("c");
const url=URL.createObjectURL(new Blob([buf(B64)]));
nv.loadVolumes([{url:url,name:"ct.nii.gz",colormap:"gray"}]).then(function(){win(-160,240);document.getElementById("i").textContent="loaded \\u2713";});
function win(lo,hi){if(!nv.volumes.length)return;nv.volumes[0].cal_min=lo;nv.volumes[0].cal_max=hi;nv.updateGLVolume();}
</script></body></html>'''

def _auth(req):
    if HUB_TOKEN and req.headers.get("authorization") != f"Bearer {HUB_TOKEN}":
        raise HTTPException(401, "bad token")

def _check_backend(ttl=30.0):
    global _backend_up, _backend_checked, _backend_err
    now = time.monotonic()
    if _backend_up is not None and (now - _backend_checked) < ttl:
        return _backend_up
    try:
        import modal; modal.Function.from_name(MODAL_APP, MODAL_FN)
        _backend_up, _backend_err = True, None
    except Exception as e:
        _backend_up, _backend_err = False, str(e)[:160]
    _backend_checked = now
    return _backend_up

def _emit(result: dict, label: str = "ct") -> dict:
    """Write <id>.nii.gz + <id>.png + <id>-viewer.html (embedded) into OUTDIR."""
    os.makedirs(OUTDIR, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    base = os.path.join(OUTDIR, f"{label}_{stamp}")
    paths = {}
    nb = result.get("nifti_base64")
    if nb:
        open(base + ".nii.gz", "wb").write(base64.b64decode(nb)); paths["nifti"] = base + ".nii.gz"
        html = VIEWER.replace("__B64__", nb).replace("__TITLE__", f"MONAI MAISI CT — {label} {stamp}")
        open(base + "-viewer.html", "w").write(html); paths["viewer"] = base + "-viewer.html"
    if result.get("png_base64"):
        open(base + ".png", "wb").write(base64.b64decode(result["png_base64"])); paths["slice_png"] = base + ".png"
    return paths

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
    return {"ok": True, "state": "remote", "warmed": True, "model": "MONAI-MAISI-3D-CT",
            "configured_model": "MONAI-MAISI-3D-CT", "device": "A100 (modal cloud)",
            "backend": "modal", "passthrough": True, "remote_app": MODAL_APP,
            "backend_status": "available" if up else "unavailable", "remote_reachable": bool(up),
            "backend_error": _backend_err, "last_backend_check_s": round(time.monotonic() - _backend_checked, 1),
            "output_dir": OUTDIR, "auto_viewer": True,
            "logs": {"local_out": LOG_OUT, "local_err": LOG_ERR, "remote_cmd": f"modal app logs {MODAL_APP}"},
            "voices": None, "idle_seconds": round(time.monotonic() - _last_request_at, 1),
            "idle_unload_seconds": 0, "keep_warm": False, "last_unload_ago_s": None}

@app.post("/generate")
async def generate(req: GenReq, request: Request):
    global _last_request_at
    _auth(request)
    if not _check_backend(ttl=0):
        raise HTTPException(503, f"modal backend unavailable: {_backend_err}")
    async with _sem:
        _last_request_at = time.monotonic()
        import modal
        result = await asyncio.to_thread(modal.Function.from_name(MODAL_APP, MODAL_FN).remote, req.dict())
        _last_request_at = time.monotonic()
    paths = _emit(result, "ct") if result.get("ok") else {}
    meta = {k: v for k, v in result.items() if "base64" not in k}
    return {"ok": result.get("ok", False), "backend": "modal", "via": "maisi-sidecar", "paths": paths, "meta": meta}

@app.post("/fetch")
async def fetch(request: Request):
    """Pull the last already-generated CT from the Modal Volume + emit its viewer (fast, no GPU)."""
    _auth(request)
    import modal
    result = await asyncio.to_thread(modal.Function.from_name(MODAL_APP, "fetch_existing").remote, "image")
    paths = _emit(result, "ct_last") if result.get("ok") else {}
    meta = {k: v for k, v in result.items() if "base64" not in k}
    return {"ok": result.get("ok", False), "paths": paths, "meta": meta}

@app.post("/admin/unload")
async def admin_unload(request: Request):
    _auth(request)
    return {"ok": True, "note": "remote (Modal) scales to zero automatically"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
