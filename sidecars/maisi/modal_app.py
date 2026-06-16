"""atelier-maisi — Modal A100: MONAI MAISI 3D CT synthesis.
v3: selects the CT IMAGE output (continuous HU), not the binary mask; auto-windows."""
import modal

app = modal.App("atelier-maisi")
image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("git")
    .pip_install("torch", "monai[all]==1.5.0", "nibabel", "numpy", "pillow",
                 "huggingface_hub", "scipy", "einops", "tqdm")
)
vol = modal.Volume.from_name("maisi-cache", create_if_missing=True)
A100_USD_PER_SEC = 2.10 / 3600.0

@app.function(gpu="A100", image=image, timeout=3600, volumes={"/cache": vol})
def generate(params: dict) -> dict:
    import time, io, os, base64, glob, traceback
    import numpy as np
    t0 = time.perf_counter()
    BUNDLE_DIR = "/cache/bundles"; NAME = "maisi_ct_generative"
    try:
        from monai.bundle import download, create_workflow
        broot = os.path.join(BUNDLE_DIR, NAME)
        if not os.path.isdir(broot):
            download(name=NAME, bundle_dir=BUNDLE_DIR); vol.commit()
        t_dl = time.perf_counter() - t0
        outdir = "/cache/out2"; os.makedirs(outdir, exist_ok=True)
        cfg = os.path.join(broot, "configs", "inference.json")
        wf = create_workflow(config_file=cfg, bundle_root=broot, workflow_type="inference",
                             **{"num_output_samples": 1, "output_size": [256, 256, 128],
                                "spacing": [1.5, 1.5, 1.5], "output_dir": outdir})
        t_inf0 = time.perf_counter(); wf.run(); t_inf = time.perf_counter() - t_inf0

        import nibabel as nib
        cands = sorted(set(glob.glob(os.path.join(outdir, "**", "*.nii.gz"), recursive=True)
                           + glob.glob(os.path.join(broot, "**", "*.nii.gz"), recursive=True)),
                       key=os.path.getmtime)
        files = []
        best = None; best_uniq = -1
        for f in cands:
            a = np.asarray(nib.load(f).dataobj)
            u = int(np.unique(a).size); mn, mx = float(a.min()), float(a.max())
            files.append({"name": os.path.basename(f), "uniq": u, "min": round(mn, 1), "max": round(mx, 1)})
            if u > best_uniq:   # CT image = most unique values (mask is binary)
                best_uniq = u; best = (f, a)
        if best is None or best_uniq <= 10:
            return {"ok": False, "error": "no continuous CT image among outputs (only masks?)",
                    "files": files, "seconds": round(time.perf_counter() - t0, 1)}
        fpath, arr = best
        arr = arr.astype(np.float32)
        mn, mx = float(arr.min()), float(arr.max())
        if mn < -100:               # HU -> soft-tissue window WL40/WW400
            lo, hi = -160.0, 240.0
        else:                        # normalized -> min-max
            lo, hi = mn, mx
        ax = arr[:, :, arr.shape[2] // 2]
        sl = np.clip((ax - lo) / (hi - lo + 1e-6), 0, 1)
        sl = (np.rot90(sl) * 255).astype(np.uint8)
        from PIL import Image
        png = io.BytesIO(); Image.fromarray(sl).save(png, format="PNG")
        elapsed = time.perf_counter() - t0
        return {"ok": True, "backend": "modal", "model": "MONAI-MAISI",
                "image_file": os.path.basename(fpath), "image_uniq": best_uniq,
                "image_range": [round(mn, 1), round(mx, 1)], "shape": list(arr.shape),
                "files": files, "download_seconds": round(t_dl, 1),
                "inference_seconds": round(t_inf, 1), "total_seconds": round(elapsed, 1),
                "est_cost_usd": round(elapsed * A100_USD_PER_SEC, 4),
                "png_base64": base64.b64encode(png.getvalue()).decode(),
                "nifti_base64": base64.b64encode(open(fpath, "rb").read()).decode()}
    except Exception as e:
        return {"ok": False, "error": str(e)[:500], "trace": traceback.format_exc()[-1500:],
                "seconds": round(time.perf_counter() - t0, 1)}

@app.local_entrypoint()
def main():
    import json
    print(json.dumps({k: v for k, v in generate.remote({}).items() if "base64" not in k}, indent=2))

@app.function(image=image, volumes={"/cache": vol}, timeout=300)
def fetch_existing(which: str = "image") -> dict:
    """Read the most recent already-generated CT *_image.nii.gz from the Volume (no GPU)."""
    import glob, os, io, base64, numpy as np, nibabel as nib
    pats = sorted(glob.glob(f"/cache/out*/**/*_{which}.nii.gz", recursive=True), key=os.path.getmtime)
    allf = sorted(glob.glob("/cache/out*/**/*.nii.gz", recursive=True), key=os.path.getmtime)
    if not pats:
        return {"ok": False, "error": f"no *_{which}.nii.gz in volume", "all_files": [os.path.basename(x) for x in allf][-10:]}
    f = pats[-1]
    a = np.asarray(nib.load(f).dataobj).astype(np.float32)
    mn, mx = float(a.min()), float(a.max())
    lo, hi = (-160.0, 240.0) if mn < -100 else (mn, mx)
    sl = a[:, :, a.shape[2] // 2]
    sl = (np.rot90(np.clip((sl - lo) / (hi - lo + 1e-6), 0, 1)) * 255).astype(np.uint8)
    from PIL import Image
    png = io.BytesIO(); Image.fromarray(sl).save(png, format="PNG")
    return {"ok": True, "file": os.path.basename(f), "shape": list(a.shape),
            "range": [round(mn, 1), round(mx, 1)], "uniq": int(np.unique(a).size),
            "all_files": [os.path.basename(x) for x in allf][-10:],
            "png_base64": base64.b64encode(png.getvalue()).decode(),
            "nifti_base64": base64.b64encode(open(f, "rb").read()).decode()}
