---
description: "Atelier Native Dashboard — Tauri (Rust + HTML webview) — real-time observability of the inference hub"
---

# Tasks: Atelier Dashboard — Tauri Build

**Branch**: `feat/native-dashboard`
**Design reference**: HTML prototypes in `/Users/gyasisutton/Documents/code/open-design/.od/projects/atelier-hub-dashboard/`
- Full dashboard: `atelier-hub-dashboard.html`
- Mini HUD card: `mini-hud-card-2.html`
- Side-by-side preview: `side-by-side.html`

**Architecture**: Tauri 2 app — Rust backend polls the Atelier governor + sidecar HTTP endpoints, passes live JSON to the HTML webview via Tauri `invoke` commands. Ships as a macOS `.app` with a menu-bar item.

**Data sources** (all on `127.0.0.1`):
- Governor `:8799` — `/pressure`, `/telemetry`, `/predictor/stats`
- Sidecars `:8770` `:8765` `:8769` `:8188` — `/readyz`
- Ollama `:11434` — `/api/ps`

**Rust prerequisite**: install via `curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y` if `cargo` is not on PATH.

---

## Wave 1 — Toolchain + Project Scaffold

- [x] **T001** Install Rust toolchain if absent: `curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y && source "$HOME/.cargo/env"`. Verify `cargo --version`. Install Tauri CLI v2: `cargo install tauri-cli --version "^2"`. Verify `cargo tauri --version`.

- [x] **T002** Scaffold the Tauri project at `dashboard/`: from repo root run `cargo tauri init` with app name `AtelierDashboard`, window title `Atelier`, `distDir` pointing to `../ui/dist`, `devUrl` pointing to `http://localhost:1420`. Accept all other defaults. This creates `dashboard/src-tauri/` with `Cargo.toml`, `tauri.conf.json`, `src/main.rs`, `src/lib.rs`, `capabilities/`.

- [x] **T003** [P] Create `dashboard/ui/` — the HTML frontend that the webview loads. Copy `atelier-hub-dashboard.html` from the design prototype as `dashboard/ui/index.html`. Strip the `<script src="https://cdn.jsdelivr.net/npm/chart.js...">` CDN tag and replace with a local `dashboard/ui/vendor/chart.umd.min.js` (download it). This makes the app fully offline.

- [x] **T004** [P] Add `dashboard/ui/hud.html` — copy `mini-hud-card-2.html` as the mini HUD card page. Same CDN-to-local replacement as T003.

- [x] **T005** [P] Create `dashboard/ui/js/atelier-bridge.js` — the JS side of the Tauri bridge. Exports `async function fetchPressure()`, `fetchTelemetry()`, `fetchPredictorStats()`, `fetchSidecars()` — each calls `window.__TAURI__.invoke('get_pressure')` etc. and returns the parsed JSON. Falls back to mock data if `window.__TAURI__` is undefined (for browser preview).

---

## Wave 2 — Rust Backend (Data Layer)

- [x] **T006** Add dependencies to `dashboard/src-tauri/Cargo.toml`: `reqwest = { version = "0.12", features = ["json"] }`, `serde = { version = "1", features = ["derive"] }`, `serde_json = "1"`, `tokio = { version = "1", features = ["full"] }`, `tauri = { version = "2", features = ["tray-icon"] }`.

- [x] **T007** Create `dashboard/src-tauri/src/models.rs` — `serde` `Deserialize` structs mirroring the governor JSON:
  - `PressureResponse` (level, free_gb, resident_gb, swapouts, tenants, auto_action, recommendation)
  - `TenantEntry` (name, state, active_jobs, queue_depth, mem_gb)
  - `TelemetryResponse` (recent_calls, recent_synths)
  - `RecentCall` (engine, chars, output_tokens, latency_s, ts)
  - `RecentSynth` (chars, seconds, engine, ts)
  - `PredictorStatsResponse` (stats: Vec<ClassStat>)
  - `ClassStat` (kind, model, runs, avg_rate, avg_seconds)
  - `SidecarReadyz` (lifecycle, active_jobs, queue_depth)

