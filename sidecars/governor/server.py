#!/usr/bin/env python3
"""Atelier memory governor — monitor (b0) + make-room (b) + force-stop (c) + watcher (d).

Observes the whole hub and computes one unified-memory pressure signal (b0); frees
memory on demand by evicting ONLY idle models across both tenants (b, make-room);
human-gated force-preempt of a BUSY model via a two-phase yield negotiation (c,
/force-stop); and an auto pressure-watcher (d) that on ALARM runs make-room itself
but only RECOMMENDS (never executes) a force-stop. It never silently touches a busy
model — observe before you act, never evict what's working (Constitution I).

Sources:
  - macOS `vm_stat`            → free / resident memory + swapouts (the cliff itself)
  - Ollama `GET /api/ps`       → loaded LLMs, footprint, context window, keep-alive
  - each sidecar `GET /readyz` → lifecycle (idle/busy/cold) + active_jobs + queue_depth
  - `~/.ollama/logs/server.log`→ per-call latency, load/evict events, the spill signal

Exposes (itself observable — no black boxes):
  GET  /healthz         liveness
  GET  /readyz          what it's monitoring + whether the log tail is live
  GET  /agent           hub-wide self-describing manifest for AI agents (entry point)
  GET  /pressure        {level, free_gb, resident_gb, swapouts, tenants[], alerts[], auto_action, recommendation}
  GET  /telemetry       recent inference calls + lifecycle events + last spill
  GET  /estimate        predicted ETA for a TTS synth or LLM reply (Bayesian per-model)
  POST /report          feed a completed run into the predictor
  GET  /benchmark       fire a tiny real generate → measure + record decode tok/s for a model
  GET  /predictor/stats learned per-model compute stats   ·   GET /predictor/export portable dataset
  POST /make-room       (b) evict ONLY idle models across both tenants
  POST /force-stop      (c) human-gated two-phase yield negotiation to preempt a BUSY model
"""
import asyncio
import collections
import os
import re
import sqlite3
import statistics
import secrets
import subprocess
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from pydantic import BaseModel

import predictor  # modular per-model ETA predictor (persistent, Bayesian)

TOTAL_RAM_GB = float(os.environ.get("ATELIER_TOTAL_RAM_GB", "64"))
CLIFF_GB = float(os.environ.get("ATELIER_CLIFF_GB", "55"))   # swap onset
WARN_GB = float(os.environ.get("ATELIER_WARN_GB", "45"))     # approaching the cliff
POLL_SECONDS = int(os.environ.get("ATELIER_POLL_SECONDS", "10"))
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_LOG = Path(os.environ.get("OLLAMA_LOG", str(Path.home() / ".ollama/logs/server.log")))
# (d) auto pressure-watcher: on ALARM, auto-run make-room (idle eviction only).
AUTO_MAKE_ROOM = os.environ.get("ATELIER_AUTO_MAKE_ROOM", "1") not in ("0", "false", "no")
AUTO_COOLDOWN = float(os.environ.get("ATELIER_AUTO_COOLDOWN", "60"))  # min seconds between auto evictions

SIDECAR_BASE = {
    "omnivoice": "http://127.0.0.1:8770",
    "kokoro": "http://127.0.0.1:8765",
    "dia": "http://127.0.0.1:8769",
    "whisper": "http://127.0.0.1:8766",
    "llamacpp": "http://127.0.0.1:8771",
    "fastmlx": "http://127.0.0.1:8772",
}
SIDECARS = {name: f"{base}/readyz" for name, base in SIDECAR_BASE.items()}
SIDECAR_LOGS = {
    "omnivoice": Path.home() / "Library/Logs/omnivoice-sidecar.out.log",
    "kokoro": Path.home() / "Library/Logs/kokoro-sidecar.out.log",
    "dia": Path.home() / "Library/Logs/dia-sidecar.out.log",
    "whisper": Path.home() / "Library/Logs/whisper-sidecar.out.log",
}
# launchd labels — used by (c) /force-stop --hard to kickstart -k a wedged sidecar.
SIDECAR_LABELS = {
    "omnivoice": "io.macstudio.hub.omnivoice",
    "kokoro": "io.macstudio.hub.kokoro",
    "dia": "io.macstudio.hub.dia",
    "whisper": "io.macstudio.hub.whisper",
    "llamacpp": "io.macstudio.hub.llamacpp",
    "fastmlx": "io.macstudio.hub.fastmlx",
}

