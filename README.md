# Atelier — Mac Studio inference hub

LAN-only AI model server running on a Mac Studio (Apple M1 Max, 64 GB). Hosts production-grade ML inference workloads as Python sidecars so any client on the LAN (a SvelteKit webapp, a notebook, a CLI) can hit one HTTP endpoint per workload instead of running models in-browser.

**Codename: Atelier** (workshop). Internal-only.

## What runs here

| Sidecar | Port | Engine | Use case |
|---|---|---|---|
| `omnivoice` | 8770 | k2-fsa/OmniVoice (Diffusion LM, PyTorch MPS) | **PRIMARY TTS** — natural multi-speaker via instruct (accent/pitch/gender), ~0.6–1× RTF |
| `kokoro` | 8765 | kokoro-onnx (CoreML/MPS) | Fallback TTS — fast, fixed-voice library, ~0.6s/line (twin on Linux `:18765`) |
| `dia` | 8769 | nari-labs/Dia-1.6B-0626 (PyTorch MPS) | Expressive cloning TTS — **retired for live** (~10× RTF), overnight batch only |
| `comfyui` | 8188 | ComfyUI (PyTorch MPS) | Image + video generation. Wan2.1 1.3B + 14B Q4 verified |
| Ollama | 11434 | Apple's prebuilt | LLM serving (gemma, qwen3:32b, etc.) — managed outside Atelier but co-resident |

**Start here:** [`docs/ATELIER.md`](docs/ATELIER.md) — the full overview (what it is, how it
works, all media types, ambitions). Visual: [`docs/atelier-infographic.html`](docs/atelier-infographic.html).
Deep design: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Topology — two boxes

Atelier is the **compute** half of a two-machine setup. It runs on the Mac; the
things that *consume* it run on a separate **Linux box**, over the LAN.

```
      Linux box (the Blade)                         Mac Studio · 192.168.0.159
      consumers / orchestration / UI                  "Atelier" · the compute
 ┌──────────────────────────────────┐         ┌──────────────────────────────────┐
 │ githubawesome webapp      :5757   │         │ omnivoice  (TTS, primary)  :8770  │
 │   SvelteKit · Radio Mode          │ ──HTTP─▶│ kokoro     (TTS, fallback) :8765  │
 │ Claude Code agent + skills        │   LAN   │ dia        (clone, batch)  :8769  │
 │ graphiti-mcp · FalkorDB   :6380   │         │ comfyui    (image/video)   :8188  │
 │ kokoro twin (offline TTS) :18765  │ ◀─JSON──│ governor   (mem + ETA)     :8799  │
 │ predictor /report caller          │         │ Ollama     (local LLMs)   :11434  │
 └──────────────────────────────────┘         └──────────────────────────────────┘
```

|  | **Mac Studio** (`192.168.0.159`) | **Linux box** (the Blade) |
|---|---|---|
| **Role** | The compute — *Atelier* itself | The consumers / orchestration / UI |
| **Runs** | omnivoice · kokoro · dia · comfyui · governor sidecars + Ollama — all under launchd | githubawesome webapp, Claude agent + skills, graphiti-mcp (FalkorDB), a local Kokoro twin |
| **Owns** | The models, the unified-memory/MPS GPU, the memory **governor**, the voice refs (`~/models/voice-refs/`), the predictor store | The product UX (Radio Mode), the corpus DB (SQLite), agent memory, the desktop launcher |
| **Speaks** | HTTP sidecar endpoints (`/tts`, `/healthz`, `/readyz`, `/admin/unload`, …) | Calls those endpoints over the LAN; POSTs real run stats back to the governor's predictor |

**Why split this way:** the Mac has the 64 GB unified memory + MPS to host the
models; the Linux box drives the experience and keeps heavy ML compute off the
workstation. The boxes reboot independently. The Linux **Kokoro twin** (`:18765`)
exists as a Mac-offline TTS fallback — note the podcast itself is deliberately
**OmniVoice-only** (a Kokoro fallback would swap the cloned host voices mid-show),
so a Mac outage stops the podcast rather than degrading its voices.

