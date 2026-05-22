# Mac Studio Inference Hub — Architecture

**Status:** Draft (2026-05-22). Video-gen sections await Gemini deep-research findings.
**Author:** drafted with Claude during the githubawesome podcast refactor session.

---

## 0. Problem

We want one LAN-only "household AI compute box" hosting multiple inference workloads under a single, consistent operational model. Workloads in scope:

| Workload | Runtime | Current state |
|---|---|---|
| LLM (text gen, script gen, chat) | Ollama | ✅ already running on `192.168.0.159:11434`, LAN-exposed |
| Embeddings | Ollama (`nomic-embed-text`) | ✅ already available via Ollama |
| TTS (podcast voices) | Kokoro via Python sidecar | ⏳ to deploy |
| ASR (transcription) | Whisper via Python sidecar | ⏳ to deploy |
| Image generation | ComfyUI (SD / Flux / SD3.5) | ⏳ to deploy |
| Video generation | ComfyUI (Wan2.1 / Mochi / LTX / …) | ⏳ to deploy, **needs research** |

Explicitly **out of scope for v1**:

- RAG / Deep Lake sidecar (user deferred — revisit when actually needed)
- VLM webapp surface (qwen2.5vl is already in Ollama and reachable; no UI work blocking)
- Multi-user / external internet access

## 1. Host

| | |
|---|---|
| Hardware | Mac Studio, Apple M1 Max, 10-core CPU (8P+2E), 32-core GPU |
| Memory | 64 GB unified |
| Disk | ~2.9 TB free of ~3.6 TB |
| OS | macOS 15.5 (Sequoia), Metal 3, arm64 |
| LAN IP | `192.168.0.159` |
| Hostname | `gyasis-Mac-Studio.local` (mDNS may not resolve from Linux peers — use IP) |
| SSH | port 22, key-based auth as `gyasisutton` |
| Existing services | Ollama 0.24.0 on `:11434` (bound to 0.0.0.0), Xcode CLT installed, Homebrew at `/opt/homebrew`, `python3.14` + `uv` available |

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
   ├── :11434  Ollama (LLM, embeddings, VLM)        ← existing
   ├── :8765   kokoro-sidecar (TTS)                  ← new
   ├── :8766   whisper-sidecar (ASR)                 ← new
   ├── :8188   ComfyUI headless (image + video)      ← new
   └── :9100   hub-supervisor (health + model index) ← new, optional v1
