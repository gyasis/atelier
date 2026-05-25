#!/usr/bin/env python3
"""Atelier memory governor — READ-ONLY monitor (b0).

Observes the whole hub and computes one unified-memory pressure signal. It NEVER
evicts anything — purely observability (Constitution I: observe before you act).
make-room / force / auto-eviction build on this telemetry later.

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

TOTAL_RAM_GB = float(os.environ.get("ATELIER_TOTAL_RAM_GB", "64"))
CLIFF_GB = float(os.environ.get("ATELIER_CLIFF_GB", "55"))   # swap onset
WARN_GB = float(os.environ.get("ATELIER_WARN_GB", "45"))     # approaching the cliff
POLL_SECONDS = int(os.environ.get("ATELIER_POLL_SECONDS", "10"))
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_LOG = Path(os.environ.get("OLLAMA_LOG", str(Path.home() / ".ollama/logs/server.log")))

SIDECARS = {
    "omnivoice": "http://127.0.0.1:8770/readyz",
    "kokoro": "http://127.0.0.1:8765/readyz",
    "dia": "http://127.0.0.1:8769/readyz",
}

_state = {
    "updated_at": None, "level": "ok", "free_gb": None, "resident_gb": None,
    "swapouts": None, "tenants": [], "alerts": [],
}
_recent_calls = collections.deque(maxlen=50)
_recent_events = collections.deque(maxlen=50)
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
        _recent_calls.append({"at": time.strftime("%H:%M:%S"), "status": m.group("status"),
                              "latency": m.group("lat"), "path": m.group("path")})
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

@asynccontextmanager
async def lifespan(app: FastAPI):
    t1 = asyncio.create_task(_poller())
    t2 = asyncio.create_task(_log_tailer())
    yield
    for t in (t1, t2):
        t.cancel()

app = FastAPI(lifespan=lifespan)

@app.get("/healthz")
def healthz():
    return {"ok": True, "service": "governor", "version": "0.1-monitor"}

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
            "last_spill": _last_spill}
