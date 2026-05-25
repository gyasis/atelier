#!/usr/bin/env python3
"""Atelier memory governor — monitor (b0) + make-room (b).

Observes the whole hub and computes one unified-memory pressure signal (b0), and frees
memory on demand by evicting ONLY idle models across both tenants (b, make-room). It
never touches a busy model — observe before you act, never evict what's working
(Constitution I). force-preempt of a busy model (c) and the auto pressure-watcher (d)
build on this.

Sources:
  - macOS `vm_stat`            → free / resident memory + swapouts (the cliff itself)
  - Ollama `GET /api/ps`       → loaded LLMs, footprint, context window, keep-alive
  - each sidecar `GET /readyz` → lifecycle (idle/busy/cold) + active_jobs + queue_depth
  - `~/.ollama/logs/server.log`→ per-call latency, load/evict events, the spill signal

Exposes (itself observable — no black boxes):
  GET /healthz   liveness
  GET /readyz    what it's monitoring + whether the log tail is live
  GET /pressure  {level: ok|warn|alarm, free_gb, resident_gb, swapouts, tenants[], alerts[]}
  GET /telemetry recent inference calls + lifecycle events + last spill
"""
import asyncio
import collections
import os
import re
import subprocess
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from pydantic import BaseModel

TOTAL_RAM_GB = float(os.environ.get("ATELIER_TOTAL_RAM_GB", "64"))
CLIFF_GB = float(os.environ.get("ATELIER_CLIFF_GB", "55"))   # swap onset
WARN_GB = float(os.environ.get("ATELIER_WARN_GB", "45"))     # approaching the cliff
POLL_SECONDS = int(os.environ.get("ATELIER_POLL_SECONDS", "10"))
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_LOG = Path(os.environ.get("OLLAMA_LOG", str(Path.home() / ".ollama/logs/server.log")))

SIDECAR_BASE = {
    "omnivoice": "http://127.0.0.1:8770",
    "kokoro": "http://127.0.0.1:8765",
    "dia": "http://127.0.0.1:8769",
}
SIDECARS = {name: f"{base}/readyz" for name, base in SIDECAR_BASE.items()}
SIDECAR_LOGS = {
    "omnivoice": Path.home() / "Library/Logs/omnivoice-sidecar.out.log",
    "kokoro": Path.home() / "Library/Logs/kokoro-sidecar.out.log",
    "dia": Path.home() / "Library/Logs/dia-sidecar.out.log",
}

_state = {
    "updated_at": None, "level": "ok", "free_gb": None, "resident_gb": None,
    "swapouts": None, "tenants": [], "alerts": [],
}
_recent_calls = collections.deque(maxlen=50)    # Ollama API calls (from ollama log)
_recent_events = collections.deque(maxlen=50)   # Ollama lifecycle events
_recent_synths = collections.deque(maxlen=50)   # per-call sidecar TTS telemetry (from sidecar logs)
_last_spill = None
_log_tail_alive = False
_prev_swapouts = None

# ---------- vm_stat ----------
def read_vm() -> dict:
    out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=4).stdout
    psize = 4096
    m = re.search(r"page size of (\d+)", out)
    if m:
        psize = int(m.group(1))
    def pages(label: str) -> int:
        mm = re.search(rf"{re.escape(label)}:\s+(\d+)", out)
        return int(mm.group(1)) if mm else 0
    free = (pages("Pages free") + pages("Pages inactive") + pages("Pages speculative")) * psize / 1e9
    used = TOTAL_RAM_GB - free
    return {
        "free_gb": round(free, 1),
        "resident_gb": round(used, 1),
        "compressed_gb": round(pages("Pages occupied by compressor") * psize / 1e9, 1),
        "swapouts": pages("Swapouts"),
    }

# ---------- async pollers ----------
async def poll_ollama(client: httpx.AsyncClient) -> list[dict]:
    try:
        r = await client.get(f"{OLLAMA_URL}/api/ps", timeout=3)
        models = r.json().get("models", [])
        return [{
            "tenant": "ollama", "name": m.get("name"),
            "mem_gb": round(m.get("size", 0) / 1e9, 1),
            "context": m.get("context"),
            "state": "busy" if m.get("expires_at") else "idle",
        } for m in models]
    except Exception:
        return []

async def poll_sidecar(client: httpx.AsyncClient, name: str, url: str) -> dict:
    try:
        d = (await client.get(url, timeout=3)).json()
        return {"tenant": "atelier", "name": name,
                "state": d.get("lifecycle", "cold"),
                "active_jobs": d.get("active_jobs", 0),
                "queue_depth": d.get("queue_depth", 0)}
    except Exception:
        return {"tenant": "atelier", "name": name, "state": "unreachable"}

