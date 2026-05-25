# Atelier Memory Governor — design spec

**Status:** design (2026-05-25). Build not yet started.
**Origin:** Mac reboot killed the un-codified OmniVoice sidecar; the podcast fell back
to single-flight Kokoro and a parallel-prep burst overwhelmed it. Diagnosis surfaced the
deeper gap: Ollama and Atelier share 64 GB with **no coordinator**, and Atelier's
idle-unload is **time-based** — it can't tell a model that's genuinely idle from one
that's mid-render, so it risks reaping a long job.

## The reframe

The governor is **job-aware**, not a blind evictor. The real signal is **what a model is
*doing*** (busy vs idle), NOT "loaded vs cold" and NOT static "LLM-vs-media priority."

### Per-model state (replaces warm/cold binary)

| State | Meaning | Evictable? |
|---|---|---|
| `idle` | loaded, no active job, no queued work | yes — reclaimable |
| `busy` | mid-inference / mid-render | **NO — protected** |
| `cold` | unloaded | n/a (costs nothing) |

### The logic (high level)

1. **QUEUE (default).** A request for a `busy` model gets back-pressure — *"there's a
   line, position N"* — and waits. Agent-based callers get a clear signal, not a forced
   preemption. *(parameter #1: target busy? → enqueue)*
2. **FORCE (human-gated).** A `force` flag can preempt a running job and evict now — but
   ONLY with explicit human approval. Never automatic. *(parameter #2)*
3. **RECLAIM (busy-aware).** On-demand `make-room <GB>` before a heavy job, plus a silent
   pressure-watcher near the ~55 GB swap cliff. **Both evict only `idle` models** — the
   overnight ComfyUI / Dia run is safe from both.
4. **ROUTE.** The governor sits in front of both tenants (Ollama + Atelier sidecars),
   knows what each is doing, routes by media type, queues contention, reclaims idle only.

> Never evict what's working. Queue the newcomer. Reclaim only idle. Force only with a
> human in the loop.

### Why not the documented `ollama-apple-silicon.md` Layer-1?

That rule prescribes a static `OLLAMA_KEEP_ALIVE=30s` — too blunt; it cold-starts the LLM
on every call. It was never applied (live check: env unset). The governor is the dynamic
replacement: keep models hot during use, free them only when something actually needs the
memory.

## Build order

### (a) Busy-state tracking in each sidecar  ← START HERE
- Wrap the inference critical section so the sidecar knows when it's actively working.
- Extend `/readyz` to report `state: idle|busy|cold`, `active_job` (or null), and
  `queue_depth`. Today `/readyz` only reports warm/cold + `idle_seconds`.
- **Fix the time-based idle-unload bug:** never unload while `state == busy` OR
  `queue_depth > 0`, regardless of the idle timer.
- Applies to: `omnivoice` (8770), `kokoro` (8765), `dia` (8769). ComfyUI (8188) exposes
  `/queue` and `/system_stats` — read those instead of adding a busy flag.

### (b) Governor / router + `make-room`
- A coordinator (lives where? — Mac-side script or a small FastAPI control plane) that:
  polls every model's state across both tenants; on `make-room <GB>` evicts `idle` models
  (sidecars via `/admin/unload`, Ollama via `ollama stop`) until headroom ≥ GB; refuses
  to touch `busy`.
- Queue semantics: a request that can't be served because the target is busy returns a
  structured "queued, position N" response the calling agent understands.

### (c) Human-gated `force`
- `force` flag on `make-room` / a request → preempt a busy job + evict. Requires an
  explicit human-approval gate (not a silent default).

### (d) Alerting pressure-watcher (NOT silent — it announces)
- Background poll of `memory_pressure` / `vm_stat` + Ollama `GET /api/ps` + sidecar `/readyz`.
- **Two thresholds, both broadcast to humans AND agents:**
  - `WARN` (~45 GB resident / cliff approaching) and `ALARM` (~55 GB / first swapout).
  - humans: desktop notification + webapp banner (+ optional sound).
  - agents: `GET /pressure` → `{level: ok|warn|alarm, free_gb, tenants:[...]}` they poll
    BEFORE dispatching heavy work; optionally an SSE stream.
- On `ALARM`, evict the lowest-priority `idle` model before macOS swaps. Never touches `busy`.
- **Context-aware:** Ollama's footprint grows with context length (KV cache). Poll `/api/ps`
  to catch the climb — don't trust a static size.
- **Big-model gating:** loading `qwen3-coder-next` (50 GB) or `deepseek-r1:70b` (42 GB) alone
  nears/exceeds the 55 GB cliff. The governor must `make-room` for the target's footprint
  BEFORE the load is issued, or refuse/queue it.

## Ollama observability — what Atelier can hook into

Ollama 0.24.0 (LAN `:11434`). What the governor can and can't see:

| Signal | Source | Available |
|---|---|---|
| Loaded models + memory (`size`/`size_vram`) + `context` + `expires_at` | `GET /api/ps` | ✅ rich, poll it |
| Context-driven footprint growth | `/api/ps` size reflects current alloc — poll to watch climb | ✅ |
| Model catalog + sizes | `GET /api/tags` | ✅ |
| Whole-machine pressure / swap onset | `memory_pressure`, `vm_stat` (Pageouts/Swapouts) | ✅ |
| Evict a model | `ollama stop <m>` / request `keep_alive:0` | ✅ |
| **In-flight request count / live tokens** | none — `/metrics` is 404 on 0.24.0 | ⚠️ **no API** |

**In-flight calls workaround:** tail `~/.ollama/logs/server.log` for request start/end
lines, and treat a freshly-bumped `expires_at` in `/api/ps` as "recently active." Good
enough to mark Ollama `busy` vs `idle`; not exact token-level telemetry.

**Catalog reality (drives the design):** several installed models exceed/near the cliff
on their own — `qwen3-coder-next` 50 GB, `deepseek-r1:70b` 42 GB, plus many 14–21 GB.
A single big LLM load is the most likely swap trigger, so big-model gating (above) is the
governor's primary job, not an edge case.

## Acceptance criteria

- A 10-min ComfyUI/Dia render is NEVER unloaded mid-job by the idle timer or the watcher.
- A second request for a busy sidecar is queued (not 502'd, not forced).
- `make-room 25` frees ≥25 GB by evicting only idle models, across both tenants.
- `force` only fires after explicit human approval.
- `atelier doctor` / `atelier-status` surfaces per-model `state` (idle/busy/cold).

## Open questions

- Where does the governor run? (Mac-side daemon vs the Linux gateway calling Mac APIs.)
- Queue scope: per-sidecar (single-flight already serializes) vs a global cross-media queue.
- How the githubawesome prep pipeline (the #77 concurrency issue) consumes the queue
  signal instead of firing N parallel TTS calls blind.
