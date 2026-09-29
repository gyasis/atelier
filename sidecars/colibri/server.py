"""
colibri-sidecar — governed wrapper around Colibrì (`coli serve`), built for OVERNIGHT work.

Colibrì runs very large MoE models on this box by streaming experts from the SSD. It is
SLOW (a fraction of a token/s to a few tok/s), so this sidecar is designed around one
assumption: nobody is waiting on the answer. Two ways in:

    SYNC   ANY /v1/{path}   OpenAI-compatible proxy to the child (chat/completions,
                            completions, brio, systemone, messages, models). No wall-clock
                            timeout. Prefer calling it THROUGH the governor:
                            POST :8799/llm/colibri/v1/chat/completions
    ASYNC  POST /jobs       submit {path, body}; returns a job_id immediately. The job is
                            persisted to disk BEFORE it runs, runs serially, takes and
                            RENEWS a governor lease for its whole duration, and writes its
                            result to disk. The caller can disconnect, and a sidecar restart
                            keeps queued jobs. GET /jobs/{id} to collect.

Why not a timeout: a long generation that is still working must not be killed. Liveness is
reported instead — the child's CPU seconds and the running job's elapsed time are in /readyz
and /jobs/{id}, so "stuck" and "slow" can be told apart by watching whether CPU advances.

Process model:  uvicorn(:8783) → `coli serve` gateway(:18783) → engine binary (e.g. qwen36).
The gateway spawns the engine as ITS OWN child, so stopping only the gateway would orphan a
multi-GB engine (upstream documents "ghost engines" that OOM'd a box). The child is therefore
started in its own session and stopped with killpg; stop verifies the group is gone.
The governor measures memory by walking this process's subtree (--port 8783 in the cmdline),
which still includes the engine because setsid does not change the parent pid.

Registry (~/.atelier/colibri-models.json, env COLIBRI_MODELS_CONFIG):
    {"default": "colibri-qwen36",
     "models": {"colibri-qwen36": {"path": "~/models/colibri/qwen36_i4_gs64",
                                   "ram": 26, "args": ["--kv-slots", "1"]}}}
`ram` is Colibrì's RAM budget in GB and doubles as the governor admission estimate.

Endpoints: GET /healthz /readyz /agent /models  POST /admin/unload
           POST /jobs  GET /jobs  GET /jobs/{id}  ANY /v1/{path}

Env: COLIBRI_PORT (8783) COLIBRI_CHILD_PORT (18783) COLIBRI_HOME (colibri checkout)
     COLIBRI_START_TIMEOUT (900 — cold load reads the whole dense set from disk)
     COLIBRI_MAX_QUEUE (8) IDLE_UNLOAD_SECONDS (600) KEEP_WARM (false)
     GOVERNOR_URL (http://127.0.0.1:8799) COLIBRI_JOBS_DIR (~/.atelier/colibri-jobs)
     COLIBRI_ADMIT_MAX_WAIT_S (0 = wait forever for admission) COLIBRI_LEASE_RENEW_S (120)
     HUB_TOKEN (optional bearer)
"""

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

PORT = int(os.environ.get("COLIBRI_PORT", "8783"))
CHILD_PORT = int(os.environ.get("COLIBRI_CHILD_PORT", "18783"))
CHILD_BASE = f"http://127.0.0.1:{CHILD_PORT}"
COLIBRI_HOME = Path(os.environ.get("COLIBRI_HOME",
                                   str(Path.home() / "services/colibri-sidecar/colibri"))).expanduser()
COLI = COLIBRI_HOME / "c" / "coli"
START_TIMEOUT = float(os.environ.get("COLIBRI_START_TIMEOUT", "900"))
MAX_QUEUE = int(os.environ.get("COLIBRI_MAX_QUEUE", "8"))
IDLE_UNLOAD_SECONDS = int(os.environ.get("IDLE_UNLOAD_SECONDS", "600"))
KEEP_WARM = os.environ.get("KEEP_WARM", "false").lower() in ("1", "true", "yes")
IDLE_TICK_SECONDS = 30
GOVERNOR_URL = os.environ.get("GOVERNOR_URL", "http://127.0.0.1:8799")
JOBS_DIR = Path(os.environ.get("COLIBRI_JOBS_DIR",
                               str(Path.home() / ".atelier/colibri-jobs"))).expanduser()
