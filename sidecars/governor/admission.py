#!/usr/bin/env python3
"""LLM admission gate — a global, memory-aware queue across Ollama + mlxlm + llamacpp.

Design: docs/LLM_ADMISSION_QUEUE.md. This module is the backend-agnostic CORE — pure
state + decision logic, NO I/O. server.py injects observations (what Ollama has loaded,
current free memory) and drives eviction; the Gate only decides admit / queue.

The invariant: a job's ARRIVAL never evicts a running model. A job either
  - fits in the remaining budget            → granted, runs in parallel; or
  - fits only after reclaiming IDLE models   → granted after server idle-evicts; or
  - fits neither                             → QUEUED (FIFO), waits for room to free.
Busy models are never preempted here — that stays the human-gated /force-stop path.

All three LLM backends share ONE unified-memory pool, so the budget is global: spreading
models across backends does not add memory, it only adds parallelism WITHIN the budget.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field


@dataclass
class Lease:
    lease_id: str
    job_id: str
    backend: str
    model: str
    est_gb: float
    started_at: float
    ttl_s: float

    def expired(self, now: float) -> bool:
        return now - self.started_at > self.ttl_s


@dataclass
class Pending:
    job_id: str
    backend: str
    model: str
    est_gb: float
    enqueued_at: float
    note: str = ""


@dataclass
class Decision:
    grant: bool
    # grant=True
    lease_id: str = ""
    backend: str = ""
    base_url: str = ""            # where to send the request (resolved backend's URL)
    reserved_gb: float = 0.0
    ttl_s: float = 0.0
    reused: bool = False          # True → rode an already-loaded model, reserved 0 new GB
    routed: str = ""             # how the backend was chosen (Phase 4 auto-routing)
    # grant=False
    position: int = 0             # 0-based slot in the FIFO queue
    eta_s: float | None = None
    reason: str = ""
    needs_idle_evict: bool = False  # head-of-queue would fit if server reclaimed idle models


class Gate:
    """Memory-aware admission core. Thread-/task-safe via an internal asyncio.Lock —
    every mutating decision (fits-check + reserve) is one critical section so two
    concurrent admits can't both 'fit' into the same headroom."""

    def __init__(self, *, budget_gb: float, default_est_gb: float = 18.0,
                 num_parallel: int = 1, est_overrides: dict[str, float] | None = None,
                 default_ttl_s: float = 900.0, cliff_gb: float | None = None,
                 live_floor_gb: float = 4.0):
        self.budget_gb = float(budget_gb)
        self.default_est_gb = float(default_est_gb)
        self.num_parallel = max(1, int(num_parallel))
        self.default_ttl_s = float(default_ttl_s)
        # HARD crash backstop: never grant if measured memory says it's unsafe, regardless
        # of the gate's own est accounting (which can drift from reality). The user has
        # crashed the machine on RAM overload before — this is the real safety net.
        self.cliff_gb = float(cliff_gb) if cliff_gb is not None else None
        self.live_floor_gb = float(live_floor_gb)   # keep this much real free RAM in reserve
        self._live_resident_gb: float | None = None
        self._live_free_gb: float | None = None
        # substring → gb (matched case-insensitively against the model name)
        self.est_overrides = {k.lower(): float(v) for k, v in (est_overrides or {}).items()}
        self.leases: dict[str, Lease] = {}
        self.queue: list[Pending] = []
        self._tags_gb: dict[str, float] = {}   # model → weights GB (from Ollama /api/tags)
        self._untracked_gb: float = 0.0         # GB loaded outside the gate (bypass-honest)
        # Phase 4 auto-routing inputs (fed by server poller):
        self._catalog: dict[str, list[str]] = {}   # backend → model ids it can serve
        self._base: dict[str, str] = {}            # backend → base_url
        self._loaded: set[tuple[str, str]] = set()  # (backend, model) currently resident/warmed
        self._seq = 0
        self.lock = asyncio.Lock()

    # ---- Phase 4: backend catalogs / routing inputs (poller-fed, lock-free reads) ----
    def set_catalog(self, catalog: dict[str, list[str]], base: dict[str, str]) -> None:
        self._catalog = {b: list(m) for b, m in catalog.items()}
        self._base = dict(base)

    def set_loaded(self, loaded: set[tuple[str, str]]) -> None:
        self._loaded = set(loaded)

    @staticmethod
    def _serves(models: list[str], model: str) -> bool:
        ml = (model or "").lower()
        for m in models:
            mm = (m or "").lower()
            if ml == mm or ml in mm or mm in ml or mm.split("/")[-1] == ml:
                return True
        return False

    def resolve_backend(self, model: str, requested: str | None) -> tuple[str, str, str]:
        """Map (model, requested-backend) → (backend, base_url, how). If the caller named a
        backend, honor it. If they passed 'auto'/'' , pick a capable backend, preferring one
        that already has the model loaded (free memory) — Ollama preferred among ties since
        it serves its whole catalog."""
        req = (requested or "auto").lower()
        if req not in ("auto", ""):
            return req, self._base.get(req, ""), f"caller-specified {req}"
        cands = [b for b, models in self._catalog.items() if self._serves(models, model)]
        if not cands:
            return "ollama", self._base.get("ollama", ""), "auto→ollama (no catalog match; may pull)"
        for b in cands:                                   # prefer an already-loaded copy
            if any(bb == b and self._serves([mm], model) for bb, mm in self._loaded):
                return b, self._base.get(b, ""), f"auto→{b} (already loaded — free)"
        cands.sort(key=lambda b: 0 if b == "ollama" else 1)
        return cands[0], self._base.get(cands[0], ""), f"auto→{cands[0]} (capable)"

    def set_live(self, *, resident_gb: float | None, free_gb: float | None) -> None:
        """Latest measured memory from vm_stat — the hard backstop input."""
        self._live_resident_gb = resident_gb
        self._live_free_gb = free_gb

    def _live_blocks(self, est_gb: float) -> str:
        """Return a non-empty reason if MEASURED memory makes this grant unsafe, else ''.
        Two independent guards: (1) already over the cliff; (2) granting would leave less
        than live_floor_gb of real free RAM. A reused (0 GB) grant skips guard (2)."""
        if self.cliff_gb is not None and self._live_resident_gb is not None \
           and self._live_resident_gb >= self.cliff_gb:
            return f"measured resident {self._live_resident_gb}GB ≥ cliff {self.cliff_gb}GB"
        if est_gb > 0 and self._live_free_gb is not None \
           and est_gb > self._live_free_gb - self.live_floor_gb:
            return (f"measured free {self._live_free_gb}GB can't hold est {est_gb}GB "
                    f"+ {self.live_floor_gb}GB floor")
        return ""

    # ---- observation injection (called by server.py poller; cheap, no lock needed) ----
    def set_tags(self, model_to_gb: dict[str, float]) -> None:
        self._tags_gb = dict(model_to_gb)

    def set_untracked_gb(self, gb: float) -> None:
        """GB of models loaded in a backend that have NO active lease (someone bypassed
        the gate). Counted against the budget so the gate stays honest under bypass."""
        self._untracked_gb = max(0.0, float(gb))

    def untracked_from(self, loaded: list[dict]) -> float:
        """Given backends' currently-loaded models [{backend, model, gb}], sum the GB of
        those with NO matching active lease — i.e. load that bypassed the gate. Read-only
        snapshot; safe to call from the poller without the lock (single event loop)."""
        leased = {(L.backend, L.model) for L in self.leases.values()}
        return round(sum(x.get("gb", 0.0) for x in loaded
                         if (x.get("backend", "ollama"), x.get("model")) not in leased), 2)

    # ---- estimation ----
    def est_gb(self, model: str, hint: float | None = None) -> float:
        if hint and hint > 0:
            return float(hint)
        ml = (model or "").lower()
        for needle, gb in self.est_overrides.items():
            if needle in ml:
                return gb
        if model in self._tags_gb:
            return round(self._tags_gb[model] * 1.15, 2)   # weights + ~15% KV at capped ctx
        # try base name without a :tag
        base = model.split(":", 1)[0] if model else ""
        for known, gb in self._tags_gb.items():
            if known.split(":", 1)[0] == base:
                return round(gb * 1.15, 2)
        return self.default_est_gb

    # ---- budget accounting ----
    def _distinct_lease_gb(self, exclude_job: str | None = None) -> float:
        """Sum est_gb over DISTINCT (backend, model) leases — two leases on the same
        resident model share one copy, so they're counted once."""
        seen: dict[tuple[str, str], float] = {}
        for L in self.leases.values():
            if exclude_job and L.job_id == exclude_job:
                continue
            seen[(L.backend, L.model)] = L.est_gb
        return sum(seen.values())

    def committed_gb(self, exclude_job: str | None = None) -> float:
        return round(self._distinct_lease_gb(exclude_job) + self._untracked_gb, 2)

    def free_budget_gb(self, exclude_job: str | None = None) -> float:
        return round(self.budget_gb - self.committed_gb(exclude_job), 2)

    def _loaded_slots(self, backend: str, model: str) -> int:
        return sum(1 for L in self.leases.values()
                   if L.backend == backend and L.model == model)

    def _idle_reclaimable_gb(self) -> float:
        # The gate itself holds no "idle" leases (a lease == in-flight); idle headroom is
        # whatever the server reports as reclaimable. server.py sets it via untracked vs
        # actual; here we expose the budget-side view only. Reclaim is decided server-side.
        return 0.0

    # ---- the decision (must hold self.lock) ----
    def _decide(self, job_id: str, backend: str, model: str, est: float) -> Decision:
        now = time.time()

        # 0. Idempotent: this job already holds a lease → return it (heartbeat/renew).
        for L in self.leases.values():
            if L.job_id == job_id:
                L.started_at = now   # renew TTL on re-poll
                return Decision(grant=True, lease_id=L.lease_id, backend=L.backend,
                                base_url=self._base.get(L.backend, ""),
                                reserved_gb=0.0, ttl_s=L.ttl_s, reused=True,
                                reason="existing lease renewed")

        # Phase 4: resolve which backend will actually serve this (honors caller, else auto).
        backend, base_url, routed = self.resolve_backend(model, backend)
        e = self.est_gb(model, est)

        # 1. Same model already resident with a free parallel slot → free grant (0 GB).
        if self._loaded_slots(backend, model) > 0 and \
           self._loaded_slots(backend, model) < self.num_parallel:
            return self._grant(job_id, backend, model, est_gb=0.0, now=now,
                               reused=True, base_url=base_url, routed=routed,
                               note=f"shares loaded {model}")

        head = (not self.queue) or self.queue[0].job_id == job_id
        budget_fits = self.committed_gb() + e <= self.budget_gb
        live_block = self._live_blocks(e)        # HARD measured-memory backstop
        fits = budget_fits and not live_block

        # 2. Fits now (budget AND measured memory) AND not stuck behind queue → grant.
        if fits and head:
            self._dequeue(job_id)
            return self._grant(job_id, backend, model, est_gb=e, now=now, reused=False,
                               base_url=base_url, routed=routed)

        # 3. Doesn't fit (or blocked by queue) → ensure queued, report position.
        self._enqueue(job_id, backend, model, e, now)
        pos = self._position(job_id)
        if live_block:
            reason = f"held — measured-memory backstop: {live_block}"
        elif not budget_fits:
            reason = "insufficient free budget"
        else:
            reason = "queued behind earlier jobs"
        d = Decision(grant=False, position=pos, eta_s=self._eta(pos), reason=reason)
        # Reclaiming IDLE models is always safe (make-room refuses busy), so signal it
        # whenever the HEAD is held by memory — budget OR live pressure. If nothing is
        # idle, make-room no-ops and the job stays queued (waiting on a busy job).
        d.needs_idle_evict = head and (not budget_fits or bool(live_block))
        return d

    def _grant(self, job_id, backend, model, *, est_gb, now, reused,
               base_url="", routed="", note="") -> Decision:
        self._seq += 1
        lease_id = f"L{self._seq:06d}-{job_id[:8]}"
        self.leases[lease_id] = Lease(lease_id=lease_id, job_id=job_id, backend=backend,
                                      model=model, est_gb=est_gb, started_at=now,
                                      ttl_s=self.default_ttl_s)
        return Decision(grant=True, lease_id=lease_id, backend=backend, base_url=base_url,
                        reserved_gb=est_gb, ttl_s=self.default_ttl_s, reused=reused,
                        routed=routed, reason=note or "granted")

    # ---- queue bookkeeping ----
    def _position(self, job_id: str) -> int:
        for i, p in enumerate(self.queue):
            if p.job_id == job_id:
                return i
        return -1

    def _enqueue(self, job_id, backend, model, est, now):
        if self._position(job_id) >= 0:
            return
        self.queue.append(Pending(job_id=job_id, backend=backend, model=model,
                                  est_gb=est, enqueued_at=now))

    def _dequeue(self, job_id: str):
        self.queue = [p for p in self.queue if p.job_id != job_id]

    def _eta(self, position: int) -> float | None:
        # No reliable per-job remaining-time signal yet (Phase 1). Surface position only.
        return None

    # ---- public async API (server.py calls these) ----
    async def admit(self, job_id: str, model: str, backend: str = "ollama",
                    est_gb: float | None = None) -> Decision:
        async with self.lock:
            return self._decide(job_id, backend or "ollama", model, est_gb or 0.0)

    async def release(self, lease_id: str = "", job_id: str = "") -> dict:
        async with self.lock:
            before = len(self.leases)
            if lease_id and lease_id in self.leases:
                self.leases.pop(lease_id, None)
            elif job_id:
                for lid in [k for k, v in self.leases.items() if v.job_id == job_id]:
                    self.leases.pop(lid, None)
            else:
                return {"ok": False, "error": "lease_id or job_id required"}
            self._dequeue(job_id) if job_id else None
            return {"ok": True, "released": before - len(self.leases),
                    "active_leases": len(self.leases), "queue_depth": len(self.queue)}

    async def reap(self, now: float | None = None) -> dict:
        """Backstop for clients that died without /release: drop expired leases.
        Called from the server poller loop."""
        now = now or time.time()
        async with self.lock:
            dead = [lid for lid, L in self.leases.items() if L.expired(now)]
            for lid in dead:
                self.leases.pop(lid, None)
            # drop stale queue entries too (client gave up)
            self.queue = [p for p in self.queue if now - p.enqueued_at < self.default_ttl_s]
            return {"reaped_leases": len(dead), "active_leases": len(self.leases),
                    "queue_depth": len(self.queue)}

    def head_needs_idle_evict(self) -> Pending | None:
        """The queue head, IF idle eviction might unblock it (server decides reclaim)."""
        if not self.queue:
            return None
        head = self.queue[0]
        if self.committed_gb() + head.est_gb <= self.budget_gb:
            return None   # it actually fits now; client just needs to re-poll
        return head

    # ---- introspection (GET /budget) ----
    def snapshot(self) -> dict:
        now = time.time()
        return {
            "budget_gb": round(self.budget_gb, 2),
            "committed_gb": self.committed_gb(),
            "free_budget_gb": self.free_budget_gb(),
            "untracked_gb": round(self._untracked_gb, 2),
            "num_parallel": self.num_parallel,
            "active_leases": [
                {"lease_id": L.lease_id, "job_id": L.job_id, "backend": L.backend,
                 "model": L.model, "est_gb": L.est_gb, "age_s": round(now - L.started_at, 1),
                 "ttl_s": L.ttl_s}
                for L in sorted(self.leases.values(), key=lambda x: x.started_at)
            ],
            "queue": [
                {"position": i, "job_id": p.job_id, "backend": p.backend,
                 "model": p.model, "est_gb": p.est_gb, "wait_s": round(now - p.enqueued_at, 1)}
                for i, p in enumerate(self.queue)
            ],
        }
