# Atelier — the household AI atelier

> One Mac Studio. Every medium. A single LAN endpoint per workload.

**Atelier** (French: *workshop*) is a LAN-only "household AI compute box" running on a
single Apple Silicon Mac Studio. It hosts production-grade ML inference for **text,
audio, image, and video** as a fleet of small, uniform Python sidecars — so any client
on the network (a SvelteKit webapp, a notebook, a CLI) hits *one HTTP endpoint per
workload* instead of each app shipping its own copy of a model.

It is deliberately two things at once:

- a **production engine** — the podcast/Radio-Mode pipeline in the githubawesome webapp
  depends on it every day; and
- an **experimentation platform** — "a bigger ComfyUI for all media models," where new
  TTS / image / video / LLM models can be slotted in behind the same operational
  contract and A/B-compared.

---

## 1. The thesis

Running models in-browser (WASM) or re-downloading weights per app wastes the one
scarce resource on this hardware: **unified memory**. Atelier centralizes every model
on the box that has the memory and the Metal GPU, and exposes each as a stable HTTP
service. Clients stay thin; models stay warm (or unload cleanly when idle); the network
is the API.

## 2. The host

| | |
|---|---|
| Hardware | Mac Studio — Apple M1 Max, 10-core CPU (8P+2E), 32-core GPU |
| Memory | **64 GB unified**, ~400 GB/s bandwidth |
| Disk | ~2.9 TB free of 3.6 TB |
| OS | macOS 15.5 (Sequoia), Metal 3, arm64 |
| LAN | `192.168.0.159` · SSH key-based as `gyasisutton` |
| Co-resident | Ollama `:11434` (LAN-exposed), Homebrew, `uv` |

**The defining constraint is unified-memory contention — not disk, not compute.**
Apple Silicon has no hard OOM like CUDA. Exceed ~55 GB resident and macOS silently
pages to NVMe, turning a 5-minute render into a 2-hour hang with no error. Every design
decision below exists to respect that ceiling.

## 3. What runs here — by medium

| Medium | Service | Port | Engine | Role | Notes |
|---|---|---|---|---|---|
| **Text / LLM** | Ollama | 11434 | gemma / qwen3:32b / … | script-gen, chat | co-resident, managed outside Atelier |
| **Embeddings** | Ollama | 11434 | `nomic-embed-text` | retrieval | via Ollama |
| **Audio · TTS** | **OmniVoice** | **8770** | k2-fsa/OmniVoice (Diffusion LM, PyTorch MPS) | **PRIMARY** — instruct-driven accent / pitch / gender | ~0.6–1× RTF; the natural-voice engine |
| **Audio · TTS** | Kokoro | 8765 | kokoro-onnx (CoreML/MPS) | fallback — fast, fixed voices | ~0.6 s/line; Mac-offline resilience twin on Linux `:18765` |
| **Audio · TTS** | Dia 1.6B | 8769 | nari-labs/Dia (PyTorch MPS) | **retired for live** — expressive cloning | ~10× RTF; overnight batch only |
| **Audio · ASR** | Whisper | 8766 | mlx-whisper | transcription | planned |
| **Image** | ComfyUI | 8188 | SDXL / Flux / SD3.5 | image gen | + optional `mflux` (MLX) on 8767 |
| **Video** | ComfyUI | 8188 | Wan2.1 1.3B + 14B Q4 | text-to-video | verified 2026-05-23; LTX-Video / Hunyuan pending |

## 4. How it works — the sidecar contract

Every Atelier workload is a small **FastAPI** app following one contract:

- **HTTP surface:** `/healthz` (liveness), `/readyz` (model-loaded?), `/tts` (or the
  workload verb), `/admin/unload` (manual eviction). Single-flight via
  `asyncio.Semaphore(1)` — one inference at a time per sidecar.
- **Runtime:** a `uv` venv (Python 3.12), PyTorch on **MPS** (Metal) or ONNX/CoreML or
  MLX, depending on the engine.