_state = {
    "updated_at": None, "level": "ok", "free_gb": None, "resident_gb": None,
    "swapouts": None, "tenants": [], "alerts": [],
    "auto_action": None,      # (d) last auto make-room the watcher ran
    "recommendation": None,   # (d) force-stop the agent should surface for human authorization
}
_last_auto = 0.0   # (d) cooldown clock for auto make-room
_recent_calls = collections.deque(maxlen=50)    # Ollama API calls (from ollama log)
_recent_events = collections.deque(maxlen=50)   # Ollama lifecycle events
_recent_synths = collections.deque(maxlen=50)   # per-call sidecar TTS telemetry (from sidecar logs)
_last_spill = None
_log_tail_alive = False
_prev_swapouts = None
# Populated by the Ollama stats watcher — real eval_count/duration from last completed call
_ollama_last_stats: dict = {}

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

def _preempt_candidate(tenants: list[dict]) -> dict | None:
    """Pick the best BUSY model to RECOMMEND preempting (largest memory win first).
    Recommendation only — never auto-executed; force-stop is human-gated (c)."""
    busy = []
    for t in tenants:
        if t.get("tenant") == "ollama" and t.get("state") == "busy":
            busy.append({"target": f"ollama:{t['name']}", "mem_gb": t.get("mem_gb", 0),
                         "why": f"ollama model busy ({t.get('mem_gb', 0)} GB)"})
        elif t.get("tenant") == "atelier" and (t.get("active_jobs") or 0) > 0:
            busy.append({"target": t["name"], "mem_gb": None,
                         "why": f"{t['active_jobs']} active job(s), queue {t.get('queue_depth', 0)}"})
    if not busy:
        return None
    busy.sort(key=lambda b: (b["mem_gb"] is not None, b["mem_gb"] or 0), reverse=True)
    return busy[0]


async def _auto_relieve(vm: dict, tenants: list[dict]):
    """(d) On ALARM: auto-run make-room (idle eviction — SAFE, can't interrupt a job).
    If still over the cliff afterward, RECOMMEND a force-stop but never execute it —
    preempting a busy model stays human-gated (c). The watcher escalates to a human,
    it does not act on its own."""
    global _last_auto
    if not AUTO_MAKE_ROOM:
        return
    now = time.time()
    if now - _last_auto < AUTO_COOLDOWN:
        return
    _last_auto = now
    res = await make_room(MakeRoomReq(dry_run=False))
    freed = [f.get("name") for f in res.get("freed", []) if f.get("evicted") or f.get("result")]
    _state["auto_action"] = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "trigger": "alarm",
        "ran": "make-room (idle eviction)", "freed": freed,
        "before_gb": res.get("before_gb"), "after_gb": res.get("after_gb"),
    }
    print(f"[governor] AUTO make-room on ALARM — freed {freed}, "
          f"{res.get('before_gb')}→{res.get('after_gb')}GB", flush=True)
    after_vm = read_vm()
    if after_vm["resident_gb"] >= CLIFF_GB:
        cand = _preempt_candidate(tenants)
        if cand:
            _state["recommendation"] = {
                "action": "force-stop", "candidate": cand["target"], "reason": cand["why"],
                "after_idle_evict_gb": after_vm["resident_gb"], "cliff_gb": CLIFF_GB,
                "how": (f"idle eviction wasn't enough — a human must authorize preempting a busy "
                        f"model: POST /force-stop {{\"target\":\"{cand['target']}\"}} for the preview, "
                        f"then re-POST confirm=true + token"),
                "note": "NOT auto-executed — force-stop is human-gated (c)",
            }
            print(f"[governor] RECOMMEND force-stop {cand['target']} — still "
                  f"{after_vm['resident_gb']}GB after idle evict (human must authorize)", flush=True)
    else:
        _state["recommendation"] = None


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
                if level == "alarm":
                    await _auto_relieve(vm, tenants)   # (d) auto idle-evict; recommend (not execute) force-stop
                elif level == "ok":
                    _state["recommendation"] = None    # pressure cleared — drop any stale recommendation
            except Exception as e:
                print(f"[governor] poll error: {e}", flush=True)
            await asyncio.sleep(POLL_SECONDS)

# ---------- Ollama log tailer ----------
_GIN = re.compile(r'^\[GIN\]\s+(?P<date>\d{4}/\d{2}/\d{2})\s+-\s+(?P<time>\d{2}:\d{2}:\d{2})\s+\|\s*(?P<status>\d+)\s*\|\s*(?P<lat>[\d.a-zµ]+)\s*\|\s*\S+\s*\|\s*(?P<method>\w+)\s+"(?P<path>[^"]+)"')
_OFFLOAD = re.compile(r'layers\.model=(?P<model>\d+).*?layers\.offload=(?P<offload>\d+)')
_EVICT = re.compile(r'msg="?(expired event received|stopping llama server)')
_RUNNER = re.compile(r'llama runner started in (?P<sec>[\d.]+) seconds')