- [x] **T008** [P] Create `dashboard/src-tauri/src/fetcher.rs` — async functions using `reqwest::Client`:
  - `fetch_pressure(client) -> Result<PressureResponse>`  → `GET 127.0.0.1:8799/pressure`
  - `fetch_telemetry(client) -> Result<TelemetryResponse>` → `GET 127.0.0.1:8799/telemetry`
  - `fetch_predictor_stats(client) -> Result<PredictorStatsResponse>` → `GET 127.0.0.1:8799/predictor/stats`
  - `fetch_sidecar(client, port) -> Result<SidecarReadyz>` → `GET 127.0.0.1:{port}/readyz`
  - `fetch_ollama_ps(client) -> Result<serde_json::Value>` → `GET 127.0.0.1:11434/api/ps`
  - All return `Ok(default)` on connection error (governor may be offline).

- [x] **T009** [P] Create `dashboard/src-tauri/src/commands.rs` — Tauri `#[tauri::command]` handlers:
  - `get_pressure() -> Result<serde_json::Value, String>`
  - `get_telemetry() -> Result<serde_json::Value, String>`
  - `get_predictor_stats() -> Result<serde_json::Value, String>`
  - `get_sidecars() -> Result<serde_json::Value, String>` (merges /readyz from all 4 sidecar ports + Ollama /api/ps)
  Each creates a short-lived `reqwest::Client`, calls `fetcher.rs`, serialises to `serde_json::Value`.

- [x] **T010** Wire commands in `dashboard/src-tauri/src/lib.rs` — register `get_pressure`, `get_telemetry`, `get_predictor_stats`, `get_sidecars` in `tauri::Builder`. Import `commands` and `fetcher` modules. Add `models` module.

---

## Wave 3 — Frontend Wiring (Live Data)

- [ ] **T011** Update `dashboard/ui/index.html` — replace all mock `const memData = [...]` style static arrays with calls to `atelier-bridge.js`. On load: call `fetchPressure()` → populate memory chart + tenant cards + status pill. Call `fetchTelemetry()` → populate inference flow strip + rolling buffer. Call `fetchSidecars()` → merge with pressure tenants. Call `fetchPredictorStats()` → populate class stats bars. Set a `setInterval` at 2500ms to refresh all data. Wire `dashboard/ui/js/atelier-bridge.js`.

- [ ] **T012** [P] Update `dashboard/ui/hud.html` — same bridge wiring for the mini HUD card. `fetchPressure()` → top bar pill + pie chart data. `fetchSidecars()` → busiest 3 rows. `fetchTelemetry()` → invocation feed + tokens/sec sparkline. 2500ms poll. Update the `● LIVE · updated Ns ago` counter using `Date.now()`.

- [ ] **T013** [P] Create `dashboard/ui/js/charts.js` — extract all Chart.js initialisation from `index.html` into a module (`initMemoryChart`, `initPieChart`, `initClassChart`, `initFlowCharts`). Each accepts a data object and returns the chart instance. `index.html` imports and calls these after each poll. Keeps the HTML clean.

---

## Wave 4 — Menu Bar + App Shell

- [ ] **T014** Configure menu-bar tray in `dashboard/src-tauri/tauri.conf.json` — set `systemTray.iconPath` to `icons/tray-icon.png`, `systemTray.iconAsTemplate` true (macOS template icon). Window config: `decorations: false`, `transparent: true`, `alwaysOnTop: false`, initial size `1440x820`.

- [ ] **T015** Create `dashboard/src-tauri/src/tray.rs` — build the tray menu:
  - Left-click on icon → show/hide the main window
  - Menu items: `Open Dashboard`, `Open Mini HUD`, separator, `Quit`
  - `Open Mini HUD` opens a second smaller window (`400x340`) loading `hud.html`
  Register in `lib.rs`.

