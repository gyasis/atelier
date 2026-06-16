"""atelier-maisi — Modal GPU app for 3D CT synthesis (MONAI MAISI).
Deployed app; the Mac 'maisi' sidecar calls generate.remote() (passthrough).
v1 proves the live A100 round-trip + reports backend readiness honestly;
full MAISI bundle weights are wired in the next iteration (no fake CT data)."""
import modal

app = modal.App("atelier-maisi")
image = (modal.Image.debian_slim(python_version="3.11")
         .pip_install("torch", "numpy", "nibabel", "pillow", "monai"))

@app.function(gpu="A100", image=image, timeout=900)
def generate(params: dict) -> dict:
    import torch, subprocess
    gpu = subprocess.run(["nvidia-smi","--query-gpu=name,memory.total","--format=csv,noheader"],
                         capture_output=True, text=True).stdout.strip()
    monai_ok = True
    try:
        import monai  # noqa
        monai_v = monai.__version__
    except Exception as e:
        monai_ok = False; monai_v = str(e)[:80]
    # v1: live GPU + env proven; MAISI bundle inference is the next iteration.
    return {
        "ok": True, "backend": "modal", "gpu": gpu,
        "cuda": torch.cuda.is_available(), "monai": monai_v,
        "status": "passthrough live on A100; MAISI bundle weights pending (next iteration)",
        "prompt": params.get("prompt"),
    }

@app.local_entrypoint()
def main():
    print(generate.remote({"prompt": "abdomen CT, normal"}))