ADMIT_MAX_WAIT_S = float(os.environ.get("COLIBRI_ADMIT_MAX_WAIT_S", "0"))
LEASE_RENEW_S = float(os.environ.get("COLIBRI_LEASE_RENEW_S", "120"))
# Overnight work YIELDS to interactive work. The governor queue is FIFO, so a 26 GB job parked at
# its head blocks every small call behind it (measured 2026-09-28: an ollama call waited 2+ min
# behind a queued colibri job). So a job asks once; if not granted it withdraws from the governor
# queue (/release) and asks again after POLITE_RETRY_S. It never sits in the governor's line.
POLITE_RETRY_S = float(os.environ.get("COLIBRI_POLITE_RETRY_S", "30"))
HUB_TOKEN = os.environ.get("HUB_TOKEN")
MODELS_CONFIG = Path(os.environ.get("COLIBRI_MODELS_CONFIG",
                                    str(Path.home() / ".atelier/colibri-models.json"))).expanduser()
# Short connect, unbounded read: a dead child fails fast, a slow generation never times out.
CHILD_TIMEOUT = httpx.Timeout(connect=5.0, read=None, write=60.0, pool=None)
JOB_PATHS = ("v1/chat/completions", "v1/completions", "v1/brio", "v1/systemone", "v1/messages")


# ---------- registry ----------
def _load_registry() -> tuple[dict, str | None]:
    reg: dict[str, dict] = {}
    default = None
    if MODELS_CONFIG.is_file():
        try:
            d = json.loads(MODELS_CONFIG.read_text())
            for alias, spec in (d.get("models") or {}).items():
                args = spec.get("args") or []
                reg[alias] = {"path": str(Path(spec["path"]).expanduser()),
                              "ram": float(spec.get("ram", 24)),
                              "args": args.split() if isinstance(args, str) else list(args),
                              # per-model engine env, e.g. {"COLI_TOOL_FALLBACK": "1"}
                              "env": {str(k): str(v) for k, v in (spec.get("env") or {}).items()},
                              # request fields filled in when a client does not set them. The
                              # engine's own --no-think is not honoured by every family (qwen38
                              # measured 2026-09-29: flag ignored, enable_thinking=false works).
                              "request_defaults": dict(spec.get("request_defaults") or {})}
            default = d.get("default")
        except Exception as e:
            print(f"[colibri] registry config error ({MODELS_CONFIG}): {e}", flush=True)
    if default not in reg:
        default = next(iter(reg), None)
    return reg, default


REGISTRY, DEFAULT_ALIAS = _load_registry()

_proc: subprocess.Popen | None = None
_current_alias: str | None = None
_warmed = False
_start_lock = asyncio.Lock()
_last_request_at = time.monotonic()
_idle_unloaded_at: float | None = None
_active = 0                                  # in-flight sync + async requests on the child
_started_at: float | None = None
_last_start_error: str | None = None
_reaped_at_boot: list[int] = []
# The child runs in its own session (so killpg reaches the engine), which also puts it OUTSIDE
# launchd's cleanup: if this process dies without running shutdown, the group is orphaned and
# keeps the child port + its RAM. Its pgid is recorded here so the next boot can reap it.
PGID_FILE = JOBS_DIR.parent / "colibri-child.pgid"


def _child_alive() -> bool:
    return _proc is not None and _proc.poll() is None


def _group_pids(pgid: int) -> list[int]:
    """Every live pid in the child's process group (gateway + engine)."""
    try:
        out = subprocess.run(["ps", "-axo", "pid=,pgid="], capture_output=True, text=True,
                             timeout=4).stdout
    except Exception:
        return []
    pids = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == str(pgid):
            pids.append(int(parts[0]))
    return pids


def _child_cpu_seconds() -> float | None:
    """Summed CPU time of the child's process group — the liveness signal. If this keeps
    rising, a long job is WORKING; if it is flat while a job runs, it is stuck."""
    if not _child_alive():
        return None
    pids = _group_pids(_proc.pid)
    if not pids:
        return None
    try:
        out = subprocess.run(["ps", "-o", "time=", "-p", ",".join(map(str, pids))],
                             capture_output=True, text=True, timeout=4).stdout
    except Exception:
        return None
    total = 0.0
    for t in out.split():
        # formats: [[dd-]hh:]mm:ss.xx
        days = 0
        if "-" in t:
            d, t = t.split("-", 1)
            days = int(d)
        parts = [float(p) for p in t.split(":")]
        secs = 0.0
        for p in parts:
            secs = secs * 60 + p
        total += secs + days * 86400
    return round(total, 1)


