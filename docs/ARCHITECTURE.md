# Mac Studio Inference Hub — Architecture

**Status:** v2 (2026-05-22). Video sections now backed by Gemini deep-research notes at `docs/research/2026-05-22-video-gen-and-hub-architecture.md`.
**Author:** drafted with Claude during the githubawesome podcast refactor session.

---

## 0. Problem

We want one LAN-only "household AI compute box" hosting multiple inference workloads under a single, consistent operational model. Workloads in scope:

| Workload | Runtime | Current state |
|---|---|---|
| LLM (text gen, script gen, chat) | Ollama | ✅ already running on `192.168.0.159:11434`, LAN-exposed |
| Embeddings | Ollama (`nomic-embed-text`) | ✅ already available via Ollama |
| TTS (podcast voices) | Kokoro via Python sidecar | ⏳ to deploy |
| ASR (transcription) | `mlx-whisper` via Python sidecar | ⏳ to deploy |
| Image generation | ComfyUI (SDXL / Flux / SD3.5) + optionally `mflux` MLX sidecar | ⏳ to deploy |
| Video generation | ComfyUI (LTX-Video, Wan2.1 1.3B + 14B Q4, HunyuanVideo Q4) | ⏳ to deploy |

Explicitly **out of scope for v1**:

- RAG / Deep Lake sidecar (user deferred — revisit when actually needed)
- VLM webapp surface (qwen2.5vl is already in Ollama and reachable; no UI work blocking)
- Multi-user / external internet access

## 1. Host

| | |
|---|---|
| Hardware | Mac Studio, Apple M1 Max, 10-core CPU (8P+2E), 32-core GPU |
| Memory | 64 GB unified, ~400 GB/s bandwidth |
| Disk | ~2.9 TB free of ~3.6 TB |
| OS | macOS 15.5 (Sequoia), Metal 3, arm64 |
| LAN IP | `192.168.0.159` |
| Hostname | `gyasis-Mac-Studio.local` (mDNS may not resolve from Linux peers — use IP) |
| SSH | port 22, key-based auth as `gyasisutton` |
| Existing services | Ollama 0.24.0 on `:11434` (bound to 0.0.0.0), Xcode CLT, Homebrew at `/opt/homebrew`, `python3.14` + `uv` available |

**Primary bottleneck on this hardware: unified-memory contention, not disk or compute.** A 14B video model (~10 GB Q4) running alongside Ollama's `qwen3:32b` (~20 GB resident) will compete for the same 64 GB. Exceed ~55 GB total resident and macOS silently swaps to NVMe, converting a 5-minute render into a 2-hour failure — no hard OOM error like CUDA throws.

## 2. Topology

```
                LAN
                 │
   ┌─────────────┼──────────────┐
   │             │              │
   ▼             ▼              ▼
[Linux box]  [Other clients]   ...
   │
   │  HTTPS / HTTP (LAN-only)
   ▼
[SvelteKit gateway @ webapp]
   │  /api/<workload>/...
   ▼
[Mac Studio: 192.168.0.159]
   ├── :11434  Ollama (LLM, embeddings, VLM)         ← existing
   ├── :8765   kokoro-sidecar (TTS)                   ← new
   ├── :8766   whisper-sidecar (ASR, mlx-whisper)     ← new
   ├── :8767   mflux-sidecar (fast Flux via MLX)      ← new, optional
   ├── :8188   ComfyUI headless (image + video)       ← new
   └── :9100   hub-supervisor (health + model index)  ← new, v2
```

Rationale for "many sidecars" over one umbrella service:
- Workloads have wildly different runtimes (Go binary for Ollama, Python+ONNX for Kokoro, Python+PyTorch+ComfyUI, MLX-Python). One process means one Python env to fight; many sidecars means each runtime stays clean.
- Failure isolation: a hung video job doesn't take down TTS.
- Independent deploy: redeploy any sidecar without disturbing others.

## 3. Sidecar conventions

Every sidecar follows this shape so the gateway treats them uniformly:

### 3.1 HTTP surface

