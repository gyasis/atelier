#!/usr/bin/env python3
"""Atelier admission client — wrap any LLM/TTS backend call with a memory lease.

The governor (:8799) runs a global, memory-aware admission gate across Ollama + mlxlm +
llamacpp (docs/LLM_ADMISSION_QUEUE.md). Callers acquire a lease BEFORE hitting a backend
and release it after, so concurrent jobs pack into one shared memory budget and the overflow
queues instead of force-evicting a running model (or crashing the box).

Async usage (the common case):

    from atelier_admit import admission
    async with admission(model="qwen3:32b", backend="ollama", est_gb=20) as lease:
        # granted — if the budget was full this waited in the governor's queue first
        resp = await client.post("http://127.0.0.1:11434/api/chat", json=...)
    # lease released automatically (even on exception)

    if not lease.granted:
        ...  # fail-open path: governor unreachable or wait timed out — ran ungated

Low-level (non-context) usage:

    lease = await acquire(model=..., backend=...)
    try: ...
    finally: await release(lease)

Design choices:
  - COOPERATIVE: this is the only thing tying a caller to the gate. A caller that skips it
    bypasses admission (the governor still SEES the load via /api/ps and counts it as
    `untracked_gb`, so the budget stays honest — it just can't queue an un-gated caller).
  - FAIL-OPEN: if the governor is down or the wait exceeds max_wait_s, proceed WITHOUT a
    lease rather than block the caller forever. lease.granted tells you which happened.
  - Re-polls with the SAME job_id, which both checks position and renews the lease TTL.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import time
import uuid
from dataclasses import dataclass

import httpx

GOVERNOR_URL = os.environ.get("ATELIER_GOVERNOR_URL", "http://127.0.0.1:8799")


@dataclass
class Lease:
    job_id: str
    backend: str
    granted: bool          # True → gate granted; False → fail-open (ran ungated)
    lease_id: str = ""
    reason: str = ""
    waited_s: float = 0.0


async def acquire(*, model: str, backend: str = "ollama", est_gb: float = 0.0,
                  job_id: str | None = None, poll_interval: float = 1.0,
                  max_wait_s: float = 600.0, fail_open: bool = True,
                  governor_url: str = GOVERNOR_URL) -> Lease:
    """Block (cooperatively) until the gate grants a lease, the wait times out, or the
    governor is unreachable. With fail_open=True the last two yield a non-granted Lease
    so the caller proceeds ungated; with fail_open=False they raise."""
    job_id = job_id or f"{backend}-{model}-{uuid.uuid4().hex[:8]}"
    body = {"job_id": job_id, "model": model, "backend": backend, "est_gb": est_gb}
    start = time.time()
    async with httpx.AsyncClient(timeout=10) as client:
        while True:
            try:
                r = await client.post(f"{governor_url}/admit", json=body)
                r.raise_for_status()
                d = r.json()
            except Exception as e:
                if fail_open:
                    return Lease(job_id, backend, False, reason=f"fail-open (governor unreachable: {e})",
                                 waited_s=round(time.time() - start, 2))
                raise
            if d.get("grant"):
                return Lease(job_id, d.get("backend", backend), True, lease_id=d.get("lease_id", ""),
                             reason=d.get("reason", "granted"), waited_s=round(time.time() - start, 2))
            if time.time() - start > max_wait_s:
                if fail_open:
                    return Lease(job_id, backend, False,
                                 reason=f"fail-open (waited {max_wait_s}s, still {d.get('reason','queued')})",
                                 waited_s=round(time.time() - start, 2))
                raise TimeoutError(f"admission wait exceeded {max_wait_s}s: {d.get('reason')}")
            await asyncio.sleep(poll_interval)


async def release(lease: Lease, *, governor_url: str = GOVERNOR_URL) -> None:
    """Release a granted lease. No-op for a fail-open (ungated) lease. Never raises —
    a failed release is backstopped by the governor's TTL reaper."""
    if not lease.granted or not lease.lease_id:
        return
    with contextlib.suppress(Exception):
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(f"{governor_url}/release",
                              json={"lease_id": lease.lease_id, "job_id": lease.job_id})


@contextlib.asynccontextmanager
async def admission(*, model: str, backend: str = "ollama", est_gb: float = 0.0,
                    job_id: str | None = None, poll_interval: float = 1.0,
                    max_wait_s: float = 600.0, fail_open: bool = True,
                    governor_url: str = GOVERNOR_URL):
    """Async context manager: acquire on enter, release on exit (even on exception)."""
    lease = await acquire(model=model, backend=backend, est_gb=est_gb, job_id=job_id,
                          poll_interval=poll_interval, max_wait_s=max_wait_s,
                          fail_open=fail_open, governor_url=governor_url)
    try:
        yield lease
    finally:
        await release(lease, governor_url=governor_url)
