"""
radiogen-sidecar — synthetic radiology image generation for the Atelier hub.

Text prompt (derived from a radiology DiagnosticReport / findings) -> synthetic
chest X-ray (diffusers Stable Diffusion on MPS) -> valid DICOM bound to a FHIR
ImagingStudy's UIDs. Follows docs/SIDECAR_PATTERN.md.

Endpoints:
    GET  /healthz       liveness (always 200 while process alive)
    GET  /readyz        readiness + the LOCKED atelier-status schema
    POST /generate      {prompt, ...} -> PNG (base64) + DICOM (base64) + UIDs
    POST /admin/unload  force-unload the model now

Env:
    RADIOGEN_MODEL        HF model id (default public placeholder; set to
                          stanfordmimi/RoentGen-v2 once an HF token accepted its gate)
    IDLE_UNLOAD_SECONDS   default 180 (model-heavy)
    KEEP_WARM             default false
    HUB_TOKEN             if set, /generate + /admin/unload require Bearer auth
    RADIOGEN_PORT         default 8772 (8771 is the llamacpp sidecar)
"""
import asyncio, base64, datetime, gc, io, os, time
from contextlib import asynccontextmanager

import numpy as np
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

MODEL = os.environ.get("RADIOGEN_MODEL", "danyalmalik/stable-diffusion-chest-xray")
IDLE_UNLOAD_SECONDS = int(os.environ.get("IDLE_UNLOAD_SECONDS", "180"))
KEEP_WARM = os.environ.get("KEEP_WARM", "false").lower() == "true"
HUB_TOKEN = os.environ.get("HUB_TOKEN")
PORT = int(os.environ.get("RADIOGEN_PORT", "8774"))
DX_SOP = "1.2.840.10008.5.1.4.1.1.1.1"  # Digital X-Ray Image Storage

_pipe = None
_warmed = False
_device = "cpu"
_sem = asyncio.Semaphore(1)            # single-flight inference (MPS is single-stream)
_last_request_at = time.monotonic()
_unload_task = None
_idle_unloaded_at = None


def _auth(req: Request):
    if HUB_TOKEN and req.headers.get("authorization") != f"Bearer {HUB_TOKEN}":
        raise HTTPException(401, "missing/invalid bearer token")


async def _load_and_warm():
    global _pipe, _warmed, _device, _idle_unloaded_at
    if _warmed:
        return
    import torch
    from diffusers import StableDiffusionPipeline
    _device = "mps" if torch.backends.mps.is_available() else "cpu"
    p = StableDiffusionPipeline.from_pretrained(MODEL, safety_checker=None,
                                                torch_dtype=torch.float32)
    _pipe = p.to(_device)
    _pipe.set_progress_bar_config(disable=True)
    _pipe("warmup", num_inference_steps=1, height=512, width=512)  # warm the graph
    _warmed = True
    _idle_unloaded_at = None


async def _unload_model():
    global _pipe, _warmed, _idle_unloaded_at
    if _pipe is None:
        return
    _pipe = None
    _warmed = False
    gc.collect()
    try:
        import torch
        if torch.backends.mps.is_available():
            torch.mps.empty_cache()
    except Exception:
        pass
    _idle_unloaded_at = time.monotonic()


async def _idle_watcher():
    while True:
        await asyncio.sleep(30)
        if KEEP_WARM or not _warmed:
            continue
        if time.monotonic() - _last_request_at > IDLE_UNLOAD_SECONDS:
            await _unload_model()


@asynccontextmanager
async def lifespan(app):
    global _unload_task
    _unload_task = asyncio.create_task(_idle_watcher())
    if KEEP_WARM:
        try:
            await _load_and_warm()
        except Exception:
            pass
    yield
    if _unload_task:
        _unload_task.cancel()


app = FastAPI(title="radiogen-sidecar", lifespan=lifespan)


class GenReq(BaseModel):
    prompt: str = Field(..., description="findings/impression text driving the image")
    steps: int = 30
    guidance: float = 4.0
    study_uid: str | None = None
    series_uid: str | None = None
    sop_uid: str | None = None
    patient_id: str = "SYN-0001"
    patient_name: str = "SYNTHETIC^PATIENT"


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/readyz")
def readyz():
    return {
        "ok": True,
        "state": "warm" if _warmed else "cold",
        "warmed": _warmed,
        "model": MODEL if _warmed else (MODEL if KEEP_WARM else None),
        "configured_model": MODEL,
        "device": _device,
        "voices": None,
        "idle_seconds": round(time.monotonic() - _last_request_at, 1),
        "idle_unload_seconds": IDLE_UNLOAD_SECONDS,
        "keep_warm": KEEP_WARM,
        "last_unload_ago_s": (round(time.monotonic() - _idle_unloaded_at, 1)
                              if _idle_unloaded_at else None),
    }


def _to_dicom(img, req: GenReq) -> bytes:
    from pydicom.dataset import Dataset, FileDataset
    from pydicom.uid import ExplicitVRLittleEndian, generate_uid
    arr = np.array(img.convert("L")).astype(np.uint16)
    sop = req.sop_uid or generate_uid()
    fm = Dataset()
    fm.MediaStorageSOPClassUID = DX_SOP
    fm.MediaStorageSOPInstanceUID = sop
    fm.TransferSyntaxUID = ExplicitVRLittleEndian
    fm.ImplementationClassUID = generate_uid()
    ds = FileDataset("img.dcm", {}, file_meta=fm, preamble=b"\0" * 128)
    now = datetime.datetime.now()
    ds.PatientName, ds.PatientID, ds.Modality = req.patient_name, req.patient_id, "DX"
    ds.StudyInstanceUID = req.study_uid or generate_uid()
    ds.SeriesInstanceUID = req.series_uid or generate_uid()
    ds.SOPClassUID, ds.SOPInstanceUID = DX_SOP, sop
    ds.StudyDate = ds.ContentDate = now.strftime("%Y%m%d")
    ds.ContentTime = now.strftime("%H%M%S")
    ds.StudyDescription = "Synthetic Chest X-ray (AI-generated)"
    ds.SeriesDescription = req.prompt[:60]
    ds.ImageComments = "SYNTHETIC / AI-GENERATED - NOT A REAL PATIENT"
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.Rows, ds.Columns = arr.shape
    ds.BitsAllocated = ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 0
    ds.PixelData = arr.tobytes()
    ds.is_little_endian, ds.is_implicit_VR = True, False
    buf = io.BytesIO()
    ds.save_as(buf)
    return buf.getvalue(), ds.StudyInstanceUID, ds.SeriesInstanceUID, sop


@app.post("/generate")
async def generate(req: GenReq, request: Request):
    global _last_request_at
    _auth(request)
    async with _sem:
        _last_request_at = time.monotonic()
        await _load_and_warm()
        img = _pipe(req.prompt, num_inference_steps=req.steps,
                    guidance_scale=req.guidance, height=512, width=512).images[0]
        png = io.BytesIO(); img.convert("L").save(png, format="PNG")
        dcm_bytes, study, series, sop = _to_dicom(img, req)
        _last_request_at = time.monotonic()
    return {
        "ok": True, "model": MODEL, "device": _device,
        "study_uid": study, "series_uid": series, "sop_uid": sop,
        "png_base64": base64.b64encode(png.getvalue()).decode(),
        "dicom_base64": base64.b64encode(dcm_bytes).decode(),
    }


@app.post("/admin/unload")
async def admin_unload(request: Request):
    _auth(request)
    await _unload_model()
    return {"ok": True, "state": "cold"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