| Endpoint | Purpose |
|---|---|
| `GET /healthz` | Liveness. Returns `{ok:true, service:"kokoro", version:"…"}` in <50ms. Never calls models. |
| `GET /readyz` | Readiness. Returns 200 only if model is loaded AND GPU queue is ready. Returns 503 during cold-load or memory pressure. |
| `GET /metrics` (optional) | Prometheus-style counters. Skip in v1 unless cheap. |
| `POST /<verb>` | The actual work. See per-workload sections. |

Sync vs async pattern (per workload, not per-sidecar):

- **Sync (response time <30s expected):** TTS turn synth, embeddings, single Whisper segment, single LLM call, fast image gen (Flux schnell via MLX). Just return the result inline. For LLM/TTS streams, use **SSE** (Server-Sent Events) — works through standard HTTP, no protocol upgrade.
- **Async (response time minutes+):** video generation, large transcription jobs, batch image gen. Use **REST submit + WebSocket progress** pattern (matching what ComfyUI itself expects):
  - `POST /jobs` → returns `{job_id}` immediately
  - Client connects to `ws://…/ws?clientId=<job_id>` for `execution_start` / `executing` / `progress` / `executed` events
  - `GET /jobs/<id>/result` — the artifact (URL or inline) when done
  - `DELETE /jobs/<id>` — cancel

This split (SSE for streams, WebSocket for jobs) is deliberate. SSE is simpler and routes through normal HTTP middleware. WebSocket is required for ComfyUI's native progress channel, so video gen has to use it anyway — re-use that pattern for any other long-running async work.

### 3.2 Port allocation

Reserved range: `8760-8799`. Currently:

| Port | Service |
|---|---|
| 8765 | kokoro-sidecar (TTS) |
| 8766 | whisper-sidecar (ASR via mlx-whisper) |
| 8767 | mflux-sidecar (Flux via MLX, optional) |
| 8768 | _reserved for future RAG sidecar_ |
| 8188 | ComfyUI (its own default port; not in 87xx range to match upstream convention) |
| 11434 | Ollama (its own default; not changing) |
| 9100 | hub-supervisor (v2) |

ComfyUI keeps its own port because every ComfyUI tutorial/docs assume 8188.

### 3.3 Auth

LAN-only deployment. Pragmatic choice:

- **v1: shared bearer token via `HUB_TOKEN` env var** loaded by every sidecar + the gateway. Header `Authorization: Bearer <token>`. Token lives in `~/.config/mac-studio-hub/token` on both ends, mode 0600.
- **IP allowlist** at the sidecar binding layer — accept only `192.168.0.0/24` connections. Simple `if request.client.host not in allowed_subnet` check in FastAPI; reject otherwise.
- **Not v1:** mTLS, OAuth, per-user tokens. Overkill for one-user LAN.

### 3.4 Logging + observability

- stdout/stderr only. launchd captures both to `/Users/gyasisutton/Library/Logs/<service>.{out,err}.log`.
- Logrotate via `newsyslog` config in `/etc/newsyslog.d/<service>.conf` — 50MB cap, 7 day retention.
- No metrics scraper in v1. Add Prometheus + Grafana later if usage warrants.

### 3.5 Deploy: launchd plists

`launchd` is the right tool here, not brew services / supervisord / tmux:

- ✅ Native, no extra install.
- ✅ Survives reboot trivially.
- ✅ Per-service env vars in plist (critical for `PYTORCH_ENABLE_MPS_FALLBACK`, `PYTORCH_MPS_HIGH_WATERMARK_RATIO`, `HF_HOME`).
- ✅ Auto-restart on crash via `KeepAlive` — essential when PyTorch OOMs and exits.
- ❌ Plist XML is ugly; `deploy/install.sh` templatizes the per-service bits.

Per service, a plist at `~/Library/LaunchAgents/io.macstudio.hub.<service>.plist`. Standard pattern:

