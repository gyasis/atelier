"""
_common/lifecycle.py — the Atelier sidecar GOVERNANCE FRAMEWORK (the 3-law constitution).

Any sidecar hooks into the governor's management system by wrapping its model in a
`GovernedSidecar` and running inference inside `async with sc.job():`. It then inherits,
for free, the three constitutional laws:

  Law 1 — NO MEMORY LEAKS. On idle/admin-unload the framework drops the model, gc's, and
    empties the accelerator cache. On UNIFIED MEMORY (mps / mlx) `empty_cache()` cannot hand
    RSS back to the OS (the allocator keeps buffers mapped), so once resident memory exceeds
    a threshold it SELF-RESTARTS the process (launchd relaunches it cold) — the only reliable
    reclaim on Metal. CUDA returns memory via empty_cache (no restart). 'remote' holds none.

  Law 2 — NEVER UNLOAD A MODEL THAT'S ACTIVELY WORKING. "busy" is GROUND TRUTH: the in-flight
    counter AND the queue's held permits must agree. The idle-watcher never reaps while busy;
    a job wedged past a hang ceiling (a thread that can't be cancelled) is recovered by restart.

  Law 3 — QUEUE CALLS. A bounded semaphore admits up to `max_concurrency` (default 1 — one
    model on unified memory). Overflow callers WAIT in line; work is never dropped, and the
    machine is never driven over the memory cliff by fan-in. Queue depth is observable.

The GOVERNOR CONTRACT (the actual hook): standard GET /healthz, GET /readyz (rich, honest
observability — no black boxes), POST /admin/unload. That's exactly what the governor polls
and calls. Register a sidecar in the governor's SIDECAR_BASE and it is fully managed — memory
accounted, idle-evicted, force-stoppable — with zero bespoke lifecycle code.

Usage (see sidecars/colpali/server.py for the reference):
    from lifecycle import GovernedSidecar
    sc = GovernedSidecar("colpali", role="visual doc retrieval",
                         load_fn=_load, unload_fn=_unload, model_name="vidore/colpali-v1.3")
    app = FastAPI(lifespan=sc.lifespan()); sc.attach(app)
    @app.post("/score")
    async def score(req):
        async with sc.job() as h:              # queue slot + busy stamp + lazy load
            return await sc.run(h.model, ...)  # blocking model call off the event loop

Devices: 'mps' | 'cuda' | 'cpu' | 'mlx' | 'remote' (auto-detected from torch if not given).
"""
import asyncio
import gc
import inspect
import os
import subprocess
import time
from contextlib import asynccontextmanager


def _auto_device() -> str:
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
        if torch.backends.mps.is_available():
            return "mps"
    except Exception:
        pass
    return "cpu"


def self_rss_gb() -> float:
    """RSS of THIS process in GB (via ps) — the honest 'am I still holding memory' check that
    decides whether an MPS/MLX reclaim-restart is actually needed (vs already at cold baseline)."""
    try:
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(os.getpid())],
                             capture_output=True, text=True, timeout=3).stdout.strip()
        return round(int(out) / 1048576, 2) if out else 0.0
    except Exception:
        return 0.0


async def _maybe_async(fn, *a):
    r = fn(*a)
    return await r if inspect.isawaitable(r) else r