def parse_log_line(line: str):
    global _last_spill
    m = _GIN.search(line)
    if m and m.group("path") in ("/api/chat", "/api/generate"):
        # Use actual timestamp from the log line (preserves history across restarts)
        log_time = m.group("time") if "time" in m.groupdict() else time.strftime("%H:%M:%S")
        log_date = m.group("date") if "date" in m.groupdict() else ""
        try:
            import datetime
            if log_date:
                dt = datetime.datetime.strptime(f"{log_date} {log_time}", "%Y/%m/%d %H:%M:%S")
                ts = dt.timestamp()
            else:
                ts = time.time()
        except Exception:
            ts = time.time()
        entry = {"at": log_time, "ts": ts,
                 "status": m.group("status"), "latency": m.group("lat"),
                 "path": m.group("path")}
        # Attach latest perf stats (model name + tok/s) if available
        if _ollama_last_stats:
            entry.update(_ollama_last_stats)
        _recent_calls.append(entry)
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
                # --- Backfill: parse last 512KB on startup to restore _recent_calls ---
                f.seek(0, os.SEEK_END)
                size = f.tell()
                backfill_start = max(0, size - 512 * 1024)
                f.seek(backfill_start)
                if backfill_start > 0:
                    f.readline()  # skip partial line at seek boundary
                for line in f:
                    parse_log_line(line.rstrip("\n"))
                # Now at end, continue tailing
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
_NS = re.compile(r'num_step=(\d+)')
_backfilled: set = set()  # log paths whose tail we've already seeded into _recent_synths

def _ingest_tts(name: str, line: str, persist: bool = True):
    m = _TTS.search(line)
    if not m:
        return
    ns = _NS.search(line)
    chars = int(m.group("chars"))
    secs = float(m.group("sec"))
    num_step = int(ns.group(1)) if ns else None
    _recent_synths.append({
        "at": time.strftime("%H:%M:%S"), "ts": time.time(), "engine": name,
        "chars": chars, "seconds": secs,
        "rtf": float(m.group("rtf")) if m.group("rtf") else None,
        "num_step": num_step,
    })
    if persist and chars:  # live synths feed the predictor; backfill (persist=False) does not
        try:
            model = f"{name}:ns{num_step}" if num_step else name
            predictor.record(kind="tts", model=model, seconds=secs, in_units=chars,
                             device="coreml/mps" if name == "kokoro" else "mps")
        except Exception as e:
            print(f"[governor] predictor.record(tts) failed: {e}", flush=True)

# ASR (whisper) telemetry. The whisper sidecar logs (model is selectable per
# request, so it's tagged on every line):
#   [asr] model=whisper-large-v3-turbo audio_s=720.0 chars=8123 bytes=11534336 14.62s rtf=49.2x lang=en
# Surface these in the same observable pane as TTS synths, and feed the predictor
# with kind="asr", model=<the whisper variant>, in_units=audio_seconds (seconds
# of audio is the natural ETA unit). Per-model recording is what lets a caller
# compare turbo vs large ETAs and decide which to load.
_ASR = re.compile(r'\[asr\](?:.*?model=(?P<model>[\w./-]+))?.*?audio_s=(?P<audio>[\d.]+).*?chars=(?P<chars>\d+).*?(?P<sec>[\d.]+)s(?:.*?rtf=(?P<rtf>[\d.]+)x)?')

def _ingest_asr(name: str, line: str, persist: bool = True):
    m = _ASR.search(line)
    if not m:
        return
    audio_s = float(m.group("audio"))
    secs = float(m.group("sec"))
    model = m.group("model") or name
    _recent_synths.append({
        "at": time.strftime("%H:%M:%S"), "ts": time.time(), "engine": name,
        "kind": "asr", "model": model, "audio_s": audio_s, "chars": int(m.group("chars")),
        "seconds": secs, "rtf": float(m.group("rtf")) if m.group("rtf") else None,
    })
    if persist and audio_s:
        try:
            predictor.record(kind="asr", model=model, seconds=secs,
                             in_units=audio_s, device="mps")
        except Exception as e:
            print(f"[governor] predictor.record(asr) failed: {e}", flush=True)