- **Idle-unload economy:** the server stays up; the *model* unloads after
  `IDLE_UNLOAD_SECONDS` of inactivity and `gc` + `empty_cache()` free the memory. This
  is the memory-ceiling discipline made automatic — "don't fill 64 GB with idle trash."
- **Managed by launchd:** each sidecar is an `io.macstudio.hub.<name>` service —
  `RunAtLoad` (survives reboot), `KeepAlive` (restart on crash), wrapped in
  `caffeinate -i` (defeats App Nap throttling of headless processes), with
  `PYTORCH_ENABLE_MPS_FALLBACK=1` and `PYTORCH_MPS_HIGH_WATERMARK_RATIO=0.0` in the
  plist environment.

### The gateway

A **SvelteKit** webapp on the Linux box (githubawesome) is the primary client. Its
Radio-Mode podcast pipeline calls the TTS chain **OmniVoice → Kokoro-mac →
Kokoro-linux**, failing over per-engine so a single sidecar being down degrades quality
rather than breaking playback.

## 5. Operating the hub

| Tool | Where | Purpose |
|---|---|---|
| `deploy/install.sh` | repo | fresh-Mac bootstrap — venvs, deps, symlink sidecars, copy plists, fetch models, `launchctl bootstrap` |
| `deploy/doctor.sh` | repo | **audit** — every sidecar has a plist, every plist is installed + loaded + healthy. Exit 1 on any gap |
| `atelier-status` | Linux CLI | live health of all sidecars (state, idle, device) + `--unload <name>` manual eviction |

> **Why `doctor.sh` exists:** on 2026-05-25 the Mac rebooted and OmniVoice — the one
> sidecar still running via `nohup` instead of a launchd service — silently died,
> degrading the podcast to the Kokoro fallback. `doctor.sh` makes that class of gap
> *loud*: a model with code but no service now fails the audit.

## 6. Repo layout

```
atelier/
├── README.md
├── docs/
│   ├── ATELIER.md            — this overview
│   ├── ARCHITECTURE.md       — full v2 design + research notes
│   └── SIDECAR_PATTERN.md    — how to add a new sidecar
├── deploy/
│   ├── install.sh            — fresh-Mac bootstrap
│   └── doctor.sh             — service completeness + health audit
├── launchd/
│   └── io.macstudio.hub.{kokoro,dia,omnivoice,comfyui}.plist
└── sidecars/
    ├── kokoro/{server.py,requirements.txt}
    ├── dia/{server.py,requirements.txt}
    └── omnivoice/{server.py,requirements.txt}
```

`~/services/<name>/server.py` are symlinks **into** this repo, so edits land directly in
the running service path.

## 7. The ambitions

1. **Every medium under one contract.** Text, audio (TTS + ASR), image, video,
   embeddings — each a uniform sidecar, each hot-swappable.
2. **Production + experimentation in one box.** Ship a single production engine per
   medium; keep the bench open to A/B a new model the moment it lands.
3. **Memory-economy as a first principle.** Idle models unload; nothing hoards the
   64 GB it isn't using. The hub should run TTS + a video render + an LLM without
   touching swap.
4. **Reproducible from zero.** `install.sh` on a fresh Mac reconstructs the whole hub;
   `doctor.sh` proves it's whole. No model lives outside the repo's knowledge again.

### Roadmap

- **ASR:** land the mlx-whisper sidecar (`:8766`).
- **Video:** LTX-Video + HunyuanVideo behind ComfyUI; tune the kornia import.
- **Image:** optional `mflux` MLX fast-Flux sidecar (`:8767`).
- **Reliability:** bound podcast prep concurrency so single-flight sidecars aren't
  overwhelmed; retry a failed chapter instead of dropping it.
- **Deferred:** RAG / DeepLake sidecar, a VLM webapp surface (qwen-VL already in Ollama).
