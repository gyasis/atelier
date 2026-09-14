# The Atelier Governed-Sidecar Framework (the memory constitution)

> Status: shipped 2026-07-05/06. This is **the** Atelier framework — the governor plus the shared
> sidecar lifecycle that every memory-holding tenant on the Mac Studio obeys. Companion to
> `SIDECAR_PATTERN.md` (the older by-hand pattern this formalizes) and `MEMORY_GOVERNOR.md`.

## The 3 laws (the constitution)

Every tenant that holds Studio GPU/unified memory obeys:

1. **No memory leaks** — a tenant returns its memory to the OS when unloaded.
2. **Never unload a model that's actively working** — "busy" is ground truth (an in-flight
   counter AND the queue lock must agree); a real job is never interrupted.
3. **Queue calls under pressure** — overflow callers WAIT; work is never dropped and the box is
   never driven over the memory cliff by fan-in.

Enforced two ways: **from inside** each sidecar (the `GovernedSidecar` framework), and **from
outside** by the governor (make-room respects the warm tag; auto-heal restarts a leaker).

## Cold-hard fact this is built on

On **Apple unified memory (Metal)**, `torch.mps.empty_cache()` / `mx.clear_cache()` **do NOT return
RSS to the OS** — the allocator keeps freed buffers mapped in-process. So a "logical unload" leaves
gigabytes resident. The ONLY reliable reclaim is to **exit the process and let launchd relaunch it
cold** (non-zero exit → `KeepAlive{SuccessfulExit:false}` → restart). CUDA returns memory via
`empty_cache` (no restart). A **child-process** wrapper reclaims by killing the child (no restart of
the wrapper needed). This is why the tenant CLASS matters (below).

## Tenant classes (how each satisfies Law 1)

| Class | Reclaim mechanism | Sidecars |
|---|---|---|
| **ollama** (native) | `keep_alive` auto-unload returns memory | ollama :11434 |
| **child-process wrapper** | kill the child → OS reclaims all of it | **llamacpp** (:8771, multi-model), **mlxlm** (:8773) |
| **in-process, unified memory** | `empty_cache` + **self-restart** when RSS > baseline+margin | **omnivoice**, **colpali**, **medner**, **dia**, **whisper** |
| **in-process, non-unified** | `gc` / `empty_cache` suffices (CUDA returns; CoreML tiny) | **kokoro** (CoreML) |
| **remote / Modal** | no local GPU memory | maisi, radiogen (Law 1 N/A) |

## `GovernedSidecar` — how a NEW sidecar hooks in

Canonical framework: **`sidecars/_common/lifecycle.py`**. A sidecar provides `load`/`unload`/work
callbacks and inherits all 3 laws + the governor contract. Reference impl: `sidecars/colpali/server.py`.

```python
from lifecycle import GovernedSidecar
from types import SimpleNamespace

def _load():                       # cold-load; return an opaque handle (bundle multiple objects)
    return SimpleNamespace(model=..., processor=...)

sc = GovernedSidecar("mysidecar", role="what it does", load_fn=_load,
                     model_name="repo/id", idle_unload_s=600)   # device auto-detected

app = FastAPI(lifespan=sc.lifespan())
sc.attach(app)                     # registers GET /healthz /readyz + POST /admin/unload

@app.post("/work")
async def work(request):
    sc.check_auth(request)
    async with sc.job() as h:      # Law 3 (queue slot) + Law 2 (busy stamp) + lazy load
        return await sc.run(lambda: h.model.do(...))   # blocking call off the event loop
```

**Deployment note (the tax):** the framework is imported flat (`from lifecycle import GovernedSidecar`),
so `lifecycle.py` is **copied beside each sidecar's `server.py`** (runtime dir + repo dir). Canonical
is `sidecars/_common/lifecycle.py`; **updating the framework means re-copying it to every sidecar** and
restarting them. (A `pip install -e` shared package would remove this — worth doing if the fleet grows.)