async def _tail_sidecar(name: str, path: Path):
    while True:
        try:
            if not path.exists():
                await asyncio.sleep(5)
                continue
            with path.open("r", errors="replace") as f:
                # Seed the estimator from the log tail once per process, so /estimate
                # is useful immediately after a restart (telemetry is in-memory and
                # would otherwise cold-start empty).
                if str(path) not in _backfilled:
                    for ln in [x for x in f.readlines() if "[tts]" in x or "[asr]" in x][-30:]:
                        _ingest_tts(name, ln, persist=False)
                        _ingest_asr(name, ln, persist=False)
                    _backfilled.add(str(path))
                f.seek(0, os.SEEK_END)
                inode = os.fstat(f.fileno()).st_ino
                while True:
                    line = f.readline()
                    if line:
                        _ingest_tts(name, line)
                        _ingest_asr(name, line)
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


async def _ollama_stats_watcher():
    """Captures real tok/s + TTFT from Ollama response bodies after each completed call.
    On startup: seeds _ollama_last_stats from the predictor DB for the loaded model."""
    global _ollama_last_stats
    last_call_ts = 0.0

    # Seed from predictor on startup — gives historical avg_rate immediately
    try:
        llm_stats = {s["model"]: s for s in predictor.stats() if s["kind"] == "llm" and (s["avg_rate"] or 0) > 1}
        async with httpx.AsyncClient() as c:
            ps = await c.get(f"{OLLAMA_URL}/api/ps", timeout=3)
            loaded = ps.json().get("models", [])
            if loaded:
                m = loaded[0].get("name", "")
                if m in llm_stats:
                    _ollama_last_stats = {
                        "model": m,
                        "tok_s": llm_stats[m]["avg_rate"],
                        "ttft_ms": None,  # predictor doesn't store TTFT yet
                        "source": "predictor_historical",
                    }
    except Exception:
        pass

    # The probe itself is an /api/generate call, so it gets logged and would
    # re-trigger this watcher every tick — a self-feedback loop that pins the
    # model resident forever. Guard with a cooldown AND by consuming every call
    # already seen (incl. our own probe) after probing.
    PROBE_COOLDOWN = float(os.environ.get("ATELIER_PROBE_COOLDOWN", "60"))
    last_probe_ts = 0.0
    async with httpx.AsyncClient() as client:
        while True:
            await asyncio.sleep(0.5)
            try:
                # Check if a new call completed since our last probe
                if _recent_calls:
                    latest = _recent_calls[-1]
                    call_ts = latest.get("ts", 0)
                    mono = time.monotonic()
                    # Cooldown breaks the runaway: at most one probe per window,
                    # however many calls (real or self-induced) show up.
                    if (call_ts > last_call_ts and latest.get("status") == "200"
                            and mono - last_probe_ts >= PROBE_COOLDOWN):
                        last_call_ts = call_ts
                        last_probe_ts = mono
                        # Find which model is/was loaded
                        ps = await client.get(f"{OLLAMA_URL}/api/ps", timeout=3)
                        models = ps.json().get("models", [])
                        if not models:
                            continue
                        model_name = models[0].get("name", "")
                        # Fire a tiny probe (8 tokens) to get fresh eval stats for this model
                        probe = await client.post(f"{OLLAMA_URL}/api/generate",
                            json={"model": model_name, "prompt": "Hi", "stream": False,
                                  "options": {"num_predict": 8}, "keep_alive": "5m"},
                            timeout=30)
                        # Consume EVERY call logged so far — including this probe's own
                        # GIN line once it lands — so the probe can't re-trigger us.
                        if _recent_calls:
                            last_call_ts = max(c.get("ts", 0) for c in _recent_calls)
                        if probe.status_code == 200:
                            d = probe.json()
                            ec = d.get("eval_count", 0)
                            ed = d.get("eval_duration", 0)
                            pe = d.get("prompt_eval_duration", 0)
                            ld = d.get("load_duration", 0)
                            if ec and ed:
                                _ollama_last_stats = {
                                    "model": model_name,
                                    "tok_s": round(ec / (ed / 1e9), 1),
                                    "ttft_ms": round(pe / 1e6, 0) if pe else None,
                                    "load_ms": round(ld / 1e6, 0) if ld else None,
                                    "eval_tokens": ec,
                                }
                                # Also record to predictor for long-term learning
                                try:
                                    predictor.record(kind="llm", model=model_name,
                                        seconds=ed/1e9, out_units=ec,
                                        in_units=d.get("prompt_eval_count"),
                                        location="local", host="mac-studio", device="mps",
                                        state="cold" if ld > 1e9 else "warm")
                                except Exception:
                                    pass
            except Exception:
                pass

@asynccontextmanager
async def lifespan(app: FastAPI):
    tasks = [asyncio.create_task(_poller()), asyncio.create_task(_log_tailer()),
             asyncio.create_task(_ollama_stats_watcher())]
    for nm, p in SIDECAR_LOGS.items():
        tasks.append(asyncio.create_task(_tail_sidecar(nm, p)))
    yield
    for t in tasks:
        t.cancel()