async def _child_healthy(client: httpx.AsyncClient) -> bool:
    try:
        r = await client.get(f"{CHILD_BASE}/health", timeout=3)
        return r.status_code == 200 and r.json().get("status") == "ok"
    except Exception:
        return False


async def _start_child(alias: str) -> None:
    """Start `coli serve` for alias (stopping a different model first). Holds _start_lock."""
    global _proc, _current_alias, _warmed, _idle_unloaded_at, _started_at, _last_start_error
    if _child_alive() and _warmed and _current_alias == alias:
        return
    spec = REGISTRY.get(alias)
    if not spec:
        raise HTTPException(404, f"unknown model '{alias}' (available: {sorted(REGISTRY)})")
    if not Path(spec["path"], "config.json").is_file():
        raise HTTPException(503, f"model container not found for '{alias}': {spec['path']}")
    if not COLI.is_file():
        raise HTTPException(503, f"coli launcher not found at {COLI} (set COLIBRI_HOME)")
    if _child_alive():
        await _stop_child()
    cmd = [sys.executable, str(COLI), "serve", "--model", spec["path"],
           "--host", "127.0.0.1", "--port", str(CHILD_PORT), "--model-id", alias,
           "--ram", str(max(1, int(round(spec["ram"])))), "--max-queue", str(MAX_QUEUE), *spec["args"]]
    # --ram is an integer GB for coli; the registry keeps a float for the governor estimate.
    print(f"[colibri] starting child ({alias}): {' '.join(cmd)}", flush=True)
    t0 = time.monotonic()
    # start_new_session → own process group, so _stop_child can take the engine down too.
    env = {**os.environ, **spec.get("env", {})}
    if spec.get("env"):
        print(f"[colibri] child env for {alias}: {spec['env']}", flush=True)
    _proc = subprocess.Popen(cmd, cwd=str(COLIBRI_HOME / "c"), start_new_session=True, env=env)
    PGID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PGID_FILE.write_text(str(_proc.pid))
    _current_alias, _started_at, _last_start_error = alias, time.time(), None
    async with httpx.AsyncClient() as client:
        deadline = time.monotonic() + START_TIMEOUT
        while time.monotonic() < deadline:
            if not _child_alive():
                rc = _proc.returncode
                _proc, _current_alias = None, None
                _last_start_error = f"coli serve exited rc={rc} during startup of '{alias}'"
                raise HTTPException(503, _last_start_error + " (see colibri-sidecar logs)")
            if await _child_healthy(client):
                _warmed, _idle_unloaded_at = True, None
                print(f"[colibri] child ready in {time.monotonic()-t0:.1f}s ({alias})", flush=True)
                return
            await asyncio.sleep(2)
    _last_start_error = f"'{alias}' not healthy within {START_TIMEOUT:.0f}s"
    await _stop_child()
    raise HTTPException(503, _last_start_error)


async def _stop_child() -> None:
    """SIGTERM the whole process group, wait, SIGKILL survivors, and verify it is empty."""
    global _proc, _current_alias, _warmed, _idle_unloaded_at, _started_at
    if _proc is None:
        return
    pgid = _proc.pid
    print(f"[colibri] stopping child group {pgid} ({_current_alias})", flush=True)
    try:
        os.killpg(pgid, signal.SIGTERM)
        os.killpg(pgid, signal.SIGCONT)        # a stopped process cannot act on SIGTERM
    except ProcessLookupError:
        pass
    for _ in range(20):                        # up to 10 s for a clean exit (launchd's own
                                               # ExitTimeOut is 20 s; leave room for SIGKILL)
        if not _group_pids(pgid):
            break
        await asyncio.sleep(0.5)
    left = _group_pids(pgid)
    if left:
        print(f"[colibri] group {pgid} survived SIGTERM {left} — SIGKILL", flush=True)
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await asyncio.sleep(1)
        left = _group_pids(pgid)
        if left:
            print(f"[colibri] ▲ group {pgid} STILL has {left} after SIGKILL", flush=True)
    try:
        _proc.wait(timeout=1)
    except Exception:
        pass
    _proc, _current_alias, _warmed, _started_at = None, None, False, None
    _idle_unloaded_at = time.monotonic()
    if not left:
        PGID_FILE.unlink(missing_ok=True)


def _apply_request_defaults(alias: str, body: dict) -> dict:
    """Fill the model's request_defaults into body without overriding anything the client set."""
    for k, v in REGISTRY.get(alias, {}).get("request_defaults", {}).items():
        body.setdefault(k, v)
    return body


