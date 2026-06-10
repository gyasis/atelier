# LLM Admission Queue — a global, memory-aware gate across Ollama + mlxlm + llamacpp

**Status:** design / proposal
**Owner:** atelier hub
**Extends:** [`MEMORY_GOVERNOR.md`](./MEMORY_GOVERNOR.md) (acceptance criterion: *"a second request for a busy
sidecar is queued — not 502'd, not forced"*; open question: *"global cross-media queue vs per-sidecar"*)

---

## 1. The problem (observed)

When two LLM jobs run concurrently against **different** models, the second job **clears the
first** — the first model is unloaded mid-session. Audit findings:

- The "clearing" is **Ollama's internal LRU model swap**, not a hub bug. When a new model
  doesn't fit the unified-memory budget, Ollama evicts the least-recently-used one.
- It bites because **nothing in the hub gates requests.** Clients hit Ollama (`:11434`),
  mlxlm, and llamacpp **directly**. The governor (`:8799`) only *monitors and advises* — it
  polls `/api/ps` + each sidecar `/readyz`, raises WARN/ALARM, and exposes `make-room` /
  `force-stop`, but it sits **beside** the request path, not **in** it.
- Per-model config (`OLLAMA_CONTEXT_LENGTH=16384`, `OLLAMA_MAX_LOADED_MODELS=3`) lets two
  **medium** (~20 GB) models co-reside, but cannot make a 42–50 GB model share with anything
  big. Config alone is necessary, not sufficient.

### The unified-memory correction

mlxlm, llamacpp, and Ollama **share one ~69 GB pool** (~48 GB usable as Metal "VRAM").
Spreading models across backends does **not** add memory. Therefore the fix is **not** three
per-backend queues — it is **one global, memory-aware admission controller** that treats the
three backends as a single resource and packs jobs into the shared budget.

What this buys us:
- ✅ Genuine parallelism *within budget*: a 20 GB Ollama job + a 14 GB llamacpp job + a 7 GB
  mlxlm job (~41 GB) run **at once**, on different backends, with zero eviction.
- ⛔ A job that doesn't fit **waits in a queue** until room frees — instead of force-evicting a
  running model.

---

## 2. Goals / non-goals

**Goals**
- No running model is evicted by the *arrival* of another job. Eviction only happens to
  **idle** models, and only to make room for a **queued** job that is next to run.
- Maximize concurrency subject to a single global memory budget.
- Span all three LLM backends (Ollama, mlxlm, llamacpp). Audio sidecars (kokoro/whisper/dia/
  omnivoice) already single-flight; the gate is **memory-aware of them** but doesn't queue them.
- Reuse the governor's existing telemetry and `make-room` primitive — minimal new surface.

**Non-goals**
- Not a load balancer that picks the *fastest* backend. Routing is "which backend already has
  this model, else which backend can host it." Perf routing is a later layer.
- Not preemption of busy models — that stays the human-gated `force-stop` path (governor (c)).
- Not a fair-share scheduler across tenants. FIFO with a small-job bypass is enough for now.

---

## 3. Where the gate lives — two options

### Option A — `/admit` advisory call (cooperative, no proxy)

Governor gains a request-path API. Clients call it *before* hitting a backend:

```
POST /admit  { model, backend?, est_gb?, job_id }
  → 200 { grant: true,  lease_id, backend, ttl_s }        # go now
  → 200 { grant: false, position, eta_s, lease_id }       # hold; poll or long-poll
POST /release { lease_id }                                 # done / aborted
```

- **Pros:** no bytes proxied (LLM streams stay direct → zero added latency/memory on the hot
  path); incremental — adopt one client at a time; survives the gate being down (clients can
  fail-open).
- **Cons:** **cooperative** — a client that skips `/admit` bypasses the gate. Acceptable on a
  trusted LAN (matches the existing trust model in `force-stop`), and the governor already sees
  un-gated load via `/api/ps`, so it can still react.

### Option B — thin reverse proxy in front of the three backends

A single ingress (e.g. `:8790/llm`) that all clients use; it admits, then streams through to
the chosen backend.

- **Pros:** **enforced**, not cooperative; one URL; can rewrite `model`→backend transparently.
- **Cons:** proxies token streams (latency + memory + a new failure point); bigger lift; needs
  per-backend protocol shims (Ollama `/api/chat` vs mlxlm vs llamacpp differ).

**Recommendation: Option A first.** It's the smallest change that satisfies the acceptance
criterion, keeps LLM streams direct, and reuses the governor process. Promote to Option B later
*iff* cooperative bypass proves to be a real problem. The admission **logic** below is identical
for both; only the enforcement boundary differs.

---

## 4. The admission logic (backend-agnostic core)

### 4.1 State the gate tracks

```
budget_gb        = floor(metal_vram_budget) - headroom_gb     # e.g. 48 - 4 = 44
loaded[]         # from governor poll: {backend, model, gb, leases, last_used}
leases[]         # in-flight grants: {lease_id, backend, model, gb, started_at, ttl}
queue[]          # FIFO of waiting admits: {job_id, model, est_gb, enqueued_at}
```

`committed_gb = sum(distinct loaded model gb) + sum(reserved-but-not-yet-loaded grants)`.
Distinct because two leases on the **same** model share one resident copy.

### 4.2 Estimating a job's footprint (`est_gb`)

```
est_gb(model) = weights_gb(model)                     # from /api/tags or sidecar manifest
              + kv_gb(ctx_len, model)                 # ≈ ctx * n_layers * 2 * d_kv * dtype
```

Maintain a tiny static table for known models; fall back to `weights_gb * 1.2` when unknown.
Capping context (`OLLAMA_CONTEXT_LENGTH=16384`) is what keeps the kv term small — the table
keys on `(model, ctx_len)`.

### 4.3 Decision (on each `/admit` and on each `/release`)

```
def try_admit(job):
    # 1. Already loaded on some backend? Same-model concurrency is free up to NUM_PARALLEL.
    if model_loaded(job.model) and slots_free(job.model):
        return grant(existing_backend, reserve=0)        # no new memory

    need = est_gb(job)
    if committed_gb + need <= budget_gb:
        backend = pick_backend(job)                       # see 4.4
        return grant(backend, reserve=need)               # fits as-is → parallel

    # 2. Doesn't fit. Can we free enough from IDLE models only?
    freeable = sum(m.gb for m in loaded if m.leases == 0)
    if committed_gb - freeable + need <= budget_gb:
        evict_idle_lru_until(need)                         # governor make-room, idle-only
        backend = pick_backend(job)
        return grant(backend, reserve=need)

    # 3. Still doesn't fit → busy models are holding the memory. QUEUE, don't evict.
    enqueue(job)
    return hold(position=queue.index(job), eta=estimate_eta())
```

On `/release` (or lease TTL expiry / job_id vanishing from polls): drop the lease, then
**drain the queue head** through `try_admit` again. A small-job bypass may let a tiny job jump
a queue blocked on a huge one *iff* it fits in current headroom (configurable; off by default
to preserve FIFO fairness).

### 4.4 Backend routing (`pick_backend`)

1. If the model is **already loaded** on a backend → use it (free).
2. Else pick the backend that can host this model with the **least eviction** (most idle
   headroom). Ollama hosts GGUF; mlxlm hosts MLX; llamacpp hosts GGUF — so the model's format
   constrains the candidate set. Encode each backend's capability + current load from the
   existing `poll_sidecar` / `poll_ollama` data.

### 4.5 Eviction safety

Only **idle** models (`leases == 0` **and** not `_ollama_recently_active()` within the 15 s
window) are evictable, via the governor's existing `make-room` (Ollama `keep_alive=0`; sidecar
`/admin/unload` which already refuses busy). A **busy** model is never touched by admission —
preempting it remains the human-gated `force-stop`. This preserves the "never unloaded
mid-job" acceptance criterion.