app = FastAPI(lifespan=lifespan)

@app.get("/healthz")
def healthz():
    return {"ok": True, "service": "governor", "version": "0.6-predictor"}

@app.get("/readyz")
def readyz():
    return {"ok": True, "monitoring": list(SIDECARS) + ["ollama"],
            "poll_seconds": POLL_SECONDS, "log_tail_alive": _log_tail_alive,
            "log": str(OLLAMA_LOG)}

@app.get("/pressure")
def pressure():
    return _state

# Roles for the hub manifest. Sidecars that serve their own GET /agent are in
# AGENT_CAPABLE — an agent drills into those for full per-service instructions.
SIDECAR_ROLES = {
    "omnivoice": "TTS — primary, instruct-driven accent/pitch/gender",
    "kokoro": "TTS — fast, fixed voices (fallback)",
    "dia": "TTS — expressive voice cloning (batch)",
    "whisper": "ASR — speech-to-text, + optional LLM structure/summarize",
    "llamacpp": "LLM — llama.cpp/llama-server (Metal, GGUF), OpenAI-compatible",
    "fastmlx": "LLM/VLM — FastMLX (MLX-native), OpenAI-compatible",
}
AGENT_CAPABLE = {"whisper", "omnivoice", "kokoro", "dia", "llamacpp", "fastmlx"}

async def _fetch_agent_manifest(client: httpx.AsyncClient, url: str) -> dict:
    """Pull one sidecar's /agent. GET /agent never wakes a model, so expanding is
    cheap and safe. Returns an `unavailable` stub if the sidecar is down/cold."""
    try:
        r = await client.get(url, timeout=3)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"unavailable": f"{type(e).__name__} — could not fetch {url} (sidecar down?)"}

@app.get("/agent")
async def agent(expand: bool = False):
    """Hub-wide self-describing manifest. An agent fetches THIS ONE route to
    discover all of Atelier: the live state, the control plane, and every
    sidecar — then drills into each sidecar's own GET /agent for method-level
    detail. The single entry point for 'how do I use Atelier?'.

    Add ?expand=true to inline EVERY sidecar's full /agent manifest in this one
    response (concurrent fan-out) — one round-trip, no follow-up fetches."""
    sidecars = {}
    for name, base in SIDECAR_BASE.items():
        sidecars[name] = {
            "base_url": base,
            "role": SIDECAR_ROLES.get(name, "sidecar"),
            "readyz": f"{base}/readyz",
            "agent": f"{base}/agent" if name in AGENT_CAPABLE else None,
        }
    if expand:
        token = os.environ.get("HUB_TOKEN")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        capable = [(n, SIDECAR_BASE[n]) for n in SIDECAR_BASE if n in AGENT_CAPABLE]
        async with httpx.AsyncClient(headers=headers) as client:
            manifests = await asyncio.gather(
                *[_fetch_agent_manifest(client, f"{b}/agent") for _, b in capable]
            )
        for (n, _), manifest in zip(capable, manifests):
            sidecars[n]["manifest"] = manifest
    return {
        "service": "atelier-governor",
        "role": "hub supervisor — memory governor, telemetry, ETA predictor",
        "summary": "One LAN inference hub on a Mac Studio. The governor watches "
                   "unified-memory pressure across Ollama + sidecars, evicts idle "
                   "models to make room, records every run, and predicts ETAs.",
        "tip": "GET /agent?expand=true to inline every sidecar's full manifest in "
               "one fetch (no follow-up calls).",
        "how_to_start": (
            "1) GET /agent?expand=true — one round-trip gives you the whole hub: "
            "control plane + every sidecar's full method list.\n"
            "2) GET /pressure — see what's loaded, memory level, and per-tenant "
            "state (busy/idle/cold) right now.\n"
            "3) Before a heavy job, check /pressure.level; if 'alarm', POST "
            "/make-room to evict idle models. Use /estimate for an ETA first.\n"
            "4) After a run, POST /report so the predictor sharpens."
        ),
        "control_plane": {
            "GET /pressure": "live memory level + tenants[] (busy/idle/cold, jobs, queue)",
            "GET /telemetry": "recent inference calls, synths (incl. [asr]), events",
            "GET /estimate": "ETA for a job — ?engine=whisper&audio_s=N (asr) | "
                             "?engine=<tts>&chars=N | ?model=<llm>&out_tokens=N",
            "GET /predictor/stats": "learned per-(kind,model) compute stats",
            "POST /report": "feed a completed run into the predictor",
            "POST /make-room": "evict ONLY idle models to free memory",
            "POST /force-stop": "human-gated preempt of a BUSY model",
        },
        "sidecars": sidecars,
        "ollama": {"base_url": OLLAMA_URL, "role": "LLM + embeddings + VLM",
                   "list_loaded": f"{OLLAMA_URL}/api/ps"},
        "notes": "LAN-only. One model per sidecar is resident at a time; unified "
                 "memory (64 GB) is the scarce resource — respect /pressure.",
    }

