# Progress

**Last Updated**: 2026-07-04 16:37:32

## Overall Progress
- Total Tasks: 24
- Completed: 21 ✅
- Pending: 3 ⏳
- Progress: 87%

## Task Breakdown
- [x] **T001** Install Rust toolchain if absent: `curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y && source "$HOME/.cargo/env"`. Verify `cargo --version`. Install Tauri CLI v2: `cargo install tauri-cli --version "^2"`. Verify `cargo tauri --version`.
- [x] **T002** Scaffold the Tauri project at `dashboard/`: from repo root run `cargo tauri init` with app name `AtelierDashboard`, window title `Atelier`, `distDir` pointing to `../ui/dist`, `devUrl` pointing to `http://localhost:1420`. Accept all other defaults. This creates `dashboard/src-tauri/` with `Cargo.toml`, `tauri.conf.json`, `src/main.rs`, `src/lib.rs`, `capabilities/`.
- [x] **T003** [P] Create `dashboard/ui/` — the HTML frontend that the webview loads. Copy `atelier-hub-dashboard.html` from the design prototype as `dashboard/ui/index.html`. Strip the `<script src="https://cdn.jsdelivr.net/npm/chart.js...">` CDN tag and replace with a local `dashboard/ui/vendor/chart.umd.min.js` (download it). This makes the app fully offline.
- [x] **T004** [P] Add `dashboard/ui/hud.html` — copy `mini-hud-card-2.html` as the mini HUD card page. Same CDN-to-local replacement as T003.
- [x] **T005** [P] Create `dashboard/ui/js/atelier-bridge.js` — the JS side of the Tauri bridge. Exports `async function fetchPressure()`, `fetchTelemetry()`, `fetchPredictorStats()`, `fetchSidecars()` — each calls `window.__TAURI__.invoke('get_pressure')` etc. and returns the parsed JSON. Falls back to mock data if `window.__TAURI__` is undefined (for browser preview).
- [x] **T006** Add dependencies to `dashboard/src-tauri/Cargo.toml`: `reqwest = { version = "0.12", features = ["json"] }`, `serde = { version = "1", features = ["derive"] }`, `serde_json = "1"`, `tokio = { version = "1", features = ["full"] }`, `tauri = { version = "2", features = ["tray-icon"] }`.
- [x] **T007** Create `dashboard/src-tauri/src/models.rs` — `serde` `Deserialize` structs mirroring the governor JSON:
- [x] **T008** [P] Create `dashboard/src-tauri/src/fetcher.rs` — async functions using `reqwest::Client`:
- [x] **T009** [P] Create `dashboard/src-tauri/src/commands.rs` — Tauri `#[tauri::command]` handlers:
- [x] **T010** Wire commands in `dashboard/src-tauri/src/lib.rs` — register `get_pressure`, `get_telemetry`, `get_predictor_stats`, `get_sidecars` in `tauri::Builder`. Import `commands` and `fetcher` modules. Add `models` module.
- [x] **T011** Update `dashboard/ui/index.html` — replace all mock `const memData = [...]` style static arrays with calls to `atelier-bridge.js`. On load: call `fetchPressure()` → populate memory chart + tenant cards + status pill. Call `fetchTelemetry()` → populate inference flow strip + rolling buffer. Call `fetchSidecars()` → merge with pressure tenants. Call `fetchPredictorStats()` → populate class stats bars. Set a `setInterval` at 2500ms to refresh all data. Wire `dashboard/ui/js/atelier-bridge.js`.
- [x] **T012** [P] Update `dashboard/ui/hud.html` — same bridge wiring for the mini HUD card. `fetchPressure()` → top bar pill + pie chart data. `fetchSidecars()` → busiest 3 rows. `fetchTelemetry()` → invocation feed + tokens/sec sparkline. 2500ms poll. Update the `● LIVE · updated Ns ago` counter using `Date.now()`.
- [x] **T013** [P] Create `dashboard/ui/js/charts.js` — extract all Chart.js initialisation from `index.html` into a module (`initMemoryChart`, `initPieChart`, `initClassChart`, `initFlowCharts`). Each accepts a data object and returns the chart instance. `index.html` imports and calls these after each poll. Keeps the HTML clean.
- [x] **T014** Configure menu-bar tray in `dashboard/src-tauri/tauri.conf.json` — set `systemTray.iconPath` to `icons/tray-icon.png`, `systemTray.iconAsTemplate` true (macOS template icon). Window config: `decorations: false`, `transparent: true`, `alwaysOnTop: false`, initial size `1440x820`.
- [x] **T015** Create `dashboard/src-tauri/src/tray.rs` — build the tray menu:
- [x] **T016** [P] Create `dashboard/src-tauri/icons/tray-icon.png` — a 22×22 dark monochrome "A" lettermark (the Atelier logo) as a PNG. Use the macOS template icon convention (black on transparent). Script it with ImageMagick: `convert -size 22x22 xc:transparent -fill black -font Helvetica-Bold -pointsize 14 -gravity Center -annotate 0 "A" dashboard/src-tauri/icons/tray-icon.png`.
- [x] **T017** [P] Add a `Tauri::command` `get_pressure_summary() -> String` in `commands.rs` that returns a one-liner like `"38.4 GB · warn"` for tray tooltip. Wire it via a 5s polling JS call that updates `document.title` with the pressure summary so the tray tooltip stays current.
- [x] **T018** Add `dashboard/ui/package.json` with a `build` script: `cp -r . dist` (static copy — no bundler needed since we have no npm deps beyond the vendored Chart.js). Verify `cargo tauri build` resolves `distDir` correctly.
- [x] **T019** Run `cargo tauri build` from `dashboard/` — produces `dashboard/src-tauri/target/release/bundle/macos/AtelierDashboard.app`. Fix any compile errors. Record final binary size.
- [x] **T020** [P] Create `dashboard/README.md` — build instructions (`rustup`, `cargo tauri dev` for dev, `cargo tauri build` for release), which governor endpoints it polls, how to install the `.app` (drag to `/Applications` or launch via Spotlight), Phase 1 scope.
- [x] **T021** [P] Update root `README.md` — add `## Native Dashboard` section: what it is, Tauri stack, link to `dashboard/README.md`.
- [ ] **T022** Add `force_stop(token: String) -> Result<String, String>` Tauri command in `commands.rs` — POST to `127.0.0.1:8799/force-stop` with `{"confirm": true, "token": token}`.
- [ ] **T023** [P] Add `make_room() -> Result<serde_json::Value, String>` command — POST to `127.0.0.1:8799/make-room`.
- [ ] **T024** [P] Add force-stop confirmation dialog to `dashboard/ui/index.html` — renders the `recommendation` from `/pressure` as a native `<dialog>` element. **Authorize** button calls `invoke('force_stop', {token})`. Human-gated: never auto-fires.

## Recent Milestones