```xml
<plist><dict>
  <key>Label</key><string>io.macstudio.hub.kokoro</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/caffeinate</string><string>-i</string>
    <string>/opt/homebrew/bin/uv</string>
    <string>run</string>
    <string>--directory</string><string>/Users/gyasisutton/services/kokoro-sidecar</string>
    <string>uvicorn</string><string>server:app</string>
    <string>--host</string><string>0.0.0.0</string>
    <string>--port</string><string>8765</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>StandardOutPath</key><string>/Users/gyasisutton/Library/Logs/kokoro-sidecar.out.log</string>
  <key>StandardErrorPath</key><string>/Users/gyasisutton/Library/Logs/kokoro-sidecar.err.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTORCH_ENABLE_MPS_FALLBACK</key><string>1</string>
    <key>PYTORCH_MPS_HIGH_WATERMARK_RATIO</key><string>0.0</string>
    <key>HF_HOME</key><string>/Users/gyasisutton/models/hf-cache</string>
    <key>HUB_TOKEN</key><string>FILE:/Users/gyasisutton/.config/mac-studio-hub/token</string>
  </dict>
</dict></plist>
```

**Two things that look optional but are not:**

1. **`/usr/bin/caffeinate -i`** wraps the command. macOS aggressively App Naps background processes that don't own a UI window — a headless ComfyUI render can stretch indefinitely if it gets paused. `caffeinate -i` prevents idle sleep / nap for the wrapped subtree.
2. **`PYTORCH_ENABLE_MPS_FALLBACK=1`** must be in the plist EnvironmentVariables, not just `.zshrc`. launchd doesn't load shell profiles. Without it, ComfyUI crashes the first time it hits an op not yet implemented in MPS (common with `float8_e4m3fn` casts).

`launchctl bootstrap gui/$(id -u) <plist>` to load. `launchctl bootout gui/$(id -u)/<label>` to unload. Survives reboot.

### 3.6 Storage layout on the Mac

```
/Users/gyasisutton/
├── services/                          # all sidecar code
│   ├── kokoro-sidecar/                # uv project: pyproject.toml, server.py, .python-version
│   ├── whisper-sidecar/               # mlx-whisper + FastAPI
│   ├── mflux-sidecar/                 # mflux + FastAPI (optional)
│   └── ComfyUI/                       # cloned upstream
├── models/                            # SHARED model store across ComfyUI + MLX
│   ├── checkpoints/                   # SDXL, SD3.5
│   ├── unet/                          # Flux UNet GGUF files
│   ├── clip/                          # text encoders
│   ├── vae/
│   ├── loras/
│   ├── video/                         # Wan2.1, LTX-Video, HunyuanVideo
│   ├── kokoro/                        # kokoro-onnx weights
│   ├── whisper/                       # mlx-whisper / whisper.cpp variants
│   └── hf-cache/                      # HF_HOME — used by mflux, mlx-whisper, transformers
├── outputs/                           # artifacts ComfyUI / video gen writes
└── Library/LaunchAgents/io.macstudio.hub.*.plist
```

**Model sharing strategy (single source of truth):**

1. **Set `HF_HOME=/Users/gyasisutton/models/hf-cache`** globally — in every launchd plist's EnvironmentVariables, plus in `~/.zprofile` for interactive shells. Forces all Python tooling (transformers, diffusers, mflux, mlx-whisper) to use one cache.
2. **ComfyUI's `extra_model_paths.yaml`** maps its per-folder dirs (`checkpoints`, `unet`, `clip`, `vae`, `loras`) directly to absolute paths under `/Users/gyasisutton/models/`. No symlinks needed for ComfyUI itself.
3. **APFS symlinks** for any model that resists both the yaml routing and HF_HOME. APFS handles symlinks flawlessly, zero disk overhead.

Result: every weight lives in exactly one place. Adding the MLX sidecar later costs zero extra disk because it reads the same `hf-cache` and `models/` tree.

## 4. Gateway (Linux box, SvelteKit)

The webapp at `~/Documents/code/githubawesome/webapp/` is the first gateway client. Pattern:

```
client (browser)
   │
   ▼
SvelteKit server: /api/<workload>/<verb>?…
   │  rewrites + adds Bearer token + caches per-input
   ▼
Mac Studio sidecar
```

Why proxy through SvelteKit instead of letting the browser hit the Mac directly:

