# Atelier sidecar pattern (2026-05-24)

Every new model that lands on the atelier hub (Mac Studio at 192.168.0.159, OR
local Linux box at localhost) MUST follow this shape. No exceptions — drift
costs unified memory or makes observability blind.

## 1. Layout

```
~/services/<name>-sidecar/      # Mac
~/services/<name>-local/        # Linux (mirrors the Mac sidecar, different port)
  .venv/                        # uv venv, Python 3.10 or 3.12
  server.py                     # FastAPI app — see template below
```

Source-tracked in `~/Documents/code/atelier/sidecars/<name>/` and symlinked
into `~/services/<name>-sidecar/` on the Mac.

## 2. Required endpoints

| Method | Path | Purpose |
|---|---|---|
| GET  | /healthz       | Liveness — returns 200 always when process alive |
| GET  | /readyz        | Readiness — see schema below |
| POST | /<verb>        | The actual work endpoint (e.g. /tts, /generate) |
| POST | /admin/unload  | Force-unload the model NOW (manual eviction) |

## 3. /readyz schema (LOCKED — atelier-status depends on this)

```json
{
  "ok": true,
  "state": "warm" | "cold",
  "warmed": true | false,
  "model": "<model id>" | null,
  "device": "mps" | "cuda (rtx-2060)" | "cpu" | ...,
  "voices": <int> | null,      // multi-speaker engines only
  "idle_seconds": <float>,
  "idle_unload_seconds": <int>,
  "keep_warm": true | false,
  "last_unload_ago_s": <float> | null
}
```

## 4. Required env vars (consistent across sidecars)

| Var | Default | Notes |
|---|---|---|
| IDLE_UNLOAD_SECONDS | 180 (model-heavy) or 300 (live) | Idle threshold before auto-unload |
| KEEP_WARM | false (overnight-only) or true (live engine) | Disable auto-unload entirely |
| HUB_TOKEN | (unset) | If set, /tts AND /admin/unload require Bearer auth |
| <NAME>_MODEL_PATH | absolute path | Per-sidecar model path |

## 5. Required code shape (lifespan + watcher)

Every server.py has these module-level globals + helpers (see existing
sidecars for full templates — `voice-clone-sidecar/server.py` is the most
complete reference):

```python
_model = None
_warmed = False
_sem = asyncio.Semaphore(1)              # single-flight inference
_last_request_at = time.monotonic()
_unload_task: asyncio.Task | None = None
_idle_unloaded_at: float | None = None

async def _load_and_warm(): ...           # idempotent cold-load + warmup
async def _unload_model(): ...            # del model + gc.collect() + torch.{mps,cuda}.empty_cache()
async def _idle_watcher(): ...            # 30s tick; checks idle > IDLE_UNLOAD_SECONDS; calls _unload_model()
```

In `lifespan(app)`:
1. Load + warm under `_sem`
2. `_unload_task = asyncio.create_task(_idle_watcher())`
3. `yield`
4. On shutdown: cancel `_unload_task`

In `/tts` (or equivalent):
1. If cold (`_model is None` or `not _warmed`): trigger `_load_and_warm()` under `_sem`
2. Update `_last_request_at = time.monotonic()`
3. Generate

## 6. After scaffolding a NEW sidecar — checklist

- [ ] Pick a free port (Mac `87xx`, Linux `187xx` to mirror with +10000 offset)
- [ ] Choose KEEP_WARM default — `false` for slow/overnight engines (Dia, future Wan), `true` for live (Kokoro, OmniVoice)
- [ ] Add launchd plist on Mac (`io.macstudio.hub.<name>`) OR systemd user unit on Linux (`<name>.service`)
- [ ] **REGISTER in `~/.local/bin/atelier-status` SIDECARS list** so the CLI picks it up
- [ ] Verify `/readyz` returns the locked schema (run `atelier-status` — should appear)
- [ ] Test cold-load cycle: warm → `atelier-status --unload <name>` → state=cold → make a real request → state=warm again
- [ ] Document in atelier README that the sidecar exists + which use case it serves

## 7. Anti-patterns (blocking — caught in code review)

- ❌ Loading the model in `Kokoro(...)` at module top-level (skips the cold-load path)
- ❌ Not assigning `_unload_task = asyncio.create_task(...)` (task gets GC'd silently)
- ❌ Reading model state from inside the watcher's await condition (race with /tts)
- ❌ `Restart=on-failure` in systemd/launchd (per economy-first rule — buggy ML workers shouldn't auto-respawn)
- ❌ Forgetting `gc.collect()` after `_model = None` (CPython holds the reference longer than needed)

## 8. References

- Origin incident: 2026-05-24, Mac at 62GB/64GB with Dia idle but resident, OmniVoice install would have triggered swap death
- Constitutional principle in Graphiti: "Preference: IDLE-MODEL-UNLOADING is a constitutional principle for inference hubs"
- Apple Silicon hard rules: `~/.claude/rules/tools/ollama-apple-silicon.md`
- Reference sidecars (most-to-least complete):
  - `voice-clone-sidecar/server.py` (Dia) — full pattern: idle-unload + cold-load + /admin/unload
  - `kokoro-sidecar/server.py` (Mac Kokoro) — same pattern, KEEP_WARM=true default
  - `kokoro-local/server.py` (Linux Kokoro) — same pattern, CUDA device-label

## 9. CLI quick-reference

```bash
atelier-status                      # state of all sidecars
atelier-status --watch              # live updating
atelier-status --unload <name>      # force-evict one
atelier-status --json | jq          # programmatic consumption
```