def _resolve_alias(requested: str | None) -> str:
    if requested and requested not in ("", "?"):
        if requested in REGISTRY:
            return requested
        raise HTTPException(404, f"unknown model '{requested}' (available: {sorted(REGISTRY)})")
    alias = _current_alias or DEFAULT_ALIAS
    if not alias:
        raise HTTPException(503, f"no models registered (write {MODELS_CONFIG})")
    return alias


async def _ensure_started(alias: str) -> None:
    if _child_alive() and _warmed and _current_alias == alias:
        return
    async with _start_lock:
        if not (_child_alive() and _warmed and _current_alias == alias):
            if _child_alive() and _current_alias != alias and _active > 0:
                raise HTTPException(409, f"busy serving '{_current_alias}' ({_active} active)")
            await _start_child(alias)


# ---------- async overnight jobs (persisted) ----------
_job_queue: asyncio.Queue[str] = asyncio.Queue()
_running_job: str | None = None
_job_phase: str | None = None                # admission | loading | running — only the last two are busy
_cancel: set[str] = set()


def _busy() -> bool:
    """Busy = the model is loading or computing. A job merely WAITING for admission is not busy:
    it holds no model memory, and pinning an idle model for it would keep the memory it is waiting
    for (the 2026-09-28 deadlock: waiting job → unload refused → never enough room to admit)."""
    return _active > 0 or _job_phase in ("loading", "running")


def _job_file(job_id: str) -> Path:
    return JOBS_DIR / f"{job_id}.json"


def _job_read(job_id: str) -> dict | None:
    f = _job_file(job_id)
    if not f.is_file():
        return None
    return json.loads(f.read_text())


def _job_write(job: dict) -> None:
    """Atomic write + fsync — a job's state must survive a crash or restart."""
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _job_file(job["job_id"]).with_suffix(".tmp")
    with open(tmp, "w") as fh:
        json.dump(job, fh, indent=1)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, _job_file(job["job_id"]))


async def _governor(client: httpx.AsyncClient, verb: str, payload: dict) -> dict:
    r = await client.post(f"{GOVERNOR_URL}/{verb}", json=payload, timeout=30)
    return r.json()


async def _run_job(job: dict) -> None:
    """admit (wait) → heartbeat-renew the lease → run on the child → persist → release."""
    global _active, _last_request_at, _job_phase
    alias = job["model"]
    est = REGISTRY[alias]["ram"]
    lease_job = f"colibri-job-{job['job_id']}"
    async with httpx.AsyncClient() as gov:
        # 1. admission — polite: ask, and if not granted, withdraw and wait OUTSIDE the governor queue
        _job_phase = "admission"
        job.update(status="waiting_admission", admit_started_at=time.time())
        _job_write(job)
        t0, attempts = time.time(), 0
        while True:
            if job["job_id"] in _cancel:
                job.update(status="cancelled", finished_at=time.time())
                _job_write(job)
                return
            attempts += 1
            try:
                d = await _governor(gov, "admit", {"job_id": lease_job, "model": alias,
                                                   "backend": "colibri", "est_gb": est})
            except Exception as e:
                d = {"grant": False, "reason": f"governor unreachable: {e}"}
            if d.get("grant"):
                job["lease"] = {"lease_id": d.get("lease_id"), "reserved_gb": d.get("reserved_gb")}
                break
            if d.get("terminal"):
                job.update(status="refused", error=f"governor: {d.get('reason')}",
                           finished_at=time.time())
                _job_write(job)
                return
            try:                                      # step out of the governor's line
                await _governor(gov, "release", {"job_id": lease_job})
            except Exception:
                pass
            # our own idle model is memory the governor is counting against us: free it while we wait
            if _child_alive() and _active == 0:
                async with _start_lock:
                    if _active == 0 and _child_alive():
                        print(f"[colibri] job {job['job_id']} not admitted; unloading idle model "
                              f"to free memory while it waits", flush=True)
                        await _stop_child()
            job["admission"] = {"attempts": attempts, "reason": d.get("reason"),
                                "last_checked_at": time.time(), "waited_s": round(time.time() - t0)}
            _job_write(job)
            if ADMIT_MAX_WAIT_S and time.time() - t0 > ADMIT_MAX_WAIT_S:
                job.update(status="refused", error=f"not admitted within {ADMIT_MAX_WAIT_S:.0f}s: "
                                                   f"{d.get('reason')}", finished_at=time.time())
                _job_write(job)
                return
            for _ in range(int(POLITE_RETRY_S)):      # interruptible by a cancel
                if job["job_id"] in _cancel:
                    break
                await asyncio.sleep(1)

        async def _heartbeat():
            while True:
                await asyncio.sleep(LEASE_RENEW_S)
                try:
                    await _governor(gov, "admit", {"job_id": lease_job, "model": alias,
                                                   "backend": "colibri", "est_gb": est})
                    job["lease_renewed_at"] = time.time()
                except Exception as e:
                    print(f"[colibri] lease renew failed for {lease_job}: {e}", flush=True)

        hb = asyncio.create_task(_heartbeat())
        try:
            _job_phase = "loading"
            job.update(status="loading", loading_started_at=time.time())
            _job_write(job)
            await _ensure_started(alias)
            body = _apply_request_defaults(alias, dict(job["body"]))
            body["model"] = alias
            body["stream"] = False
            _job_phase = "running"
            job.update(status="running", started_at=time.time())
            _job_write(job)
            _active += 1
            try:
                async with httpx.AsyncClient(timeout=CHILD_TIMEOUT) as c:
                    r = await c.post(f"{CHILD_BASE}/{job['path']}", json=body)
                try:
                    result = r.json()
                except Exception:
                    result = {"raw": r.text}
                job.update(status="done" if r.status_code < 400 else "failed",
                           http_status=r.status_code, result=result)
                if r.status_code >= 400:
                    job["error"] = f"child returned HTTP {r.status_code}"
            finally:
                _active -= 1
                _last_request_at = time.monotonic()
        except HTTPException as e:
            job.update(status="failed", error=f"{e.status_code}: {e.detail}")
        except Exception as e:
            job.update(status="failed", error=f"{type(e).__name__}: {e}")
        finally:
            hb.cancel()
            _job_phase = None
            if job["job_id"] in _cancel and job.get("status") != "done":
                job.update(status="cancelled", error="cancelled by DELETE /jobs (forced)")
            job["finished_at"] = time.time()
            if job.get("started_at"):
                job["run_seconds"] = round(job["finished_at"] - job["started_at"], 1)
            _job_write(job)                       # persist BEFORE releasing / reporting
            try:
                await _governor(gov, "release", {"job_id": lease_job})
            except Exception as e:
                print(f"[colibri] release failed for {lease_job}: {e}", flush=True)
            print(f"[colibri] job {job['job_id']} → {job['status']} "
                  f"({job.get('run_seconds')}s)", flush=True)