- ✅ Browser only talks to its origin (no CORS pain).
- ✅ The bearer token never leaves the server (keeps `HUB_TOKEN` server-side).
- ✅ The gateway caches outputs (e.g. `data/podcast-audio/<project_id>.wav`) so the second listener gets it instantly.
- ✅ Centralized retry / fallback logic per workload.
- ✅ Single chokepoint to swap in a different host later (multi-Mac, cloud burst, etc.).

Caching conventions on the gateway side:

| Workload | Cache key | Cache location | TTL |
|---|---|---|---|
| TTS (per project) | `project_id` | `data/podcast-audio/<id>.wav` | until invalidated by user |
| Image gen (one-shot) | sha256(prompt + params) | `data/images/<hash>.png` | 30 days |
| Video gen | sha256(prompt + params + model) | `data/videos/<hash>.mp4` | 30 days |
| Whisper transcript | sha256(audio bytes) | `data/transcripts/<hash>.json` | indefinite |
| LLM dialog scripts | `project_id` | already done at `data/podcast-cache/<id>.json` | indefinite |

## 5. Per-workload notes

### 5.1 TTS — Kokoro sidecar (port 8765)

- **Runtime:** Python 3.12 via `uv venv`, `fastapi` + `uvicorn` + `kokoro-onnx` + `onnxruntime` with CoreML provider.
- **Why kokoro-onnx not kokoro-tts:** lightweight (no PyTorch), good macOS arm64 support, uses CoreML execution provider for ANE/GPU acceleration.
- **Endpoint:** `POST /tts` with `{text, voice, speed}` → `audio/wav`. Sync (<2s per turn at full speed; sub-second on cache hits).
- **Model:** `Kokoro-82M-v1.0-ONNX` q8 quantized (~325MB), stored at `models/kokoro/`.
- **First call:** warm model in `lifespan` startup so `/readyz` flips true once warm.
- **Concurrency:** single-process, one inflight request via an `asyncio.Semaphore(1)` — onnxruntime CoreML EP is single-stream.

### 5.2 ASR — Whisper sidecar (port 8766)

- **Runtime:** Python 3.12 via `uv venv`, FastAPI + `mlx-whisper`.
- **Why mlx-whisper:** RTF ~40-50× on M1 Max for `whisper-large-v3-turbo` (12 min audio in ~14-18 sec), 30-50% faster than `whisper.cpp`, no C++ build chain. Drop-in `pip install mlx-whisper`.
- **Alternative to revisit:** `FluidAudio` (CoreML/Parakeet on ANE) is reportedly ~5× faster than `mlx-whisper` (~0.19s vs ~1.02s per inference). Not v1 because it's harder to wire into a Python sidecar and the ecosystem is younger. Track for v2 if Whisper latency matters more.
- **Endpoint:** `POST /transcribe` (sync, <30s for short clips): `multipart/form-data` audio file → `{text, segments, language}`.
- **Async path:** `POST /transcribe/batch` returns `{job_id}` for hour-long inputs; progress via `/jobs/<id>/stream` (SSE).
- **Model:** `whisper-large-v3-turbo` (~1.6 GB) cached under `~/models/whisper/`.

### 5.3 LLM script gen — Ollama proxy (port 11434)

Already runs. Gateway will call it directly: `fetch('http://192.168.0.159:11434/api/chat', …)`. No new sidecar.

For the podcast LEO/SARAH banter, the recommended local models (from the on-disk inventory) are **`gemma4:31b`** or **`qwen3:32b`**. Keep Gemini API as a fallback path via env-var toggle in `lib/server/podcast.ts`.

**Important coexistence rule:** before dispatching a heavy ComfyUI video job, call `ollama stop <model>` to release ~20 GB of unified memory — otherwise the video render will swap to disk. The gateway can automate this for the duration of a video job and reload after.

### 5.4 ComfyUI — image + video (port 8188)