## Hardware requirements

- Apple Silicon Mac (M1/M2/M3 with Max or Ultra variant strongly recommended)
- 64 GB unified memory — needed to coexist Ollama + ComfyUI + a sidecar without swap
- ~150 GB free disk for models (Wan2.1 + Dia + Kokoro + cached HF)
- macOS 14+ (tested on 15.5 Sequoia)

## Install (fresh Mac)

```bash
git clone <this-repo> ~/Documents/code/atelier
cd ~/Documents/code/atelier
bash deploy/install.sh
```

`install.sh` will:
1. Verify prerequisites (uv, Homebrew, Xcode CLT).
2. Create `~/services/<sidecar>/.venv/` for each sidecar.
3. Symlink the canonical `sidecars/<name>/server.py` from this repo into `~/services/<name>/server.py`.
4. Copy the launchd plists from `launchd/` into `~/Library/LaunchAgents/`.
5. Pull the required models into `~/models/` (kokoro-onnx, dia weights, etc.).
6. `launchctl bootstrap` each plist so services come up.

## Develop

You can hack on Atelier from **either** machine:
- Locally on the Mac (`cd ~/Documents/code/atelier && edit server.py`) — change takes effect on the next `launchctl kickstart -k gui/$(id -u)/io.macstudio.hub.<name>`.
- Remotely from a Linux box over SSH (e.g. `ssh gyasisutton@192.168.0.159 'cd ~/Documents/code/atelier && ...'`).

The sidecar `~/services/<name>/server.py` files are symlinks INTO this repo, so edits in the repo land directly in the running service path.

## Repo layout

```
atelier/
├── README.md                                — this file
├── docs/
│   ├── ARCHITECTURE.md                      — full design + research notes
│   ├── PRD-atelier-media-toolkit.md         — parked PRD for media helper skills
│   └── research/                            — Gemini deep-research outputs
├── sidecars/
│   ├── kokoro/{server.py,requirements.txt}  — fast basic TTS
│   └── dia/{server.py,requirements.txt}     — expressive multi-speaker TTS
├── launchd/                                 — io.macstudio.hub.*.plist files
└── deploy/
    └── install.sh                           — bootstrap a fresh Mac
```

## Operate

```bash
# List running services
launchctl list | grep io.macstudio.hub

# Restart a sidecar after editing its server.py
launchctl kickstart -k gui/$(id -u)/io.macstudio.hub.kokoro
launchctl kickstart -k gui/$(id -u)/io.macstudio.hub.dia

# Tail logs
tail -f ~/Library/Logs/dia-sidecar.err.log
tail -f ~/Library/Logs/kokoro-sidecar.err.log
tail -f ~/Library/Logs/comfyui.err.log

# Reachability from a LAN client
curl http://192.168.0.159:8769/healthz   # Dia
curl http://192.168.0.159:8765/healthz   # Kokoro
curl http://192.168.0.159:8188/system_stats   # ComfyUI
```

## Models

Everything lives under `~/models/`. Not in this repo (too big). `install.sh` downloads what's needed.

```
~/models/
├── kokoro/                  — kokoro-v1.0.fp16.onnx, voices-v1.0.bin
├── voice-refs/              — leo_ref.wav, sarah_ref.wav (for Dia cloning)
├── hf-cache/                — HuggingFace cache (Dia + future)
└── unet/, vae/, clip/       — ComfyUI: Wan2.1 1.3B + 14B Q4 + umt5 + vae
```

## Related

- **githubawesome webapp** (`~/Documents/code/githubawesome/webapp/` on the Linux box) — the first Atelier *client*. Proxies its `/api/podcast/tts` endpoint to Atelier's `:8765` or `:8769`.
- **Global rule** `~/.claude/rules/tools/ollama-apple-silicon.md` — Ollama coexistence rules, MPS env vars, ComfyUI custom-node compatibility — applied by every Claude session that touches this stack.

## License

Internal-use only. Sidecar model licenses vary (see each sidecar's README for upstream license). No redistribution of cloned voices that mimic real people without their consent.