class GovernedSidecar:
    def __init__(self, name, *, load_fn, unload_fn=None, role="", model_name=None,
                 device=None, idle_unload_s=None, keep_warm=None,
                 reclaim_threshold_gb=None, max_gen_hang_s=None, max_concurrency=1,
                 hub_token_env="HUB_TOKEN"):
        U = name.upper()
        self.name = name
        self.role = role
        self._load_fn = load_fn
        self._unload_fn = unload_fn
        self.model_name = model_name or name
        self.device = device or os.environ.get(f"{U}_DEVICE") or _auto_device()
        self.idle_unload_s = int(idle_unload_s if idle_unload_s is not None
                                 else os.environ.get("IDLE_UNLOAD_SECONDS", 600))
        self.keep_warm = (keep_warm if keep_warm is not None
                          else os.environ.get("KEEP_WARM", "false").lower() in ("1", "true", "yes"))
        self.reclaim_threshold_gb = float(reclaim_threshold_gb if reclaim_threshold_gb is not None
                                          else os.environ.get("RECLAIM_THRESHOLD_GB", 2.0))
        # Reclaim is BASELINE-RELATIVE: restart when RSS exceeds the captured cold baseline by
        # this margin. A loaded model is always well above baseline, so this fires even for a
        # 1.4 GB model (an absolute 2 GB threshold silently leaked those). reclaim_threshold_gb
        # is only the fallback before the baseline is captured.
        self.reclaim_margin_gb = float(os.environ.get("RECLAIM_MARGIN_GB", "0.6"))
        self._cold_rss = None    # RSS with no model loaded, captured at lifespan startup
        self.max_gen_hang_s = float(max_gen_hang_s if max_gen_hang_s is not None
                                    else os.environ.get("MAX_GEN_HANG_S", 600))
        self.max_concurrency = int(os.environ.get(f"{U}_MAX_CONCURRENCY", max_concurrency))
        self.hub_token = os.environ.get(hub_token_env)
        # ---- lifecycle state ----
        self.model = None            # whatever load_fn returns (opaque handle; user accesses .model)
        self._warmed = False
        self._sem = asyncio.Semaphore(self.max_concurrency)   # Law 3: the queue/concurrency gate
        self._start_lock = asyncio.Lock()                     # serializes load/unload/swap
        self._active = 0             # in-flight jobs
        self._waiting = 0            # callers blocked on the queue
        self._starts: list[float] = []   # monotonic start of each in-flight job (for hang detection)
        self._last_request_at = time.monotonic()
        self._idle_unloaded_at = None
        self._idle_task = None

    # ================= Law 1: memory =================
    def _permits_free(self) -> int:
        return getattr(self._sem, "_value", self.max_concurrency)

    def _clear_cache(self):
        try:
            if self.device == "cuda":
                import torch; torch.cuda.empty_cache()
            elif self.device == "mps":
                import torch; torch.mps.synchronize(); torch.mps.empty_cache()
            elif self.device == "mlx":
                import mlx.core as mx
                fn = getattr(mx, "clear_cache", None) or getattr(getattr(mx, "metal", None), "clear_cache", None)
                if fn:
                    fn()
        except Exception as e:
            print(f"[{self.name}] clear_cache({self.device}) failed: {e}", flush=True)

    def _needs_restart_reclaim(self) -> bool:
        # Only unified memory (mps/mlx) can't return RSS via empty_cache; restart is the reclaim.
        if self.device not in ("mps", "mlx"):
            return False
        rss = self_rss_gb()
        if self._cold_rss is not None:          # baseline-relative (the correct signal)
            return rss > self._cold_rss + self.reclaim_margin_gb
        return rss > self.reclaim_threshold_gb  # fallback before baseline captured

    def _reclaim_via_restart(self, reason: str):
        print(f"[{self.name}] {reason}: RSS={self_rss_gb()}GB still mapped after unload — "
              f"self-restarting to return {self.device} memory to the OS (launchd relaunches cold)",
              flush=True)
        os._exit(42)   # non-zero → launchd KeepAlive{SuccessfulExit:false} relaunches cold

    async def _delayed_restart(self, delay: float, reason: str):
        await asyncio.sleep(delay)   # let an in-flight HTTP response flush first
        self._reclaim_via_restart(reason)

    async def _unload(self):
        """Drop the model + run unload_fn teardown + gc + clear accelerator cache. Caller must
        hold ALL queue permits (via _drain) so nothing is mid-inference."""
        had = self.model is not None
        if had and self._unload_fn:
            try:
                await _maybe_async(self._unload_fn, self.model)
            except Exception as e:
                print(f"[{self.name}] unload_fn error: {e}", flush=True)
        self.model = None
        self._warmed = False
        if had:
            self._idle_unloaded_at = time.monotonic()
        gc.collect()
        self._clear_cache()
        if had:
            print(f"[{self.name}] unloaded ({self.device})", flush=True)

    async def _drain(self):
        for _ in range(self.max_concurrency):
            await self._sem.acquire()

    def _undrain(self):
        for _ in range(self.max_concurrency):
            self._sem.release()

    # ================= load (lazy) =================
    async def ensure_loaded(self):
        if self.model is not None and self._warmed:
            return
        async with self._start_lock:
            if self.model is None or not self._warmed:
                print(f"[{self.name}] cold-loading {self.model_name} on {self.device}", flush=True)
                t0 = time.perf_counter()
                self.model = await _maybe_async(self._load_fn)
                self._warmed = True
                self._idle_unloaded_at = None
                print(f"[{self.name}] ready in {time.perf_counter()-t0:.1f}s", flush=True)

    # ================= Law 2: busy (ground truth) =================
    def is_busy(self) -> bool:
        """Both signals must agree: an in-flight counter > 0 AND a queue permit actually held.
        A lone _active>0 with all permits free is a desync/false signal — never blocks reclaim."""
        return self._active > 0 and self._permits_free() < self.max_concurrency

    def active_elapsed_s(self):
        return round(time.monotonic() - min(self._starts), 1) if self._starts else None

    # ================= Law 2 + 3: the guarded job =================
    @asynccontextmanager
    async def job(self):
        """Wrap one inference: wait for a queue slot (Law 3), mark active + stamp start (Law 2),
        ensure the model is loaded, yield the handle, then release. The idle-watcher/unload can
        never reap while this is open."""
        self._waiting += 1
        async with self._sem:
            self._waiting -= 1
            self._active += 1
            t0 = time.monotonic()
            self._starts.append(t0)
            try:
                await self.ensure_loaded()
                yield self.model
            finally:
                self._active -= 1
                try:
                    self._starts.remove(t0)
                except ValueError:
                    pass
                self._last_request_at = time.monotonic()

    async def run(self, fn, *a, **k):
        """Run a blocking model call in a worker thread so the event loop stays responsive."""
        return await asyncio.to_thread(lambda: fn(*a, **k))

    # ================= idle-watcher (enforces all 3) =================
    async def _idle_watcher(self):
        if self.keep_warm:
            print(f"[{self.name}] idle-watcher off (KEEP_WARM=true)", flush=True)
            return
        print(f"[{self.name}] idle-watcher active (unload after {self.idle_unload_s}s idle)", flush=True)
        while True:
            await asyncio.sleep(30)
            if self.model is None:
                continue
            # Law 2 safety net: a job wedged past the hang ceiling pins _active forever — recover.
            el = self.active_elapsed_s()
            if self._active > 0 and el is not None and el > self.max_gen_hang_s:
                print(f"[{self.name}] HUNG job {el}s > {self.max_gen_hang_s}s ceiling — "
                      f"{'force-restarting' if self.device in ('mps', 'mlx') else 'WARN (no auto-recover)'}",
                      flush=True)
                if self.device in ("mps", "mlx"):
                    self._reclaim_via_restart("hung-job")
                continue
            if (not self.is_busy() and self._waiting == 0
                    and time.monotonic() - self._last_request_at > self.idle_unload_s):
                await self._drain()               # take every slot → nothing can start mid-unload
                try:
                    if not self.is_busy():
                        await self._unload()
                        if self._needs_restart_reclaim():
                            self._reclaim_via_restart("idle-unload")
                finally:
                    self._undrain()

    # ================= FastAPI wiring (the governor contract) =================
    def lifespan(self):
        sc = self

        @asynccontextmanager
        async def _ls(app):
            if sc._cold_rss is None:
                sc._cold_rss = self_rss_gb()   # cold baseline (no model) — reclaim is relative to this
            if sc.keep_warm:
                try:
                    await sc.ensure_loaded()
                except Exception as e:
                    print(f"[{sc.name}] pre-warm failed: {e}", flush=True)
            sc._idle_task = asyncio.create_task(sc._idle_watcher())
            yield
            if sc._idle_task and not sc._idle_task.done():
                sc._idle_task.cancel()
        return _ls

    def check_auth(self, request):
        if self.hub_token:
            if request.headers.get("authorization", "") != f"Bearer {self.hub_token}":
                from fastapi import HTTPException
                raise HTTPException(401, "invalid bearer token")

    def readyz(self) -> dict:
        loaded = self._warmed and self.model is not None
        return {
            "ok": True, "service": self.name,
            "state": "warm" if loaded else "cold",
            "lifecycle": "cold" if not loaded else ("busy" if self.is_busy() else "idle"),
            "busy": self.is_busy(),
            "active_jobs": self._active,
            "active_elapsed_s": self.active_elapsed_s(),   # how long the oldest in-flight job has run
            "slots_used": self.max_concurrency - self._permits_free(),
            "max_concurrency": self.max_concurrency,
            "queue_depth": self._waiting,                   # callers waiting for a slot (Law 3)
            "model": self.model_name if loaded else None,
            "device": self.device,
            "rss_gb": self_rss_gb(),                        # real resident memory (Law 1 visibility)
            "cold_rss_gb": self._cold_rss,                  # baseline with no model (reclaim is relative to this)
            "reclaim_margin_gb": self.reclaim_margin_gb,
            "idle_seconds": round(time.monotonic() - self._last_request_at, 1),
            "idle_unload_seconds": self.idle_unload_s,
            "keep_warm": self.keep_warm,
            "last_unload_ago_s": (round(time.monotonic() - self._idle_unloaded_at, 1)
                                  if self._idle_unloaded_at else None),
        }

    async def admin_unload(self, request):
        """Free memory now. Refuses while genuinely busy unless ?force=true (governor's
        human-gated preempt). On MPS/MLX, if memory is still mapped after the logical unload,
        self-restart to actually return the RSS. Delays the exit so this response flushes."""
        self.check_auth(request)
        force = request.query_params.get("force", "").lower() in ("1", "true", "yes")
        if self.is_busy() and not force:
            return {"unloaded": False, "refused": "busy", "active_jobs": self._active,
                    "active_elapsed_s": self.active_elapsed_s()}
        if self._active > 0 and self._permits_free() >= self.max_concurrency:
            print(f"[{self.name}] STALE busy: _active={self._active} but no permit held — "
                  f"treating as idle (proceeding with unload)", flush=True)
        had = self.model is not None
        await self._drain()
        try:
            await self._unload()
        finally:
            self._undrain()
        reclaim, rss = "empty_cache", self_rss_gb()
        if self._needs_restart_reclaim():
            reclaim = "process-restart"
            asyncio.create_task(self._delayed_restart(0.6, "admin/unload"))
        return {"unloaded": had, "forced": force, "model": self.model_name,
                "device": self.device, "reclaim": reclaim, "rss_gb": rss}

    def attach(self, app):
        """Register the governor-contract endpoints on the sidecar's FastAPI app."""
        from fastapi import Request

        @app.get("/healthz")
        async def _healthz():
            return {"ok": True, "service": self.name, "engine": self.role or self.name,
                    "device": self.device, "framework": "governed-sidecar/1.0"}

        @app.get("/readyz")
        async def _readyz():
            return self.readyz()

        @app.post("/admin/unload")
        async def _admin_unload(request: Request):
            return await self.admin_unload(request)