async def _job_worker() -> None:
    """Serial — one job at a time. A 64 GB box holds one Colibrì model and one sequence."""
    global _running_job
    while True:
        job_id = await _job_queue.get()
        job = _job_read(job_id)
        if not job or job.get("status") not in ("queued",):
            continue
        _running_job = job_id
        try:
            await _run_job(job)
            _cancel.discard(job_id)
        except Exception as e:
            job.update(status="failed", error=f"worker: {type(e).__name__}: {e}",
                       finished_at=time.time())
            _job_write(job)
        finally:
            _running_job = None


def _recover_jobs() -> dict:
    """On boot: re-queue QUEUED and WAITING_ADMISSION jobs (neither touched the model); mark
    loading/running jobs INTERRUPTED (never auto-retried — a failure that killed the process may
    well be deterministic)."""
    counts = {"requeued": 0, "interrupted": 0}
    if not JOBS_DIR.is_dir():
        return counts
    for f in sorted(JOBS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime):
        try:
            job = json.loads(f.read_text())
        except Exception:
            continue
        if job.get("status") in ("queued", "waiting_admission"):
            # waiting_admission never touched the model: requeue it rather than lose it
            if job["status"] == "waiting_admission":
                job["status"] = "queued"
                _job_write(job)
            _job_queue.put_nowait(job["job_id"])
            counts["requeued"] += 1
        elif job.get("status") in ("loading", "running"):
            job.update(status="interrupted", error="sidecar restarted while the job was in flight; "
                                                   "resubmit to retry", finished_at=time.time())
            _job_write(job)
            counts["interrupted"] += 1
    return counts