@app.get("/telemetry")
def telemetry():
    return {"recent_calls": list(_recent_calls), "recent_events": list(_recent_events),
            "recent_synths": list(_recent_synths), "last_spill": _last_spill,
            "ollama_perf": _ollama_last_stats}


@app.get("/estimate")
def estimate(engine: str = "", model: str = "", kind: str = "", chars: int = 0,
             out_tokens: int = 0, num_step: int | None = None, audio_s: float = 0.0,
             location: str = "local", state: str = "warm"):
    """Predict ETA via the modular predictor (per-model Bayesian, learns from
    accumulated runs). TTS: ?engine=omnivoice&chars=N[&num_step=48|64]. ASR:
    ?engine=whisper&audio_s=N. LLM:
    ?model=<name>[&out_tokens=N][&location=local|cloud][&state=warm|cold]."""
    if not kind:
        if engine == "whisper":
            kind = "asr"
        elif engine in ("omnivoice", "kokoro", "dia"):
            kind = "tts"
        else:
            kind = "llm"
    if kind == "asr":
        # model is the whisper variant short-name as recorded from [asr] logs,
        # e.g. whisper-large-v3-turbo (fast) or whisper-large-v3 (accurate).
        return predictor.predict(kind="asr", model=(model or "whisper-large-v3-turbo"),
                                 in_units=audio_s, state=state)
    if kind == "tts":
        mdl = model or engine
        if num_step:
            mdl = f"{mdl}:ns{num_step}"
        return predictor.predict(kind="tts", model=mdl, in_units=chars, state=state)
    return predictor.predict(kind="llm", model=(model or engine or "unknown"),
                             out_units=(out_tokens or None), location=location, state=state)


class RunReport(BaseModel):
    kind: str                       # "tts" | "llm"
    model: str
    seconds: float
    in_units: int | None = None     # chars (tts) or prompt tokens (llm)
    out_units: int | None = None    # audio-seconds (tts) or output tokens (llm)
    rate: float | None = None
    location: str = "local"
    host: str = "mac-studio"
    device: str = "mps"
    state: str = "warm"
    net_latency_ms: float = 0.0
    queue_depth: int = 0


@app.post("/report")
def report(r: RunReport):
    """Feed a completed run into the predictor so it sharpens. The gateway POSTs its
    real generate stats here (e.g. Ollama eval_count/eval_duration) to learn
    tokens/s + output distributions — including thinking models."""
    predictor.record(**r.model_dump())
    return {"ok": True, "recorded": r.model_dump()}


@app.get("/predictor/stats")
def predictor_stats():
    """What the predictor has learned per (kind, model) — the shareable compute-stats base."""
    return {"runs": predictor.stats(), "db": str(predictor.DB)}


@app.get("/predictor/export")
def predictor_export():
    """Export the full run dataset (JSON) — portable compute-stats, sharable across
    servers / other local models. Re-import elsewhere by POSTing rows to /report."""
    c = sqlite3.connect(str(predictor.DB))
    c.row_factory = sqlite3.Row
    rows = [dict(x) for x in c.execute("SELECT * FROM runs ORDER BY ts").fetchall()]
    c.close()
    return {"count": len(rows), "schema": "atelier-predictor-v1", "runs": rows}


