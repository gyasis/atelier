# Movable Panel Layout — Gridstack plan

**Goal:** turn the Atelier dashboard from a hardcoded CSS grid into a **Photoshop/Superset-style workspace** where the user drags, resizes, and rearranges panels themselves — and the layout persists — instead of us editing CSS to move things.

**Status:** ✅ **implemented** (2026-06-06). Gridstack vendored at `dashboard/ui/vendor/`, all 6 panels are draggable/resizable widgets, Edit/Lock + Reset toolbar, layout persists to `localStorage["atelier.layout.v1"]`, charts resize on tile resize. Verified in-browser (6 widgets, persistence, charts survive moves). Target was `dashboard/ui/` only — no Rust/backend changes.

---

## 1. Library decision — Gridstack.js

| Option | Verdict |
|---|---|
| **Gridstack.js** ✅ | Vanilla TS/JS, **zero dependencies**, ~10 KB, drag + resize + snap-to-grid + **serialize/restore** layout, responsive breakpoints. Vendors as a local file → keeps the Tauri app **offline**. The framework-agnostic equivalent of what Apache Superset uses (`react-grid-layout`). The proportionate choice for a dashboard of rearrangeable tiles. |
| Golden Layout / Dockview | True IDE/Photoshop **docking** — tab stacks, nested splits, pop-out windows. Heavier, steeper, different mental model (window management). Revisit only if we need tabs/nesting, not for tiles. |

**Pick: Gridstack.js.** Offline-vendorable, vanilla (our webview is plain JS + Chart.js, no React), and it does exactly drag/resize/persist.

---

## 2. Current state (what we're converting)

Two hardcoded CSS grids in `dashboard/ui/index.html`:

- `.app` (top): `grid-template-columns: 1.15fr 1fr 1.25fr`
- `.hud` (footer): `grid-template-columns: 0.85fr 1.15fr 1.25fr`

Six panels (`data-od-id` preserved for the OpenDesign source mapping):

| Panel `data-od-id` | Contents | Chart canvas |
|---|---|---|
| `memory-panel` | Resident Memory big-readout + chart | `#memChart` |
| `tenants-panel` | Tenants/Sidecars tile grid (`#tenantGrid`) | — |
| `tasks-panel` | Task Stream: activity gantt + table | — |
| `hud-pie` | Loaded-now pie + legend | `#pieChart` |
| `hud-lines` | Inference latency (TTFT) + generation tok/s | `#ttftChart`, `#tpsChart` |
| `hud-status` | Sidecar status table | — |

4 Chart.js charts total — these need an explicit resize hook (Chart.js doesn't auto-reflow inside a Gridstack resize).

---

## 3. Target design

- Each `<section class="panel" data-od-id="…">` becomes a **Gridstack widget**: wrapped in `.grid-stack-item > .grid-stack-item-content`, with `gs-x / gs-y / gs-w / gs-h` initial coords (12-column grid).
- One `.grid-stack` container replaces both CSS grids (panels can move freely between the old top/bottom regions).
- **Edit/Lock toggle** (a small `✎ / 🔒` button in the header): `grid.setStatic(true|false)`. Default **locked** so normal viewing doesn't accidentally drag; unlock to rearrange.
- **Persistence:** on `grid.on('change')`, save `grid.save(false)` (layout only, no content) to `localStorage["atelier.layout.v1"]`; restore on load. (Later: persist to a Tauri file via a `save_layout` command so it survives reinstalls and syncs the HUD window.)
- **Reset layout** button → clear storage, re-apply the default coords.

Default coordinate map (12-col, ~6 rows; mirrors the current arrangement):

```
memory-panel   x0  y0  w4 h4      tenants-panel  x4 y0 w4 h4    tasks-panel x8 y0 w4 h8
hud-pie        x0  y4  w4 h4      hud-lines      x4 y4 w4 h4    hud-status  x8 y8 w4 h4
```

---

## 4. Implementation steps

1. **Vendor Gridstack offline** — download into `dashboard/ui/vendor/`:
   - `gridstack-all.js` (UMD bundle, ~v11.x)
   - `gridstack.min.css`
   Add `<link>` + `<script>` tags to `index.html` (local paths, no CDN — matches the existing local `vendor/chart.umd.min.js`).
2. **Restructure markup** — wrap the 6 panels in `.grid-stack` / `.grid-stack-item` with initial `gs-*` attrs. Keep each panel's inner DOM (and `data-od-id`) intact so `refreshAll()`'s selectors (`[data-od-id="tenants-panel"] …`, `#tenantGrid`, canvas ids) keep working unchanged.
3. **Init Gridstack** — `GridStack.init({ column: 12, cellHeight: '7vh', float: true, margin: 6, staticGrid: true, handle: '.panel-drag' })`. Add a tiny drag handle in each panel header.
4. **Edit/Lock toggle** — button flips `grid.setStatic()`; show a subtle "edit mode" outline when unlocked.
5. **Chart.js resize** — `grid.on('resizestop', (e, el) => { el.querySelectorAll('canvas').forEach(c => Chart.getChart(c)?.resize()); })`. Also resize on `change` for programmatic moves.
6. **Persist + restore** — `grid.on('change', saveLayout)`; on boot, if stored layout exists, `grid.load(stored)` before wiring data; else apply defaults.
7. **Theme** — override Gridstack's default CSS (resize handles, placeholder) to the dark/gold palette so it matches the OD design. Keep panel borders/headers as-is.
8. **HUD window** — `hud.html` is a separate webview; give it its own (smaller) default layout + storage key, or leave it fixed for v1.

---

## 5. Risks / notes

- **Chart reflow** is the main gotcha — verified hook is `resizestop` + `Chart.resize()`. Without it charts keep their old pixel size.
- **`data-od-id` must survive** — they map back to the OpenDesign prototype; don't strip them when wrapping.
- **Embedded HUD iframe** (`#hudIframe`) sits inside the layout — treat it as a normal widget or pin it.
- Offline: never reference a CDN at runtime; vendor everything (the app is a sealed `.app`).
- No backend impact — this is purely `dashboard/ui/` + one optional Tauri `save_layout`/`load_layout` command later for file-based persistence.

---

## 6. Future

- File-based layout persistence via a Tauri command (survives reinstalls; shareable presets).
- Multiple named layouts ("monitoring", "debug", "compact") with a switcher.
- If tabs/nesting/pop-out windows are ever wanted → migrate to **Golden Layout** or **Dockview** (bigger lift; only if the tile model proves too limiting).

Sources: [Gridstack.js](https://gridstackjs.com/) · [github.com/gridstack/gridstack.js](https://github.com/gridstack/gridstack.js/) · [JS grid layouts 2026 (Sencha)](https://www.sencha.com/blog/must-try-javascript-grid-layouts-for-modern-web-design/)