def _reap_stale_child() -> list[int]:
    """Kill a child group left behind by a previous sidecar process (crash / SIGKILL)."""
    if not PGID_FILE.is_file():
        return []
    try:
        pgid = int(PGID_FILE.read_text().strip())
    except ValueError:
        PGID_FILE.unlink(missing_ok=True)
        return []
    pids = _group_pids(pgid)
    if pids:
        # only reap if it is really ours: the group leader must be a `coli serve` for our port
        try:
            lead = subprocess.run(["ps", "-o", "command=", "-p", str(pgid)], capture_output=True,
                                  text=True, timeout=4).stdout
        except Exception:
            lead = ""
        if lead and not ("coli" in lead and f"--port {CHILD_PORT}" in lead):
            print(f"[colibri] pgid file points at a foreign group {pgid}: {lead.strip()!r} — left alone",
                  flush=True)
            PGID_FILE.unlink(missing_ok=True)
            return []
        print(f"[colibri] ▲ reaping orphaned child group {pgid} {pids} from a previous run", flush=True)
        for sig in (signal.SIGTERM, signal.SIGCONT):
            try:
                os.killpg(pgid, sig)
            except ProcessLookupError:
                pass
        for _ in range(20):
            if not _group_pids(pgid):
                break
            time.sleep(0.5)
        if _group_pids(pgid):
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            time.sleep(1)
    PGID_FILE.unlink(missing_ok=True)
    return pids


# ---------- lifecycle ----------
async def _idle_watcher() -> None:
    if KEEP_WARM:
        return
    while True:
        await asyncio.sleep(IDLE_TICK_SECONDS)
        if (_child_alive() and not _busy() and _job_queue.empty()
                and time.monotonic() - _last_request_at > IDLE_UNLOAD_SECONDS):
            async with _start_lock:
                if _active == 0 and _child_alive():
                    await _stop_child()


@asynccontextmanager
async def lifespan(app: FastAPI):
    _reaped_at_boot.extend(_reap_stale_child())
    rec = _recover_jobs()
    print(f"[colibri] jobs recovered: {rec}; models: {sorted(REGISTRY)}", flush=True)
    tasks = [asyncio.create_task(_idle_watcher()), asyncio.create_task(_job_worker())]
    if KEEP_WARM and DEFAULT_ALIAS:
        try:
            await _ensure_started(DEFAULT_ALIAS)
        except Exception as e:
            print(f"[colibri] pre-warm failed: {e}", flush=True)
    yield
    for t in tasks:
        t.cancel()
    async with _start_lock:
        await _stop_child()


app = FastAPI(lifespan=lifespan)


def _check_auth(request: Request) -> None:
    if not HUB_TOKEN:
        return
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer ") or auth[7:] != HUB_TOKEN:
        raise HTTPException(401, "invalid bearer token")


@app.get("/healthz")
def healthz():
    return {"ok": True, "service": "colibri", "engine": "colibri (coli serve)",
            "models": sorted(REGISTRY), "default": DEFAULT_ALIAS, "coli": str(COLI),
            "coli_present": COLI.is_file()}


@app.get("/readyz")
def readyz():
    alive = _child_alive() and _warmed
    busy = _busy()
    return {
        "ok": True,
        "state": "warm" if alive else "cold",
        "lifecycle": "cold" if not alive else ("busy" if busy else "idle"),
        "busy": busy,
        "active_jobs": _active,
        "queue_depth": _job_queue.qsize(),
        "running_job": _running_job,
        "job_phase": _job_phase,
        "warmed": _warmed,
        "engine": "colibri",
        "model": _current_alias,
        "model_path": REGISTRY.get(_current_alias, {}).get("path") if _current_alias else None,
        "ram_budget_gb": REGISTRY.get(_current_alias, {}).get("ram") if _current_alias else None,
        "available_models": sorted(REGISTRY),
        "default_model": DEFAULT_ALIAS,
        "child_pid": _proc.pid if _child_alive() else None,
        "child_group_pids": _group_pids(_proc.pid) if _child_alive() else [],
        "child_cpu_seconds": _child_cpu_seconds(),
        "child_uptime_s": round(time.time() - _started_at, 1) if _started_at and alive else None,
        "child_port": CHILD_PORT,
        "device": "cpu+ssd",
        "idle_seconds": round(time.monotonic() - _last_request_at, 1),
        "idle_unload_seconds": IDLE_UNLOAD_SECONDS,
        "keep_warm": KEEP_WARM,
        "last_start_error": _last_start_error,
        "reaped_at_boot": _reaped_at_boot,
        "last_unload_ago_s": round(time.monotonic() - _idle_unloaded_at, 1) if _idle_unloaded_at else None,
    }


@app.post("/admin/unload")
async def admin_unload(request: Request):
    _check_auth(request)
    force = request.query_params.get("force", "").lower() in ("1", "true", "yes")
    if _busy() and not force:
        return {"unloaded": False, "refused": "busy", "active_jobs": _active,
                "running_job": _running_job, "model": _current_alias}
    was, prev = _child_alive(), _current_alias
    if was:
        async with _start_lock:
            await _stop_child()
    return {"unloaded": was, "forced": force, "model": prev}