@app.get("/benchmark")
async def benchmark(model: str = "", tokens: int = 64, keep_alive: str = "0",
                    prompt: str = "Write a few sentences describing a sunset over the ocean."):
    """Fire a tiny REAL generate against Ollama to MEASURE decode tok/s for `model`,
    then record it to the predictor so /estimate sharpens immediately. The rate is
    clean — eval_count / eval_duration (decode only), NOT total_duration (which folds
    in cold-load + prompt-eval). Economy-first: keep_alive=0 unloads the model right
    after the measurement; pass keep_alive=5m to leave it warm.
    Examples: /benchmark?model=gemma3:4b  ·  /benchmark?model=deepseek-r1:7b&tokens=128"""
    if not model:
        return {"ok": False, "error": "model required (e.g. /benchmark?model=gemma3:4b)"}
    payload = {"model": model, "prompt": prompt, "stream": False,
               "keep_alive": keep_alive, "options": {"num_predict": tokens}}
    async with httpx.AsyncClient() as client:
        try:
            r = await client.post(f"{OLLAMA_URL}/api/generate", json=payload, timeout=180)
        except Exception as e:
            return {"ok": False, "error": f"ollama generate failed: {e}"}
    if r.status_code != 200:
        return {"ok": False, "error": f"ollama {r.status_code}: {r.text[:200]}"}
    d = r.json()
    eval_count = d.get("eval_count") or 0
    eval_ns = d.get("eval_duration") or 0
    if not eval_count or not eval_ns:
        return {"ok": False, "error": "ollama returned no decode stats (0 tokens?)", "raw": d}
    decode_s = eval_ns / 1e9
    load_s = (d.get("load_duration") or 0) / 1e9
    state = "cold" if load_s > 1.0 else "warm"   # a real cold-load shows up as seconds of load_duration
    prompt_count = d.get("prompt_eval_count")
    measured = {
        "decode_tok_s": round(eval_count / decode_s, 1), "out_tokens": eval_count,
        "decode_s": round(decode_s, 2), "prompt_tokens": prompt_count,
        "load_s": round(load_s, 2), "total_s": round((d.get("total_duration") or 0) / 1e9, 2),
        "state": state,
    }
    try:
        predictor.record(kind="llm", model=model, seconds=decode_s, out_units=eval_count,
                         in_units=prompt_count, location="local", host="mac-studio",
                         device="mps", state=state)
        recorded = True
    except Exception as e:
        print(f"[governor] benchmark record failed: {e}", flush=True)
        recorded = False
    return {"ok": True, "model": model, "measured": measured, "recorded": recorded,
            "keep_alive": keep_alive,
            "now_predicts": predictor.predict(kind="llm", model=model,
                                               out_units=tokens, location="local", state="warm")}


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


# (c) Force-preempt a BUSY model — a yield NEGOTIATION, not a blunt kill.
# Make-room (b) only evicts idle models; it refuses to interrupt a running job.
# When a sender genuinely needs memory a busy receiver is holding, this is the
# escalation — but it's HUMAN-GATED: a poll/handshake between sender and receiver
# with a human authorizing in the middle. Two phases:
#   1. unconfirmed POST  → PREVIEW: who's asking (requester/need_gb), what the
#      receiver is doing right now (busy? active_jobs? queue_depth?), how
#      disruptive yielding would be, + a short-lived confirm_token. Touches nothing.
#   2. POST confirm=true + token → the human has authorized; the receiver yields.
# No single blind call can preempt a busy model — that IS the gate.
# Trust model: the LAN is free+open (no network auth between services). The
# human-gate is enforced BEHAVIORALLY at the agent layer — the calling agent
# shows the preview and asks "are you sure?" before sending confirm=true. The
# governor doesn't authenticate the human; the two-phase token just guarantees
# the agent saw the disruption preview before it could authorize.
_force_tokens: dict[str, dict] = {}   # token -> {target, hard, ts}
_FORCE_TOKEN_TTL = 60.0


class ForceStopReq(BaseModel):
    target: str = ""        # receiver asked to yield: "omnivoice"|"kokoro"|"dia"|"ollama:<model>"
    requester: str = ""     # sender — who needs the memory (for the human-readable handshake)
    need_gb: float = 0.0    # how much the sender needs (informational, shown to the human)
    confirm: bool = False   # human authorization — must be true WITH a valid token to execute
    token: str = ""         # echo the confirm_token returned by the preview (poll) call
    hard: bool = False       # sidecar only: kickstart -k the process vs a soft model-unload


async def _receiver_state(client: httpx.AsyncClient, target: str) -> dict:
    """Poll what the receiver is doing right now — the 'receiver' half of the handshake."""
    if target.startswith("ollama:"):
        model = target.split(":", 1)[1]
        try:
            ps = (await client.get(f"{OLLAMA_URL}/api/ps", timeout=3)).json().get("models", [])
        except Exception:
            ps = []
        m = next((x for x in ps if x.get("name") == model), None)
        return {"kind": "ollama", "model": model, "loaded": m is not None,
                "mem_gb": round(m.get("size", 0) / 1e9, 1) if m else 0.0,
                "busy": _ollama_recently_active(), "active_jobs": None, "queue_depth": None}
    base = SIDECAR_BASE.get(target)
    if not base:
        return {"kind": "unknown", "error": f"unknown target '{target}'"}
    try:
        d = (await client.get(f"{base}/readyz", timeout=3)).json()
    except Exception as e:
        return {"kind": "sidecar", "name": target, "error": f"unreachable: {e}"}
    return {"kind": "sidecar", "name": target, "lifecycle": d.get("lifecycle"),
            "busy": bool(d.get("busy")), "active_jobs": d.get("active_jobs"),
            "queue_depth": d.get("queue_depth")}


