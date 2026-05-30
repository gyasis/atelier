# Atelier — Constitution

Durable principles that govern every design decision in this project. New work that
violates one of these is wrong by default, even if it "works."

---

## I. Everything is observable — no black boxes

**Every model, every call, every memory event, and every state transition MUST be
queryable from outside the process.** Nothing in Atelier is allowed to hold state you
can't inspect.

Concretely, this means:

- **Every sidecar** exposes `/healthz` (alive?) + `/readyz` (loaded? **and what is it
  doing** — `lifecycle: idle|busy|cold`, `active_jobs`, `queue_depth`). "Loaded" is never
  enough; a model's *activity* must be visible.
- **The memory governor** exposes `GET /pressure` — the live cross-tenant picture (free
  memory, per-model state, warn/alarm level) — and **announces** WARN/ALARM to humans
  AND agents. The watcher is never silent.
- **Ollama** (a peer tenant, not a black box) is observed via `/api/ps` + structured
  parsing of `~/.ollama/logs/server.log` (calls, token cost, KV-cache size, load/evict
  events, the spill signal).
- **The fleet** is observable in aggregate: `atelier-status` (live health) and
  `deploy/doctor.sh` (codified ⇄ loaded ⇄ healthy) — a model that exists but isn't
  surfaced is a bug (this is the gap that killed OmniVoice on 2026-05-25).

> If you can't see what it's doing, it doesn't ship. No black boxes.

## II. Memory-economy — idle models free their memory

The Mac Studio's 64 GB unified memory is the scarce resource and the swap cliff (~55 GB)
is silent and catastrophic. Therefore:

- The **server** stays up; the **model** unloads after idle. Nothing hoards memory it
  isn't using.
- Idle-unload is **job-aware** — it never reaps a model that's `busy` or has queued work
  (Principle I makes "busy" visible so this is enforceable).
- Eviction is **coordinated** across both tenants (Ollama + sidecars), never blind.

## III. Reproducible from zero

`deploy/install.sh` reconstructs the whole hub on a fresh Mac; `deploy/doctor.sh` proves
it's whole. No model, service, or config lives outside the repo's knowledge. If it isn't
codified here, it doesn't survive a reboot.

---

These principles are why the memory governor is built monitor-first (Principle I before
any eviction) and why busy-state tracking (Principle I) is the foundation the whole
governor stands on. See `docs/MEMORY_GOVERNOR.md`.
