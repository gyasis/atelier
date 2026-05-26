# PRD — Atelier Native Dashboard (macOS / SwiftUI)

**Status:** Draft, parked for future work (2026-05-26)
**Project codename:** **Atelier**
**Author:** drafted with Claude during the Radio Mode voice-engine hardening session
**Important:** This PRD is **planning-only**. Implementation is deferred until explicitly scheduled. Captured so the design context isn't lost.

---

## 1. Problem

Atelier already exposes rich observability — but only as JSON over HTTP and a terminal table:

- governor `:8799` — `/pressure` (level, free/resident GB, swapouts, tenants, auto_action, recommendation), `/telemetry` (recent calls + synths + spill), `/predictor/stats`, `/estimate`
- each sidecar — `/readyz` (lifecycle, active_jobs, queue_depth)
- Ollama — `/api/ps`
- `atelier-status` CLI — a one-shot terminal roll-up

To *see* the hub today you `curl` JSON or read a CLI table. The Constitution's **Principle I — "everything observable, no black boxes"** deserves a beautiful native pane of glass: a macOS app that shows the whole atelier breathing in real time.

**User framing (2026-05-26):** *"a mac native dashboard that gives us all the observability of incoming and outgoing, class, and memory pressure — in a beautiful graph and text, interactive, Swift style."*

## 2. Goals

- **Native macOS app** — SwiftUI + Swift Charts. Beautiful + editorial, not a generic admin template.
- **At-a-glance, real-time** view of: memory pressure, inference flow (in + out), model/class compute stats, governor state.
- **Read-first** (observe), with an optional **human-gated control surface** (phase 2).
- Runs **on the Mac**, talks to the governor + sidecars over `127.0.0.1`.

## 3. What it shows — panels → data source → viz

| Panel | Source | Visualization |
|---|---|---|
| **Memory pressure** (hero) | `/pressure`: `level`, `free_gb`, `resident_gb`, `swapouts` (poll ~2–3s) | Live area chart of resident GB over time with the **WARN (45 GB)** + **CLIFF (55 GB)** lines drawn; a big colored status pill (green/amber/red); swapouts counter (flashes red if rising = swap death) |
| **Tenants** (what's loaded) | `/pressure` `tenants[]` — sidecars (idle/busy/cold, active_jobs, queue_depth) + Ollama models (mem_gb) | A live card board, one per sidecar/model: state dot, busy/queue, memory. Animates busy↔idle |
| **Inference flow** (in + out) | `/telemetry` `recent_calls` (Ollama) + `recent_synths` (TTS: chars, seconds, engine) | Throughput strip — incoming req/min, outgoing tok/s (LLM) + audio-sec/s (TTS); a rolling timeline of recent calls (engine · size · latency) |
| **Classes + per-model stats** | `/predictor/stats` (per kind+model: runs, avg_rate, avg_seconds) + predictor classes (`llm:thinking / large / standard / claude`, `tts`) | Per-class bars of tok/s (or s/char for TTS), run counts, the Bayesian rate — the "shareable compute-stats" made visual |
| **Predictor ETA** | `/estimate` | A small "what would model X / N chars take?" widget |
| **Governor actions** | `/pressure` `auto_action` + `recommendation` | Last auto make-room (the watcher) + any pending **force-stop recommendation** as an actionable card |

## 4. Interaction model

- **Live polling** of the governor every ~2–3 s (SSE/websocket later if the governor grows a stream).
- **Display:** a **menu-bar item** (always-glance — pressure pill + headline numbers) that opens a **full window** for the detailed graphs. (Both, recommended.)
- **Read-only by default.** **Phase 2 control surface:** a native UI for the human-gated `/force-stop` two-phase yield negotiation — a dialog that renders the *preview* (who's asking, what's busy, the disruption) with an **Authorize** button that sends `confirm=true` + the token. This makes the agent-layer "are you sure?" a real native prompt. Plus a manual **Make Room** button (`/make-room`).

## 5. Architecture

- **SwiftUI**, target macOS 14+; **Swift Charts** for graphs.
- **Networking:** `URLSession` polling the governor `:8799` + sidecars over `127.0.0.1` (the app runs on the Mac). `Codable` structs mirror the JSON shapes (`/pressure`, `/telemetry`, `/predictor/stats`, `/readyz`).
- A thin **`AtelierClient` actor** polls + publishes `@Published` state; views observe it.
- **No auth** — local Mac, matches the open-LAN trust model.
- **Read-only observer** honors Constitution I — it never mutates hub state except via the explicit, human-gated control surface (phase 2).
- **Distribution:** a local unsigned `.app` (Spotlight-launchable) and/or a LaunchAgent menu-bar item.

## 6. Design language

- **Editorial / instrument-panel** aesthetic — NOT a SaaS/Grafana dashboard template (per the standing preference that Atelier visuals are editorial, not SaaS-landing). Dark, restrained accent, serif headers + monospace data, generous whitespace. The pressure chart is the hero. Think a financial terminal × an oscilloscope, not an admin panel.
- **Graph + text interwoven** — every graph has a plain-text readout beside it (the "beautiful graph and text" the user asked for).

## 7. Scope / phases

- **Phase 1 — read-only monitor:** pressure chart + tenant board + flow strip + class stats. The pane of glass.
- **Phase 2 — control:** human-gated `/force-stop` dialog + manual `/make-room`.
- **Phase 3 — history:** persist telemetry locally for longer time-ranges + trend charts (the predictor `runs` store already holds history; chart rates over weeks).

## 8. Non-goals

- Not a replacement for the governor — the governor stays the source of truth; the app is a viewer.
- No remote/cloud access (local Mac only).
- No editing of model configs / no model management beyond the gated force-stop / make-room.

## 9. Open questions

- Menu-bar vs standalone window vs both? (lean: both)
- Polling vs adding an SSE/websocket stream to the governor? (start: polling)
- Swift Charts enough, or a custom Canvas/Metal layer for a true live "oscilloscope" feel?
- Surface ComfyUI job progress too (it has its own WS at `:8188`)?
- Live where — a `dashboard/` Xcode project inside the atelier repo, or a separate repo?

## 10. Why this matters

This is **Principle I (everything observable) made tangible** — the monitor-first ethos as a beautiful native artifact. It turns the governor's JSON into a living picture of the atelier: you glance at the menu bar and *know* the hub's state.

---

**Ephemeral marker:** parked product PRD (not a session work-tracker). Keep until the dashboard is built or explicitly dropped. Companion docs: `MEMORY_GOVERNOR.md` (the data source), `PREDICTOR.md` (the class stats), `CONSTITUTION.md` (Principle I), `README.md` §Topology (where this app sits — on the Mac, observing the hub).