@app.post("/force-stop")
async def force_stop(req: ForceStopReq):
    if not req.target:
        return {"ok": False, "error": "target required (omnivoice|kokoro|dia|ollama:<model>)"}
    is_ollama = req.target.startswith("ollama:")
    if not is_ollama and req.target not in SIDECAR_BASE:
        return {"ok": False, "error": f"unknown target '{req.target}'"}

    async with httpx.AsyncClient() as client:
        rstate = await _receiver_state(client, req.target)
        busy = bool(rstate.get("busy"))
        aj, qd = rstate.get("active_jobs"), rstate.get("queue_depth")
        if busy:
            bits = []
            if aj:
                bits.append(f"{aj} in-flight job{'s' if aj != 1 else ''}")
            if qd:
                bits.append(f"{qd} queued")
            disruption = "WILL ABORT " + (" + ".join(bits) if bits else "a running job")
        else:
            disruption = "receiver is idle — yielding is safe (prefer /make-room for idle)"

        # ---- Phase 1: PREVIEW (poll) — no token or unconfirmed. Touch nothing. ----
        if not req.confirm:
            token = secrets.token_hex(8)
            _force_tokens[token] = {"target": req.target, "hard": req.hard, "ts": time.time()}
            # opportunistic GC of expired tokens
            now = time.time()
            for t in [k for k, v in _force_tokens.items() if now - v["ts"] > _FORCE_TOKEN_TTL]:
                _force_tokens.pop(t, None)
            free_now = read_vm()["free_gb"]
            return {
                "ok": True, "phase": "preview",
                "handshake": {
                    "sender": req.requester or "(unspecified)",
                    "need_gb": req.need_gb or None,
                    "free_gb_now": free_now,
                    "receiver": req.target,
                },
                "receiver_state": rstate,
                "disruption": disruption,
                "method": ("hard kickstart -k (process restart)" if req.hard
                           else "soft model-unload (process stays up, reloads on next call)"),
                "confirm_token": token, "expires_in_s": int(_FORCE_TOKEN_TTL),
                "next": "human authorizes → re-POST same target with confirm=true and this token",
            }

        # ---- Phase 2: EXECUTE — confirm=true requires a valid, matching, fresh token ----
        tok = _force_tokens.get(req.token)
        if not tok:
            return {"ok": False, "error": "missing/expired confirm_token — re-run the preview (poll) call first"}
        if tok["target"] != req.target:
            return {"ok": False, "error": f"token was issued for '{tok['target']}', not '{req.target}'"}
        if time.time() - tok["ts"] > _FORCE_TOKEN_TTL:
            _force_tokens.pop(req.token, None)
            return {"ok": False, "error": "confirm_token expired — re-run the preview (poll) call"}
        _force_tokens.pop(req.token, None)   # one-shot

        before = read_vm()["free_gb"]
        result: dict = {}
        if is_ollama:
            model = req.target.split(":", 1)[1]
            try:
                await client.post(f"{OLLAMA_URL}/api/generate",
                                  json={"model": model, "keep_alive": 0}, timeout=20)
                result = {"method": "ollama keep_alive=0",
                          "note": "unloads after the current request returns; Ollama has no clean mid-stream abort"}
            except Exception as e:
                return {"ok": False, "error": f"ollama force-unload failed: {e}"}
        elif req.hard:
            label = SIDECAR_LABELS[req.target]
            try:
                subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"],
                               check=True, capture_output=True, timeout=15)
                result = {"method": f"launchctl kickstart -k {label}",
                          "note": "process killed + relaunched by launchd; cold on next call"}
            except subprocess.CalledProcessError as e:
                return {"ok": False, "error": f"kickstart failed: {e.stderr.decode()[:200]}"}
        else:
            base = SIDECAR_BASE[req.target]
            try:
                r = (await client.post(f"{base}/admin/unload", params={"force": "true"}, timeout=15)).json()
                result = {"method": "soft unload?force=true", "sidecar_result": r}
            except Exception as e:
                return {"ok": False, "error": f"soft force-unload failed: {e}"}

        await asyncio.sleep(1.5)   # let macOS reclaim before re-reading
        after = read_vm()["free_gb"]
        return {"ok": True, "phase": "executed", "target": req.target,
                "requester": req.requester or None, "need_gb": req.need_gb or None,
                "before_gb": before, "after_gb": after, "freed_gb": round((after or 0) - (before or 0), 1),
                "reached": (after >= req.need_gb) if req.need_gb else None,
                "result": result}