```

Rationale for "many sidecars" over one umbrella service:
- Workloads have wildly different runtimes (Python+ONNX, Python+PyTorch+ComfyUI, Go binary for Ollama). One process means one Python env to fight; many sidecars means each runtime stays clean.
- Failure isolation: a hung video job doesn't take down TTS.
- Independent deploy: redeploy any sidecar without disturbing others.

## 3. Sidecar conventions

Every sidecar follows this shape so the gateway treats them uniformly:

### 3.1 HTTP surface

| Endpoint | Purpose |
|---|---|
| `GET /healthz` | Liveness. Returns `{ok:true, service:"kokoro", version:"…"}` in <50ms. Never calls models. |
| `GET /readyz` | Readiness. Returns `{ok:true, models_loaded:[…], gpu:"metal", warmed:true}` after first successful inference. Returns 503 if model not yet loaded. |
| `GET /metrics` (optional) | Prometheus-style counters: requests_total, latency, errors. Skip in v1 unless cheap. |
| `POST /<verb>` | The actual work. See per-workload sections. |

Sync vs async pattern (per workload, not per-sidecar):

- **Sync** (response time <30s expected): TTS turn synth, embeddings, single Whisper segment, single LLM call. Just return the result inline.
- **Async** (response time minutes+): video generation, large transcription jobs, batch image gen. Return a `job_id` immediately, expose:
  - `GET /jobs/<id>` — status + progress + ETA
  - `GET /jobs/<id>/result` — the artifact (URL or inline) when done
  - `DELETE /jobs/<id>` — cancel
  - Server-Sent Events stream at `GET /jobs/<id>/stream` (optional, nice-to-have)

### 3.2 Port allocation

Reserved range: `8760-8799`. Currently:

| Port | Service |
|---|---|
| 8765 | kokoro-sidecar (TTS) |
| 8766 | whisper-sidecar (ASR) |
| 8767 | _reserved for future RAG sidecar_ |
| 8768 | _reserved_ |
| 8188 | ComfyUI (its own default port; not in 87xx range to match upstream convention) |
| 11434 | Ollama (its own default; not changing) |
| 9100 | hub-supervisor (if/when we build one) |

Why a reserved range: makes firewall / discovery / launchd plist patterns predictable. ComfyUI keeps its own port because every ComfyUI tutorial online assumes 8188.

### 3.3 Auth

LAN-only deployment. Pragmatic choice:

- **v1: shared bearer token via `HUB_TOKEN` env var** loaded by every sidecar + the gateway. Header `Authorization: Bearer <token>`. Token lives in `~/.config/mac-studio-hub/token` on both ends, mode 0600.
- **Not v1:** mTLS, OAuth, per-user tokens. Overkill for one-user LAN.
- **Defense in depth:** sidecars bind to `0.0.0.0` (LAN-reachable) but a launchd-managed firewall rule limits inbound to LAN subnet `192.168.0.0/24`. Optional.

### 3.4 Logging + observability

- stdout/stderr only. launchd captures both to `/Users/gyasisutton/Library/Logs/<service>.{out,err}.log`.
- Logrotate via `newsyslog` config in `/etc/newsyslog.d/<service>.conf` — 50MB cap, 7 day retention.
- No metrics scraper in v1. Add Prometheus + Grafana later if the hub gets enough usage to justify.

### 3.5 Deploy

Per service, a `launchd` plist at `~/Library/LaunchAgents/io.macstudio.hub.<service>.plist`. Standard pattern:

```xml
<plist><dict>
  <key>Label</key><string>io.macstudio.hub.kokoro</string>
  <key>ProgramArguments</key>
  <array>
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
    <key>HUB_TOKEN</key><string>FILE:/Users/gyasisutton/.config/mac-studio-hub/token</string>
  </dict>
