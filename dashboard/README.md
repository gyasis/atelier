# Atelier Dashboard

Native macOS observability app for the Atelier inference hub. Built with Tauri 2 (Rust backend + HTML/Chart.js frontend).

## What it shows

- **Memory pressure** — resident GB vs WARN (45 GB) / CLIFF (55 GB) thresholds, live area chart
- **Tenants** — one card per sidecar/model: state (up/busy/cold), active jobs, queue depth, memory GB
- **Task stream** — gantt swimlane + timestamped table of recent inference calls
- **Scene Info HUD** — always-visible bottom strip: loaded-memory pie, TTFT + tokens/sec line charts, sidecar status table
- **Mini HUD card** — compact floating overlay (420×320 px) accessible from the menu-bar tray

## Data sources

Polls every 2.5 s over `127.0.0.1`:

| Source | Endpoint |
|---|---|
| Governor | `:8799/pressure`, `/telemetry`, `/predictor/stats` |
| omnivoice | `:8770/readyz` |
| kokoro | `:8765/readyz` |
| whisper | `:8766/readyz` |
| dia | `:8769/readyz` |
| comfyui | `:8188/readyz` |
| Ollama | `:11434/api/ps` |

Log-tail watcher reads `~/Library/Logs/{ollama,omnivoice,whisper,comfyui}.log` to extract real-time `tokens/s`, synthesis RTF, ASR RTF, and image generation progress for the sparklines.

## Build

```bash
# Prerequisites
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
source "$HOME/.cargo/env"
cargo install tauri-cli --version "^2"

# Dev (hot-reload)
cd dashboard
cargo tauri dev

# Release
cargo tauri build
# → src-tauri/target/release/bundle/macos/AtelierDashboard.app
# → src-tauri/target/release/bundle/dmg/AtelierDashboard_0.1.0_aarch64.dmg
```

## Install

Drag `AtelierDashboard.app` to `/Applications`, or double-click the `.dmg`.

The app icon appears in the menu bar. Left-click toggles the dashboard window. Right-click for Mini HUD / Quit.

## Phase scope

- **Phase 1** (this build): read-only observer — all panels, log watcher, menu-bar tray, mini HUD
- **Phase 2** (deferred): human-gated force-stop dialog + make-room button