### Reclaim is BASELINE-RELATIVE (critical)
The framework captures the process's **cold RSS at startup** and restart-reclaims when
`RSS > cold_rss + reclaim_margin_gb` (default margin 0.6 GB). An earlier **absolute 2 GB threshold
silently leaked sub-2 GB models** (colpali 1.2 GB, whisper 1.7 GB stayed mapped). Env overrides:
`RECLAIM_MARGIN_GB`, `RECLAIM_THRESHOLD_GB` (fallback), per-sidecar `<NAME>_MAX_CONCURRENCY`,
`<NAME>_DEVICE`, `IDLE_UNLOAD_SECONDS`, `KEEP_WARM`, `MAX_GEN_HANG_S`.

### Ground-truth busy + hung recovery (Law 2)
`is_busy() = _active > 0 AND a queue permit is held`. `/readyz` exposes `active_elapsed_s` +
`slots_used` so "busy" is never opaque (a real job shows a small climbing clock; a stuck one shows
absurd elapsed). A job exceeding `MAX_GEN_HANG_S` (default 600 s) is a wedged thread → restart to recover.

## The WARM TAG (allowed to stay resident)
Set **`KEEP_WARM=true`** in a sidecar's plist → it does NOT idle-unload and reports `keep_warm:true`
in `/readyz`. The governor's **make-room evicts non-warm idle sidecars FIRST** and **preserves
warm-tagged ones unless the box is over the cliff**. Use for latency-critical models (e.g. whisper for
chiron audio ingest — currently tagged warm; kokoro also warm).

## AUTO-HEAL (Law 1 enforced from OUTSIDE)
The governor poller runs `_autoheal_check`: if a sidecar reports **`state=cold` but still holds
> `AUTOHEAL_FLOOR_GB` (1.5) for > `AUTOHEAL_GRACE_S` (150 s)**, it leaked → the governor hard-restarts
it via `launchctl kickstart -k`. Self-healing (the framework restart-reclaim) fixes it first; this is
the backstop so even a bespoke / future non-compliant sidecar cannot hold leaked memory. Events surface
in `/pressure.autoheal`. Disable with `ATELIER_AUTOHEAL=0`.

## Multi-model llamacpp lane (:8771)
`llamacpp` is now a governed **llama-swap**: a registry (`~/.atelier/llamacpp-models.json`) + it swaps
the `llama-server` child to whichever alias the request's `model` names (one resident at a time).
`GET /models` = the menu. Hosts `gemma4-12b` (default) + `agents-a1-{q4,iq2,q8}` (InternScience 35B MoE
`qwen35moe`; a REASONING model — give ≥150 `max_tokens` or the answer is empty). Call via the governor:
`POST :8799/llm/llamacpp/v1/chat/completions {model:"agents-a1-q4", ...}`.

## Governor `/pressure` — the observability contract (what the dashboard reads)
Top-level: `resident_gb`, `free_gb`, `baseline_gb` (non-LLM floor), `committed_gb` (attributed LLM),
`budget_gb`, `level`, `top_procs`, **`constitution`** {laws, autoheal{enabled,floor_gb,grace_s},
cliff_gb, warn_gb}, **`autoheal`** (recent restarts). Per `tenants[]` (atelier): `state`, `mem_gb`
(measured subtree RSS, now including cold sidecars so leaks are visible), `active_jobs`,
`active_elapsed_s`, `queue_depth`, **`keep_warm`**, **`device`**, **`model`**, **`available_models`**,
**`cold_rss_gb`**, **`governed`**.

## Verify
```
curl -s :8799/pressure | python3 -m json.tool          # constitution + per-tenant fields
curl -s :8771/readyz                                   # framework contract (busy/queue/rss/cold_rss/keep_warm)
curl -s -X POST :8779/admin/unload                     # → {reclaim: empty_cache|process-restart, rss_gb}
curl -s -X POST :8799/make-room -d '{"dry_run":true}'  # warm-tagged preserved below cliff
```