</dict></plist>
```

`launchctl load`, `launchctl bootout` to manage. Survives reboot. Per-service log files. No supervisord, no pm2.

Why launchd over alternatives:
- ✅ Native, no extra install.
- ✅ Survives reboot trivially.
- ✅ Per-service env vars in plist.
- ❌ Plist XML is ugly; we'll have a small `deploy/install.sh` that templatizes the per-service bits.

### 3.6 Storage layout on the Mac

```
/Users/gyasisutton/
├── services/                          # all sidecar code
│   ├── kokoro-sidecar/                # uv project: pyproject.toml, server.py, .python-version
│   ├── whisper-sidecar/
│   └── ComfyUI/                       # cloned upstream
├── models/                            # shared model store (symlinked into ComfyUI/models/, HF cache, etc.)
│   ├── checkpoints/                   # SDXL, Flux, SD3.5, Wan2.1, …
│   ├── loras/
│   ├── vae/
│   ├── kokoro/                        # kokoro-onnx weights
│   ├── whisper/                       # whisper.cpp / mlx variants
│   └── hf-cache/                      # HF_HOME mirror, symlinked from ~/.cache/huggingface
├── outputs/                           # artifacts ComfyUI / video gen writes
└── Library/LaunchAgents/io.macstudio.hub.*.plist
```

Key trick: every model lives ONCE under `~/models/`. ComfyUI's per-folder dirs (`models/checkpoints`, `models/loras`, etc.) are symlinks into `~/models/`. The HF cache is also symlinked. Saves tens of GB when MLX and PyTorch stacks both want the same weights.

`HF_HOME` env var set in each sidecar's launchd plist points at `/Users/gyasisutton/models/hf-cache` so transformers / diffusers don't redownload.

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
- ✅ The gateway can cache outputs (e.g. `data/podcast-audio/<project_id>.wav`) so the second listener gets it instantly.
- ✅ Centralized retry / fallback logic per workload.

Caching conventions on the gateway side:

| Workload | Cache key | Cache location | TTL |
|---|---|---|---|
| TTS (per project) | `project_id` | `data/podcast-audio/<id>.wav` | until invalidated by user |
| Image gen (one-shot) | sha256(prompt + params) | `data/images/<hash>.png` | 30 days |
| Video gen | sha256(prompt + params) | `data/videos/<hash>.mp4` | 30 days |
| Whisper transcript | sha256(audio bytes) | `data/transcripts/<hash>.json` | indefinite |
| LLM dialog scripts | `project_id` | already done at `data/podcast-cache/<id>.json` | indefinite |

## 5. Per-workload notes

### 5.1 TTS — Kokoro sidecar (port 8765)

- **Runtime:** Python 3.12 via uv, `fastapi` + `uvicorn` + `kokoro-onnx` + `onnxruntime` with CoreML provider.
- **Why kokoro-onnx not kokoro-tts:** kokoro-onnx is lightweight (no PyTorch), well-supported on macOS arm64, uses CoreML execution provider for Apple Silicon GPU/ANE acceleration.
- **Endpoint:** `POST /tts` with `{text, voice, speed}` → `audio/wav`. Sync (<2s per turn at full speed).
- **Model:** `Kokoro-82M-v1.0-ONNX` q8 quantized (~325MB), shared with browser fallback path if we keep it.
- **First call:** warm model in `lifespan` startup so `/readyz` flips true after warm.
- **Concurrency:** single-process, one inflight request via a `asyncio.Semaphore(1)` — Kokoro's ONNX session is not thread-safe and onnxruntime CoreML EP is single-stream anyway.

### 5.2 ASR — Whisper sidecar (port 8766)

**To be decided post-research:** whisper.cpp Metal vs whisper-mlx vs faster-whisper on Apple Silicon for `large-v3`. Section will be filled in after Gemini deep-research lands.

Tentative shape:
- `POST /transcribe` (sync, <30s for short clips): `multipart/form-data` audio file → `{text, segments, language}`.
- `POST /transcribe/batch` (async, returns job_id): larger files / batches.

### 5.3 LLM script gen — Ollama proxy

Already runs on `:11434`. Gateway will call it directly via `fetch('http://192.168.0.159:11434/api/chat', …)`. No new sidecar; just adapt the existing `lib/server/podcast.ts` to point at Ollama instead of the Gemini SDK when we want to swap.

Recommended model for LEO/SARAH banter (from on-disk inventory): **gemma4:31b** or **qwen3:32b**. Keep Gemini API as fallback path via env-var toggle.

### 5.4 ComfyUI — image + video (port 8188)

**Details await deep-research.** Key open questions the research will answer:

- Install path on macOS arm64: portable bundle vs git clone + uv venv?
- Headless mode: ComfyUI ships a REST API at `/prompt` + queue management at `/queue` — usable without the web UI being open?
- launchd-managed: is anything in ComfyUI's stack fragile when run from launchd vs Terminal session (sometimes Python frameworks misbehave without TTY)?
- Image gen models known-good on M1 Max 64GB: SDXL ✓ probably, Flux.1-dev ✓ probably (q8), SD3.5 ✓ probably.
- Video gen models: this is the open question — Wan2.1 1.3B vs 14B, Mochi, LTX, CogVideoX, AnimateDiff. Realistic resolution × duration × time-to-output on this exact hardware.

Async job lifecycle for video is mandatory — every generation is multi-minute. Gateway pattern:

```
POST /api/video/generate { prompt, model, params }   → 202 { job_id, eta_s }
GET  /api/video/jobs/<id>                            → { status, progress, frame_url? }
GET  /api/video/jobs/<id>/result                     → 200 video/mp4 OR 425 if not done
```

SvelteKit gateway translates these to ComfyUI's `/prompt` + `/history` + `/view` endpoints, plus caches the final mp4 under `data/videos/<hash>.mp4`.

### 5.5 Embeddings + VLM — already in Ollama

No new sidecar. Direct calls to:
- `POST http://192.168.0.159:11434/api/embeddings` with `model:"nomic-embed-text"`
- `POST http://192.168.0.159:11434/api/chat` with `model:"qwen2.5vl:32b"` + image payload