def compute_level(vm: dict, spill_recent: bool) -> tuple[str, list[str]]:
    global _prev_swapouts
    alerts = []
    swap_rising = _prev_swapouts is not None and vm["swapouts"] > _prev_swapouts
    _prev_swapouts = vm["swapouts"]
    level = "ok"
    if swap_rising or vm["resident_gb"] >= CLIFF_GB:
        level = "alarm"
        if swap_rising:
            alerts.append(f"SWAPPING — swapouts rose to {vm['swapouts']} (over the cliff)")
        if vm["resident_gb"] >= CLIFF_GB:
            alerts.append(f"resident {vm['resident_gb']} GB ≥ cliff {CLIFF_GB} GB")
    elif vm["resident_gb"] >= WARN_GB or spill_recent:
        level = "warn"
        if vm["resident_gb"] >= WARN_GB:
            alerts.append(f"resident {vm['resident_gb']} GB ≥ warn {WARN_GB} GB")
        if spill_recent:
            alerts.append("Ollama model spilled to system RAM (offload < model)")
    return level, alerts

async def _poller():
    async with httpx.AsyncClient() as client:
        while True:
            try:
                vm = read_vm()
                tenants = await poll_ollama(client)
                for name, url in SIDECARS.items():
                    tenants.append(await poll_sidecar(client, name, url))
                spill_recent = _last_spill is not None and (time.time() - _last_spill["at"] < 120)
                level, alerts = compute_level(vm, spill_recent)
                _state.update({
                    "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "level": level, "free_gb": vm["free_gb"], "resident_gb": vm["resident_gb"],
                    "swapouts": vm["swapouts"], "tenants": tenants, "alerts": alerts,
                })
                if level != "ok":
                    print(f"[governor] {level.upper()} — resident={vm['resident_gb']}GB free={vm['free_gb']}GB :: {'; '.join(alerts)}", flush=True)
            except Exception as e:
                print(f"[governor] poll error: {e}", flush=True)
            await asyncio.sleep(POLL_SECONDS)

# ---------- Ollama log tailer ----------
_GIN = re.compile(r'^\[GIN\].*?\|\s*(?P<status>\d+)\s*\|\s*(?P<lat>[\d.]+[µmn]?s)\s*\|\s*\S+\s*\|\s*(?P<method>\w+)\s+"(?P<path>[^"]+)"')
_OFFLOAD = re.compile(r'layers\.model=(?P<model>\d+).*?layers\.offload=(?P<offload>\d+)')
_EVICT = re.compile(r'msg="?(expired event received|stopping llama server)')
_RUNNER = re.compile(r'llama runner started in (?P<sec>[\d.]+) seconds')

def parse_log_line(line: str):
    global _last_spill
    m = _GIN.search(line)
    if m and m.group("path") in ("/api/chat", "/api/generate"):
        _recent_calls.append({"at": time.strftime("%H:%M:%S"), "ts": time.time(),
                              "status": m.group("status"), "latency": m.group("lat"),
                              "path": m.group("path")})
        return
    m = _OFFLOAD.search(line)
    if m:
        model, offload = int(m.group("model")), int(m.group("offload"))
        if offload < model:
            _last_spill = {"at": time.time(), "model": model, "offload": offload}
            _recent_events.append({"at": time.strftime("%H:%M:%S"), "event": "spill", "detail": f"{offload}/{model} layers on GPU"})
        return
    if _EVICT.search(line):
        _recent_events.append({"at": time.strftime("%H:%M:%S"), "event": "ollama_evict"})
        return
    m = _RUNNER.search(line)
    if m:
        _recent_events.append({"at": time.strftime("%H:%M:%S"), "event": "model_loaded", "load_s": float(m.group("sec"))})

async def _log_tailer():
    global _log_tail_alive
    while True:
        try:
            if not OLLAMA_LOG.exists():
                _log_tail_alive = False
                await asyncio.sleep(5)
                continue
            with OLLAMA_LOG.open("r", errors="replace") as f:
                f.seek(0, os.SEEK_END)
                inode = os.fstat(f.fileno()).st_ino
                _log_tail_alive = True
                while True:
                    line = f.readline()
                    if line:
                        parse_log_line(line.rstrip("\n"))
                        continue
                    await asyncio.sleep(1)
                    # detect rotation
                    try:
                        if OLLAMA_LOG.exists() and os.stat(OLLAMA_LOG).st_ino != inode:
                            break
                    except OSError:
                        break
        except Exception as e:
            _log_tail_alive = False
            print(f"[governor] log tailer error: {e}", flush=True)
            await asyncio.sleep(5)

# Per-call sidecar TTS telemetry: every sidecar logs a line like
#   [tts] chars=293 14.74s rtf=0.82x        (omnivoice)
#   [tts] voice=af_bella chars=80 6.68s     (kokoro)
# Tail those so synth calls are visible in the governor — not siloed in each
# sidecar's private log (Constitution I: one observable pane, no black boxes).
_TTS = re.compile(r'\[tts\].*?chars=(?P<chars>\d+).*?(?P<sec>[\d.]+)s(?:.*?rtf=(?P<rtf>[\d.]+)x)?')