- [ ] **T016** [P] Create `dashboard/src-tauri/icons/tray-icon.png` — a 22×22 dark monochrome "A" lettermark (the Atelier logo) as a PNG. Use the macOS template icon convention (black on transparent). Script it with ImageMagick: `convert -size 22x22 xc:transparent -fill black -font Helvetica-Bold -pointsize 14 -gravity Center -annotate 0 "A" dashboard/src-tauri/icons/tray-icon.png`.

- [ ] **T017** [P] Add a `Tauri::command` `get_pressure_summary() -> String` in `commands.rs` that returns a one-liner like `"38.4 GB · warn"` for tray tooltip. Wire it via a 5s polling JS call that updates `document.title` with the pressure summary so the tray tooltip stays current.

---

## Wave 5 — Build + Package

- [ ] **T018** Add `dashboard/ui/package.json` with a `build` script: `cp -r . dist` (static copy — no bundler needed since we have no npm deps beyond the vendored Chart.js). Verify `cargo tauri build` resolves `distDir` correctly.

- [ ] **T019** Run `cargo tauri build` from `dashboard/` — produces `dashboard/src-tauri/target/release/bundle/macos/AtelierDashboard.app`. Fix any compile errors. Record final binary size.

- [ ] **T020** [P] Create `dashboard/README.md` — build instructions (`rustup`, `cargo tauri dev` for dev, `cargo tauri build` for release), which governor endpoints it polls, how to install the `.app` (drag to `/Applications` or launch via Spotlight), Phase 1 scope.

- [ ] **T021** [P] Update root `README.md` — add `## Native Dashboard` section: what it is, Tauri stack, link to `dashboard/README.md`.

---

## Checkpoint: Phase 1 Complete

T001–T021 done. `AtelierDashboard.app` launches, menu-bar icon shows pressure level, full dashboard window opens with live data from the governor, mini HUD card accessible from tray menu. Commit on `feat/native-dashboard`, open PR to `main`.

---

## Wave 6 — Phase 2: Control Surface (DEFERRED)

*Do not start until Phase 1 PR merges.*

- [ ] **T022** Add `force_stop(token: String) -> Result<String, String>` Tauri command in `commands.rs` — POST to `127.0.0.1:8799/force-stop` with `{"confirm": true, "token": token}`.

- [ ] **T023** [P] Add `make_room() -> Result<serde_json::Value, String>` command — POST to `127.0.0.1:8799/make-room`.

- [ ] **T024** [P] Add force-stop confirmation dialog to `dashboard/ui/index.html` — renders the `recommendation` from `/pressure` as a native `<dialog>` element. **Authorize** button calls `invoke('force_stop', {token})`. Human-gated: never auto-fires.

---

## Dependencies

- Wave 2 depends on Wave 1 (Cargo.toml must exist before adding deps).
- Wave 3 depends on Wave 2 (bridge calls Tauri commands from Wave 2).
- Wave 4 depends on Wave 3 (tray needs commands wired).
- Wave 5 depends on Wave 4 (build needs complete app).
- Wave 6 deferred until Wave 5 checkpoint.

## Parallel opportunities

- T003, T004, T005 in Wave 1 are all `[P]`.
- T007, T008, T009 in Wave 2 are all `[P]` (different files).
- T011, T012, T013 in Wave 3 are all `[P]`.
- T016, T017 in Wave 4 are `[P]`.
- T020, T021 in Wave 5 are `[P]`.

## Notes

- No auth — trust model is local Mac only, matches the open-LAN sidecar pattern.
- Fetch errors return `Ok(default/empty)` — the governor may be offline; the UI degrades gracefully showing zeroes.
- Constitution Principle I: this app IS "everything observable" made tangible.
- Phase 2 force-stop is HUMAN-GATED — the app never auto-sends it.
- Design source of truth: the HTML prototypes in open-design. Do not deviate from the palette (#0E0E0E / #C8A96E / SF Mono).