Gateway wraps these into `/api/embed` and `/api/vlm/chat` for consistency.

## 6. Implementation order

Locked priority: **video first** (it's the hardest and constrains the rest).

1. **Gemini deep-research lands** → fill in §5.4 with concrete model picks and ComfyUI install path.
2. **ComfyUI deploy** on Mac Studio (clone, uv venv, models dir + symlinks, launchd plist).
3. **One end-to-end video gen** test from CLI on the Mac, then via curl from the Linux box.
4. **SvelteKit gateway**: add `/api/video/generate` → ComfyUI proxy + async job tracking + cache.
5. **Webapp UI** (minimal): a video gen panel in the existing webapp (or new `/video` route).
6. **TTS sidecar** (kokoro-onnx) — apply the same patterns shaken out from steps 2-4.
7. **Whisper sidecar** — same.
8. **Token + launchd hardening** — once 2-3 sidecars exist and the pattern is real, package an `install.sh` that drops the plists + token files.
9. **Optional:** `hub-supervisor` on :9100 — single endpoint that aggregates `/healthz` + `/readyz` across all sidecars + lists currently loaded models. Useful for the LM-Studio-like picker UI.

## 7. Open questions

- **Per-workload memory accounting** — 64GB unified means video gen (~20-40GB) cohabits poorly with a 32B Ollama model (~20GB). Strategy: do we evict Ollama models before launching video jobs (Ollama supports `keep_alive: 0` to unload), or accept higher swap?
- **Model store deduplication** — symlinks work, but what about `safetensors` files that ComfyUI expects in two places at once with different filenames? Verify per-ext mapping post-research.
- **Failover** — if Mac Studio is asleep / down, does the gateway fail loud or fall back to alternatives (Gemini for script, browser-Kokoro for TTS, "video unavailable")? v1: fail loud, log to console; v2: configurable.
- **HUB_TOKEN rotation** — manual for v1. If multiple humans ever use this, revisit.
- **macOS sleep / display-off behavior** — does the Mac Studio aggressively idle the GPU under launchd-only workloads? May need `caffeinate -di` wrapper for video gen jobs. Confirm post-research.

## 8. Deferred / future

- RAG sidecar (Deep Lake or alternative) — when there's an actual document corpus to index.
- MLX-native stack (mflux, etc.) alongside ComfyUI — only if research shows clear wins.
- LM-Studio-style model picker UI — webapp work, blocked on the hub-supervisor :9100 endpoint existing.
- Multi-host: extend to a second Mac (Studio Ultra?) — keep the gateway agnostic, scale by adding more `hub` endpoints.

## 9. Document hygiene

This doc lives in its own repo (`~/Documents/code/mac-studio-hub/`) so it survives the webapp moving / being renamed. Future contents of that repo:

```
mac-studio-hub/
├── docs/ARCHITECTURE.md         # this doc
├── sidecars/
│   ├── kokoro/                  # pyproject.toml + server.py
│   ├── whisper/
│   └── comfyui/                 # deploy notes + node-pack list, NOT the upstream code
├── deploy/
│   ├── launchd/                 # plist templates
│   ├── install.sh               # bootstrap on a fresh Mac
│   └── token-gen.sh
└── README.md
```