async def _tail_sidecar(name: str, path: Path):
    while True:
        try:
            if not path.exists():
                await asyncio.sleep(5)
                continue
            with path.open("r", errors="replace") as f:
                f.seek(0, os.SEEK_END)
                inode = os.fstat(f.fileno()).st_ino
                while True:
                    line = f.readline()
                    if line:
                        m = _TTS.search(line)
                        if m:
                            _recent_synths.append({
                                "at": time.strftime("%H:%M:%S"), "ts": time.time(),
                                "engine": name, "chars": int(m.group("chars")),
                                "seconds": float(m.group("sec")),
                                "rtf": float(m.group("rtf")) if m.group("rtf") else None,
                            })
                        continue
                    await asyncio.sleep(1)
                    try:
                        if path.exists() and os.stat(path).st_ino != inode:
                            break
                    except OSError:
                        break
        except Exception as e:
            print(f"[governor] sidecar tail {name} error: {e}", flush=True)
            await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(app: FastAPI):
    tasks = [asyncio.create_task(_poller()), asyncio.create_task(_log_tailer())]
    for nm, p in SIDECAR_LOGS.items():
        tasks.append(asyncio.create_task(_tail_sidecar(nm, p)))
    yield
    for t in tasks:
        t.cancel()

app = FastAPI(lifespan=lifespan)

@app.get("/healthz")
def healthz():
    return {"ok": True, "service": "governor", "version": "0.3-sidecar-telemetry"}

@app.get("/readyz")
def readyz():
    return {"ok": True, "monitoring": list(SIDECARS) + ["ollama"],
            "poll_seconds": POLL_SECONDS, "log_tail_alive": _log_tail_alive,
            "log": str(OLLAMA_LOG)}

@app.get("/pressure")
def pressure():
    return _state

@app.get("/telemetry")
def telemetry():
    return {"recent_calls": list(_recent_calls), "recent_events": list(_recent_events),
            "recent_synths": list(_recent_synths), "last_spill": _last_spill}


def _ollama_recently_active(window: float = 15.0) -> bool:
    """Proxy for 'Ollama busy' — any /api/chat|generate call within `window` seconds.
    The API has no in-flight metric, so the log tail is our busy signal."""
    now = time.time()
    return any(now - c.get("ts", 0) < window for c in _recent_calls)


class MakeRoomReq(BaseModel):
    need_gb: float = 0.0   # informational target; reached=true once free_gb >= need_gb
    dry_run: bool = False  # preview what WOULD be evicted, touch nothing


@app.post("/make-room")
async def make_room(req: MakeRoomReq):
    """(b) Free memory by evicting ONLY idle models across both tenants. Never touches
    a busy model (sidecar /admin/unload refuses busy; Ollama skipped if recently active).
    Safe + agent-callable — idle eviction can't interrupt a running job. Preempting a
    BUSY model is step (c), human-gated."""
    before = read_vm()["free_gb"]
    freed: list[dict] = []
    notes: list[str] = []
    async with httpx.AsyncClient() as client:
        # 1. idle sidecars (cheap, and /admin/unload double-checks busy)
        for name, base in SIDECAR_BASE.items():
            try:
                d = (await client.get(f"{base}/readyz", timeout=3)).json()
            except Exception:
                continue
            if d.get("lifecycle") == "idle":
                if req.dry_run:
                    freed.append({"tenant": "atelier", "name": name, "would_evict": True})
                else:
                    try:
                        r = (await client.post(f"{base}/admin/unload", timeout=12)).json()
                        freed.append({"tenant": "atelier", "name": name, "result": r})
                    except Exception as e:
                        notes.append(f"{name} unload failed: {e}")
        # 2. Ollama loaded models — only if not actively generating
        if _ollama_recently_active():
            notes.append("ollama skipped — inference call within last 15s")
        else:
            try:
                ps = (await client.get(f"{OLLAMA_URL}/api/ps", timeout=3)).json().get("models", [])
            except Exception:
                ps = []
            for m in ps:
                nm = m.get("name")
                gb = round(m.get("size", 0) / 1e9, 1)
                if req.dry_run:
                    freed.append({"tenant": "ollama", "name": nm, "mem_gb": gb, "would_evict": True})
                else:
                    try:
                        await client.post(f"{OLLAMA_URL}/api/generate",
                                          json={"model": nm, "keep_alive": 0}, timeout=20)
                        freed.append({"tenant": "ollama", "name": nm, "mem_gb": gb, "evicted": True})
                    except Exception as e:
                        notes.append(f"ollama stop {nm} failed: {e}")
    if not req.dry_run:
        await asyncio.sleep(1.5)  # let macOS reclaim before re-reading
    after = read_vm()["free_gb"]
    return {"ok": True, "dry_run": req.dry_run, "before_gb": before, "after_gb": after,
            "need_gb": req.need_gb, "reached": after >= req.need_gb if req.need_gb else None,
            "freed": freed, "notes": notes}