@app.get("/models")
def models_menu(request: Request):
    _check_auth(request)
    return {"object": "list", "default": DEFAULT_ALIAS, "loaded": _current_alias,
            "data": [{"id": a, "object": "model", "owned_by": "atelier-colibri",
                      "loaded": a == _current_alias and _warmed, "ram_gb": REGISTRY[a]["ram"],
                      "path": REGISTRY[a]["path"]} for a in sorted(REGISTRY)]}


@app.post("/jobs")
async def submit_job(request: Request):
    """Submit overnight work: {path: "v1/chat/completions"|…, body: {...}, model?, job_id?}."""
    _check_auth(request)
    req = await request.json()
    path = (req.get("path") or "v1/chat/completions").lstrip("/")
    if path not in JOB_PATHS:
        raise HTTPException(400, f"path must be one of {JOB_PATHS}")
    body = req.get("body")
    if not isinstance(body, dict):
        raise HTTPException(400, "body must be a JSON object (the request you would send to the path)")
    alias = _resolve_alias(req.get("model") or body.get("model"))
    job_id = req.get("job_id") or time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
    if _job_file(job_id).exists():
        raise HTTPException(409, f"job_id '{job_id}' already exists")
    job = {"job_id": job_id, "status": "queued", "path": path, "model": alias, "body": body,
           "submitted_at": time.time(), "label": req.get("label")}
    _job_write(job)                                   # persisted before it is queued
    await _job_queue.put(job_id)
    return JSONResponse({"ok": True, "job_id": job_id, "status": "queued",
                         "position": _job_queue.qsize(), "poll": f"/jobs/{job_id}"}, status_code=202)


