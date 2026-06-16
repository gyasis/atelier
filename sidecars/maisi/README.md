# maisi-sidecar (:8775) — 3D CT (MONAI MAISI), CLOUD-PASSTHROUGH

First cloud-passthrough sidecar: a thin local proxy that forwards /generate to the
Modal A100 app `atelier-maisi` (modal_app.py). $0 idle (Modal scales to zero).
/readyz reports backend=modal, device="A100 (modal cloud)", passthrough=true.

- Modal app: `modal deploy modal_app.py` (app name atelier-maisi)
- venv: ~/services/cxr-gen (has `modal`, token in ~/.modal.toml)
- launchd: io.macstudio.hub.maisi
- NOTE: v1 proves the live A100 round-trip; MONAI MAISI bundle weights = next iteration.
