# radiogen-sidecar (:8774) — synthetic chest X-ray

Local PyTorch/MPS sidecar. diffusers Stable Diffusion (RoentGen-v2, gated) ->
synthetic CXR -> valid DICOM (Modality DX) bound to a FHIR ImagingStudy's UIDs.

- `RADIOGEN_MODEL` (default public placeholder; set `stanfordmimi/RoentGen-v2`)
- Endpoints: /healthz /readyz /generate /admin/unload  (SIDECAR_PATTERN.md)
- venv: ~/services/cxr-gen ; launchd: io.macstudio.hub.radiogen