- **Install:** `uv venv comfy-env --python 3.12` (NOT 3.14 — experimental ABI changes break ComfyUI C-extensions on arm64). `pip install torch torchvision torchaudio` (PyTorch 2.5.1 or latest nightly; MPS is standard). Then clone ComfyUI upstream into `~/services/ComfyUI/`.
- **Required env vars (set in launchd plist EnvironmentVariables, never just shell):**
  - `PYTORCH_ENABLE_MPS_FALLBACK=1` — silent CPU fallback when an op isn't in MPS (e.g. `float8_e4m3fn` casts). Without this, hard crashes on first miss.
  - `PYTORCH_MPS_HIGH_WATERMARK_RATIO=0.0` — disables aggressive memory pre-allocation that triggers OOM on unified memory.
- **Launch:** `python main.py --listen 0.0.0.0 --port 8188 --output-directory /Users/gyasisutton/outputs` wrapped in `caffeinate -i` via the plist's `ProgramArguments`.
- **API mode:** ComfyUI is just an HTTP server. No browser needed if your client knows the workflow:
  - `POST /prompt` — submit a workflow in **API JSON format** (different from the UI's JSON; export from the UI via "Save (API Format)" or generate programmatically).
  - `GET /history/<prompt_id>` — completed job data.
  - `GET /view?filename=…&type=output` — fetch artifact.
  - `WS /ws?clientId=<UUID>` — REQUIRED for usable progress tracking. The gateway maintains one persistent WS connection per active job; messages stream `execution_start`, `executing`, `progress`, `executed` events.
- **Custom node packs:**
  - ✅ `ComfyUI-Manager` — dependency tracking + clean updates.
  - ✅ `ComfyUI-GGUF` (city96) — required for 4-bit DiT loading (Wan2.1 14B, Flux Q4, HunyuanVideo Q4).
  - ✅ `ComfyUI-LTXVideo` — well-maintained but occasionally needs an autocast fallback flag.
  - ❌ Anything depending on **Triton** or **SageAttention** — does not compile natively on macOS arm64.
  - ❌ Anything depending on **xformers** — CUDA-only, broken on MPS. Apple Silicon uses native `sdpa` instead.
  - ❌ "Memory offloading" nodes designed for low-VRAM Windows GPUs — they fight Apple's unified memory architecture and slow things down dramatically.

#### 5.4.1 Image gen — concrete picks

| Model | Path | 1024×1024 time | Notes |
|---|---|---|---|
| **SDXL 1.0** | ComfyUI, FP16 | 12-15s (25 steps) | The fastest reliable workhorse. |
| **Flux.1-schnell** | **MLX (`mflux`)** at 8767, OR ComfyUI Q8 | **~15s** (MLX, 4 steps) / 110-120s (ComfyUI PyTorch) | MLX is 7× faster — worth the parallel sidecar. |
| **Flux.1-dev** | ComfyUI, GGUF Q4_K_M | 240-300s (20 steps) | Q4 retains ~92% of FP16 fidelity. Q8 (~12 GB) flirts with swap threshold under Ollama load — avoid. |
| **SD 3.5 Large** | ComfyUI, FP8 / GGUF | 45-60s (30 steps) | Competent but Flux is better for typography + prompt adherence. |

#### 5.4.2 Video gen — concrete picks for M1 Max 64GB

A 4-second 480p clip is the realistic unit of work. **Treat all video jobs as async + cacheable** — even the fastest is multi-minute.

| Model | RAM | 4s 480p time | When to use |
|---|---|---|---|
| **LTX-Video (2B)** | 4-6 GB FP16/BF16 | ~5 min | **Daily driver.** Best speed × quality on this hardware. Excellent temporal consistency. |
| **Wan2.1 1.3B** | 6-8 GB FP16 | ~4-6 min | Rapid storyboarding / prototyping. Motion less cinematic than 14B. |
| **Wan2.1 14B Q4_K_M GGUF** | ~10 GB Q4 | ~11-15 min | Final cinematic renders. **Hard ceiling: ~2s at 480p without managing resolution carefully** (scales to 4-8s only with strict resolution discipline). Async batch only. |
| **HunyuanVideo Q4** | 9-12 GB | 15-20 min | Cinematic alternative. Q4 works; VAE decode is the slow step on MPS. |
| **CogVideoX-5B FP8** | ~8 GB | ~8 min | Viable but superseded by Wan / LTX. |
| **Mochi-1 Q4 (10B)** | ~12 GB | 15+ min | Falling behind Wan2.2 + LTX in 2026. Skip. |
| **AnimateDiff / SVD** | ~4 GB | ~1 min | U-Net architectures — deprecated for new work. Avoid. |

**The "golden combo" for this rig:** prototype with **Wan2.1 1.3B** (4-6 min iteration cycle, FP16 fits comfortably) → commit to **LTX-Video** for medium-fidelity 4-8s clips → graduate to **Wan2.1 14B Q4** when you want the cinematic look and don't mind a 15-min batch.

### 5.5 mflux — fast Flux via MLX (port 8767, optional)

Decision: **maintain a dual-stack** — ComfyUI for orchestration (ControlNets, LoRA chains, video, image-to-video) AND a tiny `mflux`-based MLX sidecar for "give me Flux schnell at full speed" requests.

- **Runtime:** Python 3.12 via `uv venv`, `mflux` (`pip install mflux`), FastAPI.
- **Endpoint:** `POST /image` with `{prompt, model, steps, seed}` → `image/png`. Sync (~15s for Flux schnell, 4 steps).
- **Why both stacks:** MLX wins on raw speed (25-40% over PyTorch-MPS, sometimes 7× on Flux schnell), but it lacks ComfyUI's ecosystem (no IPAdapter, no ControlNet, no LoRA chaining). ComfyUI is the "I want to do something custom" engine; mflux is the "I want a quick image, fast" endpoint.
- **Same model dir:** reads from `~/models/hf-cache/` — no weight duplication.

### 5.6 Embeddings + VLM — already in Ollama

No new sidecar. Direct calls to:
- `POST http://192.168.0.159:11434/api/embeddings` with `model:"nomic-embed-text"`
- `POST http://192.168.0.159:11434/api/chat` with `model:"qwen2.5vl:32b"` + image payload

Gateway wraps these into `/api/embed` and `/api/vlm/chat` for consistency.

## 6. Disk budget

Realistic total of new model files on top of existing Ollama (~400 GB):

| Category | Models | Size |
|---|---|---|
| Video | Wan2.1 14B Q4 (~10) + Wan2.1 1.3B (~6) + LTX-Video (~5) + HunyuanVideo Q4 (~9) | **~30 GB** |
| Image | SDXL (~7) + Flux.1-dev Q4 (~7) + Flux.1-schnell Q8 (~12) + SD 3.5 (~6) | **~32 GB** |
| Audio / TTS | Whisper large-v3-turbo (~1.6) + Kokoro ONNX (<1) + VAEs (~2) | **~5 GB** |
| LoRAs (50× ~50MB) | various | **~2-5 GB** |
| **New total** | | **~70-75 GB** |
| Existing Ollama | | ~400 GB |
| **Grand total** | | **~475 GB** |

Well within the 2.9 TB free. Disk is not the constraint.

## 7. Implementation order

Locked priority: **video first** (it's the hardest and constrains the rest).

1. **ComfyUI deploy on Mac Studio**
   - `uv venv` with Python 3.12, install PyTorch 2.5.1+, clone ComfyUI, install `ComfyUI-GGUF` + `ComfyUI-Manager` + `ComfyUI-LTXVideo`.
   - Write the launchd plist with `caffeinate -i` wrapper + required env vars.
   - Create `~/models/` tree + `extra_model_paths.yaml`.
   - Download Wan2.1 1.3B (start small, validate the stack works).
2. **One end-to-end video gen** test from CLI on the Mac (curl against `:8188/prompt` with API-format JSON), then via curl from the Linux box, then verify WebSocket progress events arrive.
3. **SvelteKit gateway**: `/api/video/generate` with REST submit + WebSocket relay + cache.
4. **Webapp UI** (minimal): a video gen panel — prompt input + model picker + job-status pane.
5. **Add Wan2.1 14B Q4 + LTX-Video** to ComfyUI once the v1 path is proven with the 1.3B model. Validate Ollama-eviction pattern (auto-stop active LLM before dispatching video job).
6. **Kokoro TTS sidecar** — apply the patterns shaken out from steps 1-3.
7. **Whisper sidecar** (`mlx-whisper`) — same.
8. **mflux sidecar** (optional, for fast Flux schnell).
9. **Token + launchd hardening** — once 3+ sidecars exist and the pattern is real, package `deploy/install.sh` that drops the plists + token files + symlink tree.
10. **v2:** `hub-supervisor` on :9100 — aggregates `/healthz` + `/readyz` across all sidecars + exposes "currently loaded models" + memory pressure signal. Powers an LM-Studio-like picker UI.

## 8. Sharp edges (hard-won lessons)

From the deep-research notes, in priority order:

1. **Unified-memory swap death.** macOS will silently page to NVMe instead of throwing OOM. Inference latency goes from 2s/iter to 400s/iter. *Mitigation:* `ollama stop <model>` before video jobs; track total resident memory (RSS sum); fail loud at gateway level if budget exceeded.
2. **App Nap throttling.** Headless launchd services get aggressively downclocked because they own no UI window. A 5-min render can stretch indefinitely. *Mitigation:* always wrap launchd commands in `caffeinate -i` (already in §3.5 plist template).
3. **`PYTORCH_ENABLE_MPS_FALLBACK=1` is mandatory, not optional.** Modern DiTs hit ops not yet in MPS (specific `bfloat16` / `float8` casts) — without fallback they hard-abort. *Mitigation:* set in plist EnvironmentVariables (not just shell — launchd doesn't read shell profiles).
4. **TCC permissions on Sequoia.** If `HF_HOME` ever lives on an external drive, launchd-run python processes get blocked with cryptic `FileNotFound` errors. *Mitigation:* keep models on the internal SSD; if external is needed, manually grant Full Disk Access to the specific `python3` binary inside the `uv venv`.
5. **C-extension compiler mismatches after macOS upgrades.** Major macOS updates can break wheels (`tokenizers`, audio libs, ONNX bindings) that were compiled against the old Xcode CLT. *Mitigation:* after every macOS major upgrade, run `xcode-select --install` and rebuild the `uv venv` from scratch. Don't try to repair in-place.

## 9. Open questions (deferred to implementation)

- **Per-workload memory accounting precision** — symbolic budget table here is rough; need empirical baselines once ComfyUI is up. The hub-supervisor (v2) should expose live `rss` per sidecar and surface contention.
- **HUB_TOKEN rotation** — manual for v1. Trivial script in `deploy/` if needed later.
- **Multi-host** — keep the gateway agnostic; scale by adding more `:port` endpoints on more Macs.
- **FluidAudio for Whisper** — defer to v2 if mlx-whisper latency is the bottleneck for any workflow.

## 10. Deferred / future

- RAG sidecar (Deep Lake or alternative) — when there's an actual document corpus to index.
- LM-Studio-style model picker UI — webapp work, blocked on `hub-supervisor` :9100 existing.
- Multi-Mac extension (Studio Ultra as a second box?) — gateway already supports it.
- ComfyUI workflow library — versioned `.json` workflow files in `mac-studio-hub/workflows/` that the gateway can name and re-use.

## 11. Repo layout

```
mac-studio-hub/
├── docs/
│   ├── ARCHITECTURE.md            # this doc
│   └── research/
│       └── 2026-05-22-video-gen-and-hub-architecture.md
├── sidecars/
│   ├── kokoro/                    # pyproject.toml + server.py
│   ├── whisper/
│   ├── mflux/
│   └── comfyui/                   # deploy notes + node-pack list + extra_model_paths.yaml template (NOT upstream code)
├── deploy/
│   ├── launchd/                   # plist templates with ${SERVICE}, ${PORT}, ${SCRIPT} placeholders
│   ├── install.sh                 # bootstrap on a fresh Mac
│   └── token-gen.sh
├── workflows/                     # versioned ComfyUI API-format .json workflows (later)
└── README.md
```