---

## 5. Concurrency & correctness

- The gate runs in the governor's single asyncio event loop. `try_admit` / `release` /
  queue-drain must be **one critical section** — guard with a single `asyncio.Lock` so the
  fits-check and the reserve are atomic (today's governor globals are unlocked; this path needs
  the lock to avoid two admits both "fitting" into the same headroom). See the race notes in
  the audit: `_state`, `_recent_calls`, etc. are written by multiple tasks.
- **Lease TTL + reconciliation:** a client can die without `/release`. Every grant has a TTL;
  the existing poller reconciles `leases` against `/api/ps` + `/readyz` and reaps leases whose
  model is no longer resident / job no longer active. The governor already polls on
  `POLL_SECONDS` — extend that loop.
- **Fail-open:** if the gate is unreachable, Option-A clients proceed un-gated (today's
  behavior) — degrade, don't deadlock.

---

## 6. Phased plan

| Phase | Deliverable | Acceptance |
|---|---|---|
| 0 ✅ | Config: `OLLAMA_CONTEXT_LENGTH=16384`, `MAX_LOADED_MODELS=3`, persisted via `io.macstudio.ollama-env.plist` | Two ~20 GB models co-reside without eviction |
| 1 | `est_gb` table + `GET /budget` (read-only: budget, committed, loaded, freeable) | Numbers match `vm_stat` / `/api/ps` within tolerance |
| 2 | `POST /admit` + `/release`, in-memory leases, single lock, **no eviction** (queue when full) | Two different-model jobs: one runs, one queues; neither evicts the other |
| 3 | Idle-only eviction via `make-room` when queue head needs room | Queued job starts after an *idle* model is reclaimed; busy models untouched |
| 4 | Cross-backend routing (`pick_backend`) across Ollama/mlxlm/llamacpp | A GGUF job and an MLX job run in parallel under budget |
| 5 | Adopt in the real callers (the #77 prep pipeline that fires N parallel TTS/LLM calls) | Pipeline consumes grants instead of racing; no clears under load |
| 6 (opt) | Promote to enforced proxy (Option B) *iff* bypass is a problem | All LLM traffic gated |

---

## 7. Open questions

- **`est_gb` accuracy** for MLX vs GGUF KV math — start with measured constants per model, refine.
- **Long-poll vs poll** for held admits — long-poll is nicer for clients; poll is simpler. Start poll.
- **Small-job bypass** — fairness vs throughput. Default off; revisit with real traffic.
- **Same-model `NUM_PARALLEL`** — currently 1 (keeps KV small). Per-model override worth it?
- **Who owns `est_gb` for sidecars** — sidecars could report their own footprint in `/readyz`.