@app.get("/jobs")
def list_jobs(request: Request, limit: int = 50):
    _check_auth(request)
    if not JOBS_DIR.is_dir():
        return {"jobs": []}
    files = sorted(JOBS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    out = []
    for f in files:
        try:
            j = json.loads(f.read_text())
        except Exception:
            continue
        out.append({k: j.get(k) for k in ("job_id", "status", "path", "model", "label",
                                         "submitted_at", "started_at", "finished_at",
                                         "run_seconds", "error")})
    return {"jobs": out, "queue_depth": _job_queue.qsize(), "running_job": _running_job}


@app.delete("/jobs/{job_id}")
async def cancel_job(job_id: str, request: Request):
    """Cancel a job. queued / waiting_admission: cancelled at once. loading / running: needs
    ?force=true and kills the engine group (the only way to stop a generation mid-flight)."""
    _check_auth(request)
    job = _job_read(job_id)
    if job is None:
        raise HTTPException(404, f"no job '{job_id}'")
    st = job.get("status")
    force = request.query_params.get("force", "").lower() in ("1", "true", "yes")
    if st == "queued":
        job.update(status="cancelled", finished_at=time.time())
        _job_write(job)
        return {"ok": True, "job_id": job_id, "was": st, "status": "cancelled"}
    if st == "waiting_admission":
        _cancel.add(job_id)
        return {"ok": True, "job_id": job_id, "was": st, "status": "cancelling (within ~1 s)"}
    if st in ("loading", "running"):
        if not force:
            return JSONResponse({"ok": False, "job_id": job_id, "status": st,
                                 "refused": "running — add ?force=true to kill the engine"},
                                status_code=409)
        _cancel.add(job_id)
        async with _start_lock:
            await _stop_child()
        return {"ok": True, "job_id": job_id, "was": st, "status": "cancelled (engine killed)"}
    return JSONResponse({"ok": False, "job_id": job_id, "status": st,
                         "refused": "already finished"}, status_code=409)


@app.get("/jobs/{job_id}")
def get_job(job_id: str, request: Request):
    _check_auth(request)
    job = _job_read(job_id)
    if job is None:
        raise HTTPException(404, f"no job '{job_id}'")
    if job.get("status") == "running" and job.get("started_at"):
        job["elapsed_s"] = round(time.time() - job["started_at"], 1)
        job["child_cpu_seconds"] = _child_cpu_seconds()   # rising = working, flat = stuck
    return job


@app.get("/agent")
def agent(request: Request):
    _check_auth(request)
    return {
        "service": "colibri",
        "role": "LLM — Colibrì (huge MoE streamed from SSD), OpenAI-compatible, OVERNIGHT/BATCH",
        "summary": "Runs very large MoE models by streaming experts from disk. SLOW by design "
                   "(fractions of a token/s up to a few tok/s). Use it for work nobody waits on: "
                   "submit to POST /jobs and collect later. No wall-clock timeouts; liveness is "
                   "child_cpu_seconds (rising = working).",
        "models": [{"alias": a, "ram_gb": REGISTRY[a]["ram"], "path": REGISTRY[a]["path"],
                    "loaded": a == _current_alias and _warmed} for a in sorted(REGISTRY)],
        "default_model": DEFAULT_ALIAS,
        "methods": [
            {"name": "submit job (PREFERRED)", "http": "POST /jobs",
             "params": "{path:'v1/chat/completions'|'v1/brio'|'v1/systemone'|…, body:{…}, label?}",
             "returns": "202 {job_id}; persisted, run serially under a renewed governor lease",
             "example": "curl -s $URL/jobs -H 'content-type: application/json' -d "
                        "'{\"path\":\"v1/chat/completions\",\"body\":{\"messages\":[{\"role\":\"user\","
                        "\"content\":\"hi\"}],\"max_tokens\":64}}'"},
            {"name": "job status/result", "http": "GET /jobs/{id}",
             "returns": "status queued|waiting_admission|loading|running|done|failed|refused|interrupted|cancelled, "
                        "result, run_seconds; while running: elapsed_s + child_cpu_seconds"},
            {"name": "cancel job", "http": "DELETE /jobs/{id}",
             "returns": "queued/waiting → cancelled; loading/running needs ?force=true (kills the engine)"},
            {"name": "closed-set scoring", "http": "POST /v1/brio (or via /jobs)",
             "params": "{state, question, options[]} | {state, questions[]} | {state, schema{}}",
             "returns": "probability per option + entropy; generates no tokens"},
            {"name": "Jev-compatible", "http": "POST /v1/systemone (or via /jobs)"},
            {"name": "sync chat", "http": "POST /v1/chat/completions",
             "note": "prefer the governor front door: POST :8799/llm/colibri/v1/chat/completions"},
            {"name": "unload", "http": "POST /admin/unload", "returns": "kills the whole engine group"},
        ],
        "instructions": (
            "1) For anything longer than a quick probe, POST /jobs and poll GET /jobs/{id}; do not "
            "hold an HTTP connection open for hours. Jobs YIELD: if the governor has no room, a job "
            "waits outside its queue and retries every 30 s, so it never blocks interactive calls. "
            "A sync call through the governor gets 503 when there is no room (it never queues).\n"
            "2) Never set a client timeout on a sync call shorter than the job; set a short CONNECT "
            "timeout and no read timeout. Watch /readyz child_cpu_seconds to tell slow from stuck.\n"
            "3) Brio/systemone are the fast path: scoring a fixed set of options costs a prefill, "
            "not a generation."),
        "openapi": "/openapi.json",
    }


@app.api_route("/v1/{path:path}", methods=["GET", "POST"])
async def proxy_v1(path: str, request: Request):
    """Sync OpenAI-compatible proxy. No read timeout; in-flight count blocks idle-unload."""
    global _active, _last_request_at
    _check_auth(request)
    body = await request.body()
    parsed: dict = {}
    if body:
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = {}
    alias = _resolve_alias(parsed.get("model"))
    await _ensure_started(alias)
    if parsed and request.method == "POST":
        before = json.dumps(parsed, sort_keys=True)
        parsed["model"] = alias
        _apply_request_defaults(alias, parsed)
        if json.dumps(parsed, sort_keys=True) != before:
            body = json.dumps(parsed).encode()
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in ("host", "content-length", "authorization")}
    client = httpx.AsyncClient(timeout=CHILD_TIMEOUT)
    _active += 1
    try:
        req = client.build_request(request.method, f"{CHILD_BASE}/v1/{path}", headers=headers,
                                   content=body, params=request.query_params)
        resp = await client.send(req, stream=True)
    except Exception as e:
        _active -= 1
        _last_request_at = time.monotonic()
        await client.aclose()
        raise HTTPException(502, f"colibri child proxy failed: {e}")

    async def streamer():
        global _active, _last_request_at
        try:
            async for chunk in resp.aiter_raw():
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()
            _active -= 1
            _last_request_at = time.monotonic()

    passthru = {k: v for k, v in resp.headers.items()
                if k.lower() not in ("content-length", "transfer-encoding", "connection")}
    return StreamingResponse(streamer(), status_code=resp.status_code, headers=passthru,
                             media_type=resp.headers.get("content-type"))
