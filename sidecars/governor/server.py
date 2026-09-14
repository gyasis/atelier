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
  GET  /inventory       (h) who ACTUALLY holds memory — process RSS reconciled against every
                            API self-report; flags anything loaded that no API admits to
  POST /unload          (h) one door to free a specific target: <sidecar>|ollama:<m>|pid:<n>
  POST /make-room       (b) evict ONLY idle models across both tenants
  POST /force-stop      (c) human-gated two-phase yield negotiation to preempt a BUSY model
  GET  /budget          (e) global LLM memory budget + active leases + wait queue
  POST /admit           (e) admission gate — grant if it fits, else queue (never evict on arrival)
  POST /release         (e) drop a lease when a job finishes/aborts
  POST /llm/{backend}/{path}  (f) opt-in CAPTURING proxy — admit→forward→record(prompt,tokens)→release
"""
import asyncio
import collections
import json
import os
import re
import socket
import sqlite3
import statistics
import secrets
import subprocess
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

import predictor  # modular per-model ETA predictor (persistent, Bayesian)
import admission  # modular memory-aware LLM admission gate (docs/LLM_ADMISSION_QUEUE.md)

TOTAL_RAM_GB = float(os.environ.get("ATELIER_TOTAL_RAM_GB", "64"))
CLIFF_GB = float(os.environ.get("ATELIER_CLIFF_GB", "55"))   # swap onset
WARN_GB = float(os.environ.get("ATELIER_WARN_GB", "45"))     # approaching the cliff
POLL_SECONDS = int(os.environ.get("ATELIER_POLL_SECONDS", "10"))
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_LOG = Path(os.environ.get("OLLAMA_LOG", str(Path.home() / ".ollama/logs/server.log")))
# (d) auto pressure-watcher: on ALARM, auto-run make-room (idle eviction only).
AUTO_MAKE_ROOM = os.environ.get("ATELIER_AUTO_MAKE_ROOM", "1") not in ("0", "false", "no")
AUTO_COOLDOWN = float(os.environ.get("ATELIER_AUTO_COOLDOWN", "60"))  # min seconds between auto evictions
# (g) AUTO-HEAL — Law 1 enforced from OUTSIDE. A sidecar reporting state=cold (model unloaded) that
# still holds > FLOOR GB is LEAKING; after a grace period, hard-restart it via launchd. The
# GovernedSidecar framework self-heals first — this backstops anything bespoke / non-self-healing.
AUTOHEAL = os.environ.get("ATELIER_AUTOHEAL", "1") not in ("0", "false", "no")
AUTOHEAL_FLOOR_GB = float(os.environ.get("ATELIER_AUTOHEAL_FLOOR_GB", "1.5"))
AUTOHEAL_GRACE_S = float(os.environ.get("ATELIER_AUTOHEAL_GRACE_S", "150"))
# (i) HARD IDLE CEILING — nothing stays resident forever, warm tag or not.
# keep_warm buys warmth DURING a work session; it is not a licence to hold GB for days.
# Any sidecar with no call in MAX_IDLE_S gets unloaded, and kicked via launchd if the
# unload doesn't take. The ONLY exemption is an explicit, REASONED entry in the exempt
# file — a forgotten `KEEP_WARM=true` in a plist is not a justification, so the flag
# alone buys nothing here. Exemptions are surfaced in /pressure and `atelier ps`, never
# silent, so a long-lived warm model is always something someone chose and can defend.
MAX_IDLE_ENFORCE = os.environ.get("ATELIER_MAX_IDLE_ENFORCE", "1") not in ("0", "false", "no")
MAX_IDLE_S = float(os.environ.get("ATELIER_MAX_IDLE_S", "1800"))          # 30 minutes
MAX_IDLE_MIN_GB = float(os.environ.get("ATELIER_MAX_IDLE_MIN_GB", "0.3"))  # ignore near-empty
WARM_EXEMPT_FILE = Path(os.environ.get(
    "ATELIER_WARM_EXEMPT_FILE", str(Path.home() / ".config/atelier/warm-exempt.json")))

SIDECAR_BASE = {
    "omnivoice": "http://127.0.0.1:8770",
    "kokoro": "http://127.0.0.1:8765",
    "dia": "http://127.0.0.1:8769",
    "whisper": "http://127.0.0.1:8766",
    "llamacpp": "http://127.0.0.1:8771",
    "fastmlx": "http://127.0.0.1:8772",
    "mlxlm": "http://127.0.0.1:8773",
    "radiogen": "http://127.0.0.1:8774",
    "maisi": "http://127.0.0.1:8775",
    "medner": "http://127.0.0.1:8131",
    "colpali": "http://127.0.0.1:8779",
    "pronounce": "http://127.0.0.1:8782",
    "tabfm": "http://127.0.0.1:8781",
    "rerank": "http://127.0.0.1:8778",
    "pyannote": "http://127.0.0.1:8767",
    "audio-llm": "http://127.0.0.1:8768",
}
SIDECARS = {name: f"{base}/readyz" for name, base in SIDECAR_BASE.items()}
SIDECAR_LOGS = {
    "omnivoice": Path.home() / "Library/Logs/omnivoice-sidecar.out.log",
    "kokoro": Path.home() / "Library/Logs/kokoro-sidecar.out.log",
    "dia": Path.home() / "Library/Logs/dia-sidecar.out.log",
    "whisper": Path.home() / "Library/Logs/whisper-sidecar.out.log",
    "pronounce": Path.home() / "Library/Logs/pronounce-sidecar.out.log",
    "radiogen": Path.home() / "Library/Logs/radiogen-sidecar.out.log",
    "maisi": Path.home() / "Library/Logs/maisi-sidecar.out.log",
    "medner": Path.home() / "Library/Logs/medner-sidecar.out.log",
    "colpali": Path.home() / "Library/Logs/colpali-sidecar.out.log",
    "tabfm": Path.home() / "Library/Logs/tabfm-sidecar.out.log",
    "rerank": Path.home() / "Library/Logs/rerank-sidecar.out.log",
    "pyannote": Path.home() / "Library/Logs/pyannote-sidecar.out.log",
    "audio-llm": Path.home() / "Library/Logs/audio-llm-sidecar.out.log",
}
# launchd labels — used by (c) /force-stop --hard to kickstart -k a wedged sidecar.
SIDECAR_LABELS = {
    "omnivoice": "io.macstudio.hub.omnivoice",
    "kokoro": "io.macstudio.hub.kokoro",
    "dia": "io.macstudio.hub.dia",
    "whisper": "io.macstudio.hub.whisper",
    "pronounce": "io.macstudio.hub.pronounce",
    "llamacpp": "io.macstudio.hub.llamacpp",
    "fastmlx": "io.macstudio.hub.fastmlx",
    "mlxlm": "io.macstudio.hub.mlxlm",
    "radiogen": "io.macstudio.hub.radiogen",
    "maisi": "io.macstudio.hub.maisi",
    "medner": "io.macstudio.hub.medner",
    "colpali": "io.macstudio.hub.colpali",
    "tabfm": "io.macstudio.hub.tabfm",
    "rerank": "io.macstudio.hub.rerank",
    "pyannote": "io.macstudio.hub.pyannote",
    "audio-llm": "io.macstudio.hub.audio-llm",
}

# ---------- LLM admission gate (the request-path queue) ----------
# Global memory budget for co-resident LLMs: keep total LLM-resident under the cliff,
# minus a headroom cushion. One pool across Ollama + mlxlm + llamacpp (unified memory).
LLM_HEADROOM_GB = float(os.environ.get("ATELIER_LLM_HEADROOM_GB", "3"))
LLM_BUDGET_GB = float(os.environ.get("ATELIER_LLM_BUDGET_GB", str(CLIFF_GB - LLM_HEADROOM_GB)))
LLM_NUM_PARALLEL = int(os.environ.get("OLLAMA_NUM_PARALLEL", "1"))
LLM_LEASE_TTL_S = float(os.environ.get("ATELIER_LLM_LEASE_TTL_S", "900"))
LLM_DEFAULT_EST_GB = float(os.environ.get("ATELIER_LLM_DEFAULT_EST_GB", "18"))
# Seed estimates for non-Ollama backends / before /api/tags is cached. Substring match.
_EST_OVERRIDES = {
    "qwen3-coder-next": 50.0, "deepseek-r1:70b": 43.0,
    # fastcontext-{rl,sft}: 4B Qwen3 GGUF, alias has no "4b" so the name heuristic
    # falls to the 18GB blind default → spurious admit-hang. Real resident ~13GB @ 64K ctx.
    "fastcontext": 13.0,
}
LLM_LIVE_FLOOR_GB = float(os.environ.get("ATELIER_LLM_LIVE_FLOOR_GB", "4"))
gate = admission.Gate(budget_gb=LLM_BUDGET_GB, default_est_gb=LLM_DEFAULT_EST_GB,
                      num_parallel=LLM_NUM_PARALLEL, est_overrides=_EST_OVERRIDES,
                      default_ttl_s=LLM_LEASE_TTL_S, cliff_gb=CLIFF_GB,
                      live_floor_gb=LLM_LIVE_FLOOR_GB,
                      default_est_ctx=int(os.environ.get("OLLAMA_CONTEXT_LENGTH", "16384")))

_state = {
    "updated_at": None, "level": "ok", "free_gb": None, "resident_gb": None,
    "swapouts": None, "tenants": [], "alerts": [],
    "auto_action": None,      # (d) last auto make-room the watcher ran
    "recommendation": None,   # (d) force-stop the agent should surface for human authorization
}
_last_auto = 0.0   # (d) cooldown clock for auto make-room
_cold_heavy_since: dict = {}   # (g) name -> monotonic ts a sidecar first went cold-but-heavy
_autoheal_log = collections.deque(maxlen=20)   # (g) recent auto-heal restarts (surfaced in /pressure)
_maxidle_log = collections.deque(maxlen=20)    # (i) recent hard-idle-ceiling evictions
_warm_exempt: dict = {}                        # (i) name -> {reason, owner, added}
_warm_exempt_at = 0.0
_warm_exempt_err: str | None = None
_recent_calls = collections.deque(maxlen=50)    # Ollama API calls (from ollama log + proxy)
_proxy_recent = collections.deque(maxlen=50)    # (path, ts) the capturing proxy recorded — dedup vs log tail
_internal_skip = collections.deque(maxlen=50)   # (path, ts) the governor's OWN probe fired — skip its GIN line
# How long after a probe marker a matching GIN line is still considered "ours". Must exceed
# a cold model load, or the probe's own log line is mistaken for user traffic.
PROBE_SKIP_WINDOW_S = float(os.environ.get("ATELIER_PROBE_SKIP_WINDOW_S", "90"))
_recent_events = collections.deque(maxlen=50)   # Ollama lifecycle events
# Durable task history — survives governor restarts (the in-memory deques alone
# cleared on every restart, so Ollama tasks vanished from the dashboard).
_HISTORY_PATH = Path.home() / ".atelier" / "telemetry-history.json"
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

# ---------- real per-sidecar memory (RSS of the process + its model-holding children) ----------
def _proc_table() -> list[tuple]:
    """(pid, ppid, rss_kb, command) for every process — one ps call."""
    try:
        out = subprocess.run(["ps", "-axo", "pid=,ppid=,rss=,command="],
                             capture_output=True, text=True, timeout=4).stdout
    except Exception:
        return []
    procs = []
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        try:
            procs.append((int(parts[0]), int(parts[1]), int(parts[2]), parts[3]))
        except ValueError:
            continue
    return procs


def _subtree_rss_gb(root_pid: int, procs: list[tuple]) -> float:
    """Sum RSS of a process and ALL its descendants (so a proxy sidecar's llama-server /
    mlx child — which actually holds the model — is counted)."""
    kids: dict[int, list[int]] = {}
    rss: dict[int, int] = {}
    for pid, ppid, r, _ in procs:
        kids.setdefault(ppid, []).append(pid)
        rss[pid] = r
    total, stack, seen = 0, [root_pid], set()
    while stack:
        x = stack.pop()
        if x in seen:
            continue
        seen.add(x)
        total += rss.get(x, 0)
        stack.extend(kids.get(x, []))
    return round(total / 1048576, 1)   # KB → GB


def _sidecar_rss_gb(name: str, procs: list[tuple]) -> float | None:
    """Real resident GB for a sidecar: find its listening process (by --port from
    SIDECAR_BASE, or the <name>-sidecar path) and sum its process subtree."""
    base = SIDECAR_BASE.get(name, "")
    port = base.rsplit(":", 1)[-1] if ":" in base else ""
    for pid, _ppid, _r, cmd in procs:
        if (port and f"--port {port}" in cmd) or f"{name}-sidecar" in cmd:
            return _subtree_rss_gb(pid, procs)
    return None


def _top_procs(procs: list[tuple], n: int = 8) -> list[dict]:
    """Top-N host processes by RSS (GB, descending) from an EXISTING _proc_table()
    snapshot — the 'where is the resident memory actually sitting' clue, computed for
    free off the poll's own ps call. The baseline is death-by-a-thousand-cuts (no single
    culprit), so this surfaces the heaviest few so a discretionary hog can be spotted."""
    rows = sorted(procs, key=lambda p: p[2], reverse=True)[:max(1, n)]
    out = []
    for pid, _ppid, rss_kb, cmd in rows:
        name = cmd.split()[0].rsplit("/", 1)[-1] if cmd else str(pid)
        out.append({"pid": pid, "gb": round(rss_kb / 1048576, 2), "name": name})
    return out


# ---------- (h) HONEST INVENTORY — process ground truth vs API self-report ----------
# The blind spot this closes: /api/ps and /readyz are SELF-REPORTS. A runner that is
# mid-load, orphaned, or wedged holds GB of RAM while its API cheerfully says "nothing
# loaded" — and the only symptom is the machine being full. Ground truth is the process
# table. /inventory attributes real RSS to an owner, names the model, and states exactly
# how to free it; anything the APIs disagree with is FLAGGED, never silently dropped.
INVENTORY_MIN_GB = float(os.environ.get("ATELIER_INVENTORY_MIN_GB", "0.4"))
OLLAMA_MANIFESTS = Path(os.environ.get(
    "OLLAMA_MANIFESTS", str(Path.home() / ".ollama/models/manifests")))
_blob_index: dict[str, str] = {}     # blob sha (hex) -> "model:tag"
_blob_index_at = 0.0


def _ollama_blob_index(max_age_s: float = 300.0) -> dict[str, str]:
    """Map an Ollama blob sha → 'model:tag' by reading the manifest tree.

    A running `llama-server --model .../blobs/sha256-<hex>` identifies its model ONLY by
    content hash — useless in a status readout. This is the reverse lookup that turns that
    hash back into a name, so an untracked runner can still be named (and killed by name).
    Cached; the manifest tree only changes on pull/rm."""
    global _blob_index, _blob_index_at
    if _blob_index and time.time() - _blob_index_at < max_age_s:
        return _blob_index
    idx: dict[str, str] = {}
    try:
        for mf in OLLAMA_MANIFESTS.rglob("*"):
            if not mf.is_file():
                continue
            parts = mf.relative_to(OLLAMA_MANIFESTS).parts   # <registry>/<ns>/<name>/<tag>
            if len(parts) < 4:
                continue
            ns, name, tag = parts[-3], parts[-2], parts[-1]
            label = f"{name}:{tag}" if ns == "library" else f"{ns}/{name}:{tag}"
            try:
                # `layers` can be absent OR explicitly null in a manifest — one bad file
                # must not truncate the whole index (it silently did, at 5 of 25 models).
                layers = json.loads(mf.read_text()).get("layers") or []
                for lay in layers:
                    if str(lay.get("mediaType", "")).endswith(".image.model"):
                        idx[str(lay.get("digest", "")).split(":")[-1]] = label
            except Exception:
                continue
    except Exception as e:
        print(f"[governor] blob index failed: {e}", flush=True)
    _blob_index, _blob_index_at = idx, time.time()
    return idx


_RUNNER_HINTS = ("llama-server", "ollama runner", "mlx_lm", "mlx-lm", "vllm",
                 "ComfyUI", "comfy", "llama-cpp", "llama_cpp")


def _runner_procs(procs: list[tuple]) -> list[dict]:
    """Every process that looks like it is HOLDING A MODEL, named where possible.

    Sidecar subtrees are excluded by the caller — this is for runners that belong to no
    sidecar (Ollama's own llama-server, a hand-started mlx_lm, ComfyUI)."""
    idx = _ollama_blob_index()
    out = []
    for pid, ppid, rss_kb, cmd in procs:
        if not any(h in cmd for h in _RUNNER_HINTS):
            continue
        gb = round(rss_kb / 1048576, 2)
        if gb < INVENTORY_MIN_GB:
            continue
        model = None
        m = re.search(r"blobs/sha256[-:]([0-9a-f]{12,})", cmd)
        if m:
            model = idx.get(m.group(1)) or f"sha256:{m.group(1)[:12]}… (no manifest)"
        elif (m2 := re.search(r"--model[= ]+(\S+)", cmd)):
            model = m2.group(1).rsplit("/", 1)[-1]
        port = None
        if (m3 := re.search(r"--port[= ]+(\d+)", cmd)):
            port = int(m3.group(1))
        kind = ("ollama-runner" if "llama-server" in cmd or "ollama runner" in cmd
                else "comfyui" if "omfy" in cmd else "runner")
        out.append({"pid": pid, "ppid": ppid, "gb": gb, "kind": kind,
                    "model": model, "port": port,
                    "exe": cmd.split()[0].rsplit("/", 1)[-1]})
    return out


def _sidecar_pid(name: str, procs: list[tuple]) -> int | None:
    base = SIDECAR_BASE.get(name, "")
    port = base.rsplit(":", 1)[-1] if ":" in base else ""
    for pid, _ppid, _r, cmd in procs:
        if (port and f"--port {port}" in cmd) or f"{name}-sidecar" in cmd:
            return pid
    return None


def _descendants(root_pid: int, procs: list[tuple]) -> set[int]:
    kids: dict[int, list[int]] = {}
    for pid, ppid, _r, _c in procs:
        kids.setdefault(ppid, []).append(pid)
    seen, stack = set(), [root_pid]
    while stack:
        x = stack.pop()
        if x in seen:
            continue
        seen.add(x)
        stack.extend(kids.get(x, []))
    return seen


async def build_inventory(client: httpx.AsyncClient) -> dict:
    """Reconcile PROCESS RSS (truth) against /api/ps + /readyz (self-report).

    Every holder gets a `tracked` verdict:
      both         — process and API agree (the healthy case)
      process-only — RAM is held but NO API admits it  ← the invisible-model bug
      api-only     — an API claims loaded but no process backs it (stale self-report)
    and an `unload` field: the exact call that frees it, or null + why not."""
    vm = read_vm()
    procs = _proc_table()
    holders: list[dict] = []
    claimed_pids: set[int] = set()
    exempt = warm_exemptions()

    # ---- 1. sidecars: RSS truth vs /readyz claim ----
    for name, base in SIDECAR_BASE.items():
        pid = _sidecar_pid(name, procs)
        if pid is not None:
            claimed_pids |= _descendants(pid, procs)
        rss = _sidecar_rss_gb(name, procs)
        try:
            d = (await client.get(f"{base}/readyz", timeout=3)).json()
        except Exception:
            d = None
        # Framework sidecars report `lifecycle` (cold|idle|busy); bespoke ones only
        # `state` (warm|cold) — normalise both, and never call a live sidecar unreachable.
        state = (d or {}).get("lifecycle")
        if state is None and d is not None:
            state = "busy" if d.get("busy") else ("idle" if d.get("state") == "warm" else "cold")
        state = state or "unreachable"
        cold_rss = (d or {}).get("cold_rss_gb")
        floor = cold_rss if cold_rss is not None else 0.3
        claims_loaded = state in ("idle", "busy")
        # Two different bars. "Is a model resident" is a small delta over the cold baseline —
        # kokoro's whole model is only ~0.3 GB, so a coarse margin called it a phantom.
        # "Is this a LEAK" needs a much bigger one: a cold sidecar's idle interpreter can sit
        # near a GB without holding any model, and that must not raise an alarm.
        holds_model = (rss or 0) > floor + 0.2
        leaking = not claims_loaded and (rss or 0) > max(floor + 1.0, 1.0)
        if not claims_loaded and (rss or 0) <= 0.5 and state != "unreachable":
            continue                      # cold and holding nothing — not a memory holder
        tracked = ("process-only" if leaking else
                   "api-only" if claims_loaded and not holds_model else "both")
        busy = state == "busy"
        holders.append({
            "owner": name, "tenant": "atelier", "kind": "sidecar", "pid": pid,
            "model": (d or {}).get("model"), "gb": rss, "state": state,
            "busy": busy, "keep_warm": bool((d or {}).get("keep_warm")),
            "idle_s": (d or {}).get("idle_seconds", (d or {}).get("idle_s")),
            # (i) why this one is allowed to sit warm past the ceiling — or None, meaning
            # it isn't and the governor will evict it.
            "exempt_reason": (exempt.get(name) or {}).get("reason"),
            "cold_rss_gb": cold_rss, "tracked": tracked,
            "note": (f"reports cold but holds {rss} GB — leaked or mid-load"
                     if tracked == "process-only" else
                     "reports loaded but RSS is at cold baseline" if tracked == "api-only" else None),
            "unload": (None if busy else f"POST /unload {{\"target\": \"{name}\"}}"),
            "unload_blocked": ("busy — use /force-stop (human-gated)" if busy else None),
        })

    # ---- 2. Ollama: /api/ps claim vs live llama-server runners ----
    try:
        ps_models = (await client.get(f"{OLLAMA_URL}/api/ps", timeout=3)).json().get("models", [])
    except Exception:
        ps_models = []
    runners = [r for r in _runner_procs(procs) if r["pid"] not in claimed_pids]
    generating = _ollama_recently_active()
    matched_runners: set[int] = set()
    for m in ps_models:
        nm = m.get("name") or ""
        api_gb = round(m.get("size", 0) / 1e9, 2)
        free_runners = [r for r in runners if r["pid"] not in matched_runners]
        run = next((r for r in free_runners
                    if r["model"] == nm or (r["model"] or "").split(":")[0] == nm.split(":")[0]),
                   None)
        if run is None and free_runners and api_gb:
            # Name match failed (unresolvable blob, alias, or a manifest we can't read).
            # An unpaired runner whose RSS is close to the API's reported size is the SAME
            # model — pair it, or the one model is counted twice and attributed_gb exceeds
            # actual resident memory.
            best = min(free_runners, key=lambda r: abs(r["gb"] - api_gb))
            if abs(best["gb"] - api_gb) <= max(2.0, 0.35 * api_gb):
                run = best
        if run:
            matched_runners.add(run["pid"])
        holders.append({
            "owner": "ollama", "tenant": "ollama", "kind": "llm", "pid": (run or {}).get("pid"),
            "model": nm, "gb": run["gb"] if run else round(m.get("size", 0) / 1e9, 2),
            "api_gb": round(m.get("size", 0) / 1e9, 2),
            "state": "busy" if generating else "idle", "busy": generating,
            "context": m.get("context"), "expires_at": m.get("expires_at"),
            "tracked": "both" if run else "api-only",
            "note": None if run else "in /api/ps but no llama-server process — stale entry",
            "unload": f"POST /unload {{\"target\": \"ollama:{nm}\"}}",
            "unload_blocked": ("generating — unload takes effect after the current call"
                               if generating else None),
        })
    # runners with NO /api/ps entry — the invisible ones. This is the case that only ever
    # showed up as "the machine is full."
    for r in runners:
        if r["pid"] in matched_runners:
            continue
        holders.append({
            "owner": r["kind"], "tenant": "ollama" if r["kind"] == "ollama-runner" else "unmanaged",
            "kind": r["kind"], "pid": r["pid"], "model": r["model"], "gb": r["gb"],
            "state": "resident", "busy": None, "tracked": "process-only",
            "note": "HOLDS RAM BUT NO API REPORTS IT — mid-load, orphaned, or unmanaged runner",
            "unload": f"POST /unload {{\"target\": \"pid:{r['pid']}\", \"confirm\": true}}",
            "unload_blocked": None,
        })
        claimed_pids.add(r["pid"])

    # ---- 3. everything else heavy — so nothing is invisible ----
    attributed = round(sum(h["gb"] or 0 for h in holders), 1)
    others = sorted(
        ({"pid": p, "gb": round(rk / 1048576, 2),
          "name": c.split()[0].rsplit("/", 1)[-1]}
         for p, _pp, rk, c in procs
         if p not in claimed_pids and rk / 1048576 >= max(INVENTORY_MIN_GB, 0.4)),
        key=lambda x: x["gb"], reverse=True)[:10]

    holders.sort(key=lambda h: h["gb"] or 0, reverse=True)
    flagged = [h for h in holders if h["tracked"] != "both"]
    return {
        "ok": True,
        "free_gb": vm["free_gb"], "resident_gb": vm["resident_gb"], "swapouts": vm["swapouts"],
        "attributed_gb": attributed,
        # Everything not attributed to a model holder: apps, the OS, kernel + the compressor.
        # Much of it never appears in `ps`, so other_processes[] won't sum to it.
        "other_gb": round(max(0.0, vm["resident_gb"] - attributed), 1),
        "holders": holders,
        "flagged": [{"owner": h["owner"], "model": h["model"], "gb": h["gb"],
                     "tracked": h["tracked"], "note": h["note"]} for h in flagged],
        "other_processes": others,
        "max_idle": {"enabled": MAX_IDLE_ENFORCE, "ceiling_s": MAX_IDLE_S,
                     "exempt": exempt, "exempt_file": str(WARM_EXEMPT_FILE),
                     "exempt_error": _warm_exempt_err, "recent": list(_maxidle_log)},
        "legend": {"both": "process + API agree",
                   "process-only": "RAM held, no API admits it",
                   "api-only": "API claims loaded, no process backs it"},
    }


# ---------- async pollers ----------
async def poll_ollama(client: httpx.AsyncClient) -> list[dict]:
    try:
        r = await client.get(f"{OLLAMA_URL}/api/ps", timeout=3)
        models = r.json().get("models", [])
        # A model in /api/ps is RESIDENT; `expires_at` is just its keep-alive expiry, NOT
        # a "generating now" signal (every loaded model has one). Real activity comes from
        # the log tail: a /api/chat|generate call within the last 15s. So:
        #   generating → "busy" (the dashboard pulses), else loaded-and-idle → "idle".
        generating = _ollama_recently_active()
        return [{
            "tenant": "ollama", "name": m.get("name"),
            "mem_gb": round(m.get("size", 0) / 1e9, 1),
            "context": m.get("context"),
            "state": "busy" if generating else "idle",
            "expires_at": m.get("expires_at"),   # surfaced so the UI can show "loaded until …"
        } for m in models]
    except Exception:
        return []

async def poll_sidecar(client: httpx.AsyncClient, name: str, url: str) -> dict:
    try:
        d = (await client.get(url, timeout=3)).json()
        governed = ("reclaim_margin_gb" in d) or ("cold_rss_gb" in d)  # on the GovernedSidecar framework
        # Framework sidecars emit `lifecycle` (cold|idle|busy). Bespoke ones (radiogen, rerank,
        # pyannote, maisi…) only emit `state` (warm|cold) — defaulting those to "cold" reported
        # a WARM model as unloaded, i.e. memory the governor held but never showed.
        lifecycle = d.get("lifecycle")
        if lifecycle is None:
            lifecycle = "busy" if d.get("busy") else ("idle" if d.get("state") == "warm" else "cold")
        return {"tenant": "atelier", "name": name,
                "state": lifecycle,
                "active_jobs": d.get("active_jobs", 0),
                "queue_depth": d.get("queue_depth", 0),
                # constitution surface for the dashboard:
                "keep_warm": bool(d.get("keep_warm")),           # WARM TAG (allowed to stay resident)
                "active_elapsed_s": d.get("active_elapsed_s"),   # how long the current job has run
                "device": d.get("device"),                       # mps|cuda|mlx|coreml|remote
                "model": d.get("model"),                         # currently-loaded model (None=cold)
                "available_models": d.get("available_models"),   # multi-model lanes (llamacpp menu)
                "cold_rss_gb": d.get("cold_rss_gb"),             # baseline (reclaim floor)
                # seconds since this sidecar's last request — the input to the hard idle
                # ceiling below. Bespoke sidecars name it differently or omit it entirely.
                "idle_seconds": d.get("idle_seconds", d.get("idle_s")),
                "governed": governed}                            # framework-managed vs bespoke
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


def _autoheal_check(tenants: list[dict]) -> None:
    """(g) Enforce Law 1 from OUTSIDE. If an atelier sidecar reports state=cold (model unloaded) yet
    still holds > AUTOHEAL_FLOOR_GB for longer than AUTOHEAL_GRACE_S, it leaked — hard-restart it via
    launchd (kickstart -k). Self-healing (GovernedSidecar restart-reclaim) fixes it first; this is the
    backstop so even a bespoke / non-self-healing sidecar can't hold leaked memory indefinitely."""
    if not AUTOHEAL:
        return
    now = time.monotonic()
    for t in tenants:
        if t.get("tenant") != "atelier":
            continue
        name = t.get("name")
        mem = t.get("mem_gb") or 0.0
        leaking = t.get("state") == "cold" and mem > AUTOHEAL_FLOOR_GB
        if not leaking:
            _cold_heavy_since.pop(name, None)
            continue
        since = _cold_heavy_since.get(name)
        if since is None:
            _cold_heavy_since[name] = now
            continue
        if now - since < AUTOHEAL_GRACE_S:
            continue
        dur = int(now - since)
        _cold_heavy_since.pop(name, None)
        label = SIDECAR_LABELS.get(name)
        if not label:
            continue
        try:
            subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"],
                           check=True, capture_output=True, timeout=15)
            print(f"[governor] AUTOHEAL restarted {name}: state=cold but held {mem}GB "
                  f"> {AUTOHEAL_FLOOR_GB}GB for {dur}s — kickstart -k", flush=True)
            _autoheal_log.append({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "name": name,
                                  "held_gb": mem, "held_s": dur, "action": "kickstart -k"})
        except Exception as e:
            print(f"[governor] AUTOHEAL kickstart {name} failed: {e}", flush=True)


def warm_exemptions(max_age_s: float = 30.0) -> dict:
    """(i) The registry of LEGITIMATE long-warm cases: name -> {reason, owner, added}.

    This is the honest half of the ceiling. There are real reasons to hold a model past
    30 idle minutes (an ASR sidecar fronting an interactive lesson, a TTS engine mid-
    podcast-run), and this is where such a case gets WRITTEN DOWN — with a reason, an
    owner, and a date — instead of hiding behind a plist flag nobody remembers setting.

    A `reason` is MANDATORY. An entry without a non-empty reason is ignored and reported
    as invalid: "someone set a flag" is exactly the state this is meant to eliminate."""
    global _warm_exempt, _warm_exempt_at, _warm_exempt_err
    if _warm_exempt and time.time() - _warm_exempt_at < max_age_s:
        return _warm_exempt
    out, err = {}, None
    try:
        if WARM_EXEMPT_FILE.exists():
            raw = json.loads(WARM_EXEMPT_FILE.read_text())
            for name, v in (raw or {}).items():
                if name.startswith("_"):
                    continue          # _comment / _example — documentation, not an exemption
                if isinstance(v, str):
                    v = {"reason": v}
                reason = (v or {}).get("reason", "").strip()
                if not reason:
                    err = f"{name}: no reason given — exemption ignored"
                    continue
                out[name] = {"reason": reason, "owner": (v or {}).get("owner"),
                             "added": (v or {}).get("added"),
                             "expires": (v or {}).get("expires")}
    except Exception as e:
        err = f"unreadable {WARM_EXEMPT_FILE}: {e}"
    _warm_exempt, _warm_exempt_at, _warm_exempt_err = out, time.time(), err
    return out


async def _max_idle_check(client: httpx.AsyncClient, tenants: list[dict]) -> None:
    """(i) Enforce the hard idle ceiling. Unload any atelier sidecar idle past MAX_IDLE_S
    REGARDLESS of its keep_warm tag; if the unload doesn't take, kick it via launchd.

    Never touches a busy sidecar or one with callers queued — the ceiling is about
    abandoned residency, not preemption."""
    if not MAX_IDLE_ENFORCE:
        return
    exempt = warm_exemptions()
    for t in tenants:
        if t.get("tenant") != "atelier" or t.get("state") != "idle":
            continue
        if t.get("busy") or (t.get("active_jobs") or 0) > 0 or (t.get("queue_depth") or 0) > 0:
            continue
        name = t.get("name")
        idle_s = t.get("idle_seconds")
        mem = t.get("mem_gb") or 0.0
        if idle_s is None:
            # A sidecar that won't say when it was last used can't be judged on idleness.
            # Flag it rather than guess — silence is not consent to keep the RAM.
            t["max_idle"] = "unknown (sidecar reports no idle_seconds)"
            continue
        if idle_s <= MAX_IDLE_S or mem < MAX_IDLE_MIN_GB:
            continue
        if name in exempt:
            t["max_idle"] = "exempt"
            t["exempt_reason"] = exempt[name]["reason"]
            continue
        acted, how = False, None
        try:
            base = SIDECAR_BASE.get(name)
            r = (await client.post(f"{base}/admin/unload", timeout=15)).json()
            if r.get("refused") == "busy":
                continue                       # raced with a new job — leave it alone
            acted, how = True, "admin/unload"
        except Exception as e:
            how = f"unload failed ({e})"
        if not acted:
            # "switched off OR kicked" — a sidecar with no working unload still gets freed.
            label = SIDECAR_LABELS.get(name)
            if label:
                try:
                    subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"],
                                   check=True, capture_output=True, timeout=15)
                    acted, how = True, f"kickstart -k ({how})"
                except Exception as e:
                    how = f"{how}; kickstart failed ({e})"
        print(f"[governor] MAX-IDLE {'evicted' if acted else 'FAILED on'} {name}: "
              f"idle {int(idle_s)}s > {int(MAX_IDLE_S)}s ceiling, held {mem}GB "
              f"(keep_warm={t.get('keep_warm')}) — {how}", flush=True)
        _maxidle_log.append({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "name": name,
                             "idle_s": int(idle_s), "held_gb": mem,
                             "keep_warm": bool(t.get("keep_warm")),
                             "action": how, "ok": acted})


async def _poller():
    async with httpx.AsyncClient() as client:
        while True:
            try:
                vm = read_vm()
                tenants = await poll_ollama(client)
                for name, url in SIDECARS.items():
                    tenants.append(await poll_sidecar(client, name, url))
                # annotate warm sidecars with their REAL measured RSS (donut/top show truth,
                # not the dashboard's hardcoded SIDECAR_MEM guesses)
                procs = _proc_table()
                for t in tenants:
                    # Measure RSS for EVERY atelier sidecar incl. COLD ones — a cold sidecar still
                    # holding memory IS the leak we must see (and auto-heal). Only skip unreachable.
                    if (t.get("tenant") == "atelier" and not t.get("mem_gb")
                            and t.get("state") not in (None, "unreachable")):
                        rss = _sidecar_rss_gb(t["name"], procs)
                        if rss:
                            t["mem_gb"] = rss
                # The governor NEVER emits a null memory reading — a "no value" (unmeasured /
                # unreachable / not admitted) is always 0, so no consumer (donut, budget, grid)
                # ever sees null/NaN. "admits no" → 0.
                for t in tenants:
                    if t.get("mem_gb") is None:
                        t["mem_gb"] = 0.0
                _autoheal_check(tenants)   # constitution enforced from OUTSIDE: cold-but-heavy → restart
                await _max_idle_check(client, tenants)   # (i) hard 30-min idle ceiling, warm tag or not
                # heaviest resident first, top-down (cold sidecars → mem 0 → sink to the bottom)
                tenants.sort(key=lambda t: t.get("mem_gb") or 0.0, reverse=True)
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
                # ---- feed the admission gate: live memory + model sizes + reconcile ----
                await _refresh_gate(client, vm, tenants)
                # ---- DYNAMIC budget: the LLM ceiling = cliff − headroom − non-LLM baseline,
                # recomputed every poll (the baseline breathes as apps open/close). This is
                # what makes admission honest: "free budget" now tracks the REAL room to the
                # cliff instead of a flat 52GB that ignored the ~24GB macOS/app floor.
                committed = gate.committed_gb()                       # LLM leases + untracked
                baseline = max(0.0, round(vm["resident_gb"] - committed, 1))   # non-LLM floor
                effective_budget = max(0.0, round(CLIFF_GB - LLM_HEADROOM_GB - baseline, 1))
                gate.set_budget(effective_budget)
                _state.update({
                    "committed_gb": committed,          # loaded/attributed LLM pressure
                    "baseline_gb": baseline,            # macOS + apps (immovable, non-LLM)
                    "budget_gb": effective_budget,      # live LLM ceiling after subtracting baseline
                    "top_procs": _top_procs(procs, 8),  # where the resident memory actually is
                    "autoheal": list(_autoheal_log),    # (g) recent Law-1 auto-restarts
                    "max_idle": {                       # (i) the hard idle ceiling + who is excused from it
                        "enabled": MAX_IDLE_ENFORCE, "ceiling_s": MAX_IDLE_S,
                        "exempt": warm_exemptions(),    # each with a WRITTEN reason, or it doesn't count
                        "exempt_file": str(WARM_EXEMPT_FILE),
                        "exempt_error": _warm_exempt_err,
                        "recent": list(_maxidle_log),
                    },
                    "constitution": {                   # the Atelier framework, surfaced for the dashboard
                        "laws": ["no memory leaks", "never unload an actively-working model",
                                 "queue calls under pressure",
                                 "nothing stays resident past the idle ceiling without a written reason"],
                        "autoheal": {"enabled": AUTOHEAL, "floor_gb": AUTOHEAL_FLOOR_GB,
                                     "grace_s": AUTOHEAL_GRACE_S},
                        "max_idle": {"enabled": MAX_IDLE_ENFORCE, "ceiling_s": MAX_IDLE_S},
                        "cliff_gb": CLIFF_GB, "warn_gb": WARN_GB},
                })
            except Exception as e:
                print(f"[governor] poll error: {e}", flush=True)
            await asyncio.sleep(POLL_SECONDS)


# Phase 4: which backends the gate can route across, and where to reach them.
LLM_ROUTE_BASE = {"ollama": OLLAMA_URL,
                  "mlxlm": SIDECAR_BASE.get("mlxlm", ""),
                  "llamacpp": SIDECAR_BASE.get("llamacpp", "")}
_tags_last = 0.0
_sidecar_models: dict[str, str] = {}   # backend → the single model it currently serves


async def _refresh_gate(client: httpx.AsyncClient, vm: dict, tenants: list[dict]):
    """Keep the admission gate's view of the world current each poll:
      - live measured memory (the HARD crash backstop — beats est accounting),
      - model-weight sizes from Ollama /api/tags + backend catalogs (refreshed lazily),
      - untracked GB = models loaded with no lease (bypass-honest),
      - loaded set for Phase-4 routing (prefer an already-resident copy),
      - reap leases whose client died without /release."""
    global _tags_last
    # (a) live memory floor — the real safety net against the RAM-overload crash
    gate.set_live(resident_gb=vm.get("resident_gb"), free_gb=vm.get("free_gb"))
    # (b) refresh model sizes + backend catalogs at most every 60s
    now = time.time()
    if now - _tags_last > 60:
        catalog: dict[str, list[str]] = {}
        try:
            tags = (await client.get(f"{OLLAMA_URL}/api/tags", timeout=4)).json().get("models", [])
            gate.set_tags({m["name"]: round(m.get("size", 0) / 1e9, 2) for m in tags})
            catalog["ollama"] = [m["name"] for m in tags]
        except Exception:
            pass
        for be in ("mlxlm", "llamacpp"):       # each LLM sidecar serves one configured model
            try:
                d = (await client.get(f"{SIDECAR_BASE[be]}/readyz", timeout=3)).json()
                if d.get("model"):
                    _sidecar_models[be] = d["model"]
                    catalog[be] = [d["model"]]
            except Exception:
                pass
        gate.set_catalog(catalog, LLM_ROUTE_BASE)
        _tags_last = now
    # (c) untracked load = models loaded with NO active lease (bypassed the gate).
    loaded = [{"backend": "ollama", "model": t.get("name"), "gb": t.get("mem_gb", 0.0)}
              for t in tenants if t.get("tenant") == "ollama" and t.get("name")]
    # sidecars never take a lease, so their measured RSS is pure untracked load — count it,
    # else /budget under-reports (the 21GB-omnivoice blind spot) and the budget path is blind.
    loaded += [{"backend": "atelier", "model": t.get("name"), "gb": t.get("mem_gb", 0.0)}
               for t in tenants if t.get("tenant") == "atelier" and t.get("mem_gb")]
    gate.set_untracked_gb(gate.untracked_from(loaded))
    # (d) loaded set for routing: Ollama resident models + any warm LLM sidecar
    loaded_set = {("ollama", t["name"]) for t in tenants
                  if t.get("tenant") == "ollama" and t.get("name")}
    for be in ("mlxlm", "llamacpp"):
        st = next((t.get("state") for t in tenants if t.get("name") == be), None)
        if be in _sidecar_models and st not in (None, "cold", "unreachable"):
            loaded_set.add((be, _sidecar_models[be]))
    gate.set_loaded(loaded_set)
    # (e) warm KV rate for loaded Ollama models so DIRECT /admit callers also get accurate
    # est (cached after first fetch — just dict hits thereafter).
    for x in loaded:
        if x["model"] not in _ollama_meta_cache:
            await _ollama_meta(x["model"])
    # (f) backstop reaper for dead clients
    await gate.reap(now)

# ---------- Ollama log tailer ----------
_GIN = re.compile(r'^\[GIN\]\s+(?P<date>\d{4}/\d{2}/\d{2})\s+-\s+(?P<time>\d{2}:\d{2}:\d{2})\s+\|\s*(?P<status>\d+)\s*\|\s*(?P<lat>[\d.a-zµ]+)\s*\|\s*(?P<client>\S+)\s*\|\s*(?P<method>\w+)\s+"(?P<path>[^"]+)"')
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
        # WHO made the call. A GIN line always carried the client IP and we threw it away,
        # so a call from the LAN gateway was indistinguishable from one the user made here —
        # and both showed an empty prompt/output, since a direct :11434 call never passes
        # through the capturing proxy. Naming the client turns "why is this blank?" into
        # "that came from a LAN host, which bypasses the governor."
        client = m.group("client")
        entry = {"at": log_time, "ts": ts,
                 "status": m.group("status"), "latency": m.group("lat"),
                 "path": m.group("path"), "client": client,
                 "via": "direct-to-ollama",
                 "capture_note": (f"called ollama directly on :11434 from {client} — the governor "
                                  "only sees the access-log line (timing), so the prompt and reply "
                                  "were never captured. Route via POST /llm/ollama/... to record them.")}
        # Attach latest perf stats (model name + tok/s) if available
        if _ollama_last_stats:
            entry.update(_ollama_last_stats)
        # Dedup by timestamp — restored file-history + log backfill can overlap.
        if any(abs((c.get("ts") or 0) - ts) < 0.5 for c in _recent_calls):
            return
        # The capturing proxy already recorded this call RICHLY (prompt + tokens). The GIN
        # log line is the same call seen plainly — skip it so we keep the rich entry.
        if any(p == m.group("path") and abs(pts - ts) < 2.0 for p, pts in _proxy_recent):
            return
        # The governor's OWN benchmark probe is an /api/generate — recorded separately and
        # labeled. Skip its raw GIN line so it can't masquerade as user traffic (and can't
        # re-trigger the stats watcher into a self-perpetuating probe loop).
        # Window must cover a COLD model load (30B ≈ 5s, larger ones far more), otherwise
        # a slow probe's log line slips past the guard and restarts the feedback loop.
        if any(p == m.group("path") and abs(pts - ts) < PROBE_SKIP_WINDOW_S for p, pts in _internal_skip):
            return
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
                # --- Backfill: parse last 4MB on startup (the /api/ps poll flood
                # pushes real /api/chat|/api/generate calls out of a small window) ---
                f.seek(0, os.SEEK_END)
                size = f.tell()
                backfill_start = max(0, size - 4 * 1024 * 1024)
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
    # Models that answered "does not support generate" — embedding models (bge-m3,
    # nomic-embed…) are a hard no for /api/generate. Without this the watcher re-probed
    # one every COOLDOWN seconds forever: an embedding call counts as a "real call", so
    # a codebase indexer doing thousands of them kept the loop permanently armed. Left
    # unfixed it was ~1400 failed 400s a day, all of them landing in the task stream.
    no_generate: set = set()
    async with httpx.AsyncClient() as client:
        while True:
            await asyncio.sleep(0.5)
            try:
                # Only REAL calls (not our own probes) should trigger a fresh probe. Excluding
                # via="governor-probe" entries is what breaks the self-perpetuating loop that
                # used to fire every PROBE_COOLDOWN seconds and pin the last model resident.
                real_calls = [c for c in _recent_calls if c.get("via") != "governor-probe"]
                if real_calls:
                    latest = real_calls[-1]
                    call_ts = latest.get("ts", 0)
                    mono = time.monotonic()
                    if (call_ts > last_call_ts and str(latest.get("status")) == "200"
                            and mono - last_probe_ts >= PROBE_COOLDOWN):
                        last_call_ts = call_ts
                        last_probe_ts = mono
                        # Find which model is/was loaded
                        ps = await client.get(f"{OLLAMA_URL}/api/ps", timeout=3)
                        models = ps.json().get("models", [])
                        if not models:
                            continue
                        # Probe the model that was actually CALLED — never models[0], which
                        # with several loaded could be an idle one nobody is using, and the
                        # probe would reset its idle timer. No name on the call → only probe
                        # when exactly one model is loaded (unambiguous); otherwise skip.
                        called = str(latest.get("model") or "")
                        if called:
                            target = next((mm for mm in models if called in
                                           (mm.get("name"), mm.get("model"))), None)
                        else:
                            target = models[0] if len(models) == 1 else None
                        if not target:
                            continue
                        model_name = target.get("name", "")
                        # Idle policy (2026-09-14): a model stays warm OLLAMA_KEEP_ALIVE (120 s)
                        # after its last REAL request, and no probe may extend that. Ollama resets
                        # the expiry on every request, so pass keep_alive = the time the model has
                        # LEFT; skip entirely if it is about to expire (a probe must not reload it).
                        try:
                            from datetime import datetime   # not imported at module level here
                            _exp = re.sub(r"(\.\d{6})\d+", r"\1", str(target.get("expires_at") or ""))
                            _left = datetime.fromisoformat(_exp).timestamp() - time.time()
                        except Exception:
                            _left = 0.0
                        if _left < 10:
                            continue
                        probe_keep_alive = f"{int(_left)}s"
                        # Fire a tiny probe (8 tokens) for fresh eval stats. Do NOT pass
                        # keep_alive — the probe must never EXTEND a model's life (policy: don't
                        # keep things warm; fade out and reclaim on demand). It inherits the
                        # global OLLAMA_KEEP_ALIVE, same as the real call it follows, so the model
                        # expires on the normal short timer instead of being pinned by benchmarking.
                        # Mark the skip BEFORE firing. ollama writes its GIN line the moment
                        # the response is sent, and the tailer can read it while this coroutine
                        # is still awaiting — so a marker appended AFTER the call arrived too
                        # late, the probe's own log line was ingested as user traffic, and it
                        # re-triggered this watcher on the next tick. That loop reloaded a 30B
                        # model every ~5 minutes forever and buried the task stream in 39
                        # self-probes out of 50 entries.
                        if model_name in no_generate:
                            continue          # embedding-only: benchmarking it is meaningless
                        _probe_started = time.time()
                        _internal_skip.append(("/api/generate", _probe_started))
                        probe = await client.post(f"{OLLAMA_URL}/api/generate",
                            json={"model": model_name, "prompt": "Hi", "stream": False,
                                  "keep_alive": probe_keep_alive,
                                  "options": {"num_predict": 8}},
                            timeout=30)
                        # Record the probe as a LABELED, visible entry (so the dashboard shows
                        # exactly what the governor is doing) AND re-mark its GIN line: a cold
                        # 30B load can take 30s+, so the completion timestamp may sit far outside
                        # the match window around the start marker.
                        _now = time.time()
                        _internal_skip.append(("/api/generate", _now))
                        _probe_out, _probe_tok = "", None
                        try:
                            _pj = probe.json()
                            _probe_out = _extract_output(_pj)
                            _probe_tok = _pj.get("eval_count")
                            if probe.status_code >= 400:
                                _err = str(_pj.get("error") or _pj)[:300]
                                _probe_out = f"▲ probe failed ({probe.status_code}): {_err}"
                                if "does not support generate" in _err:
                                    no_generate.add(model_name)
                                    print(f"[governor] {model_name} does not support generate — "
                                          f"will not benchmark it again", flush=True)
                        except Exception:
                            pass
                        _recent_calls.append({
                            # the real status: hardcoding 200 made a failing probe look healthy
                            "at": time.strftime("%H:%M:%S"), "ts": _now,
                            "status": str(probe.status_code),
                            "latency": f"{_now - _probe_started:.2f}s",
                            "path": "/api/generate", "model": model_name,
                            "backend": "ollama", "via": "governor-probe",
                            "prompt": f"▣ governor warm-up/benchmark probe for {model_name} (num_predict=8)",
                            # record what came back, so opening this row shows something real
                            # instead of an empty pane
                            "output": _probe_out or "(no text — probe capped at 8 tokens)",
                            "eval_tokens": _probe_tok,
                        })
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

# ---------- durable task history (survives restarts) ----------
def _save_history():
    try:
        _HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        _HISTORY_PATH.write_text(json.dumps({
            "recent_calls": list(_recent_calls),
            "recent_synths": list(_recent_synths),
            "recent_events": list(_recent_events),
        }))
    except Exception as e:
        print(f"[governor] save history failed: {e}", flush=True)

def _load_history():
    try:
        if not _HISTORY_PATH.exists():
            return
        d = json.loads(_HISTORY_PATH.read_text())
        for c in d.get("recent_calls", []): _recent_calls.append(c)
        for s in d.get("recent_synths", []): _recent_synths.append(s)
        for ev in d.get("recent_events", []): _recent_events.append(ev)
        print(f"[governor] restored history: {len(_recent_calls)} calls, "
              f"{len(_recent_synths)} synths", flush=True)
    except Exception as e:
        print(f"[governor] load history failed: {e}", flush=True)

async def _history_saver():
    while True:
        await asyncio.sleep(15)
        _save_history()

_intercept_started = False   # guard: only the primary lifespan may open the second socket


async def _serve_intercept_port():
    """(j) TRANSPARENT CAPTURE — also answer on ollama's own port.

    Opt-in capture never finishes the job: every client that only exposes a "base URL"
    (Cline, LiteLLM, anything embedding-based) keeps finding its way back to :11434, and
    ollama's logs cannot recover a bypassed call — even at --log-verbosity 4 they record
    token COUNTS, never prompt text. The only way to be sure is to BE the port.

    Ollama moves to 127.0.0.1:11435 (loopback-only) and the governor answers on 11434,
    so a call is captured whether or not the client cooperates. Same app, same state,
    second socket — the drop-in /api/* and /v1/* routes already speak ollama's dialect.

    Binds nothing unless ATELIER_INTERCEPT_PORT is set, and a bind failure is logged
    loudly but never fatal: losing the governor must not also take down the hub."""
    port = int(os.environ.get("ATELIER_INTERCEPT_PORT", "0"))
    if not port:
        return
    host = os.environ.get("ATELIER_INTERCEPT_HOST", "0.0.0.0")
    # Self-loop check compares the FULL address, not just the port. Sharing a port number
    # with ollama is the normal case here: ollama binds 127.0.0.1:11434 (Expose off) while
    # we bind the LAN address on the same port, so LAN clients reach us and loopback
    # reaches ollama. Only an identical host AND port would proxy to itself.
    from urllib.parse import urlparse
    up = urlparse(OLLAMA_URL)
    up_host = (up.hostname or "").replace("localhost", "127.0.0.1")
    up_port = up.port or 11434
    if up_port == port and (up_host == host or host == "0.0.0.0"):
        print(f"[governor] INTERCEPT ABORTED: upstream {OLLAMA_URL} is the same address as "
              f"{host}:{port} — that would proxy to itself. Bind the LAN address "
              f"(ATELIER_INTERCEPT_HOST) or move ollama to another port.", flush=True)
        return
    global _intercept_started
    if _intercept_started:
        return
    _intercept_started = True
    import uvicorn
    # lifespan="off" is REQUIRED, not tidiness: serving the same `app` object runs its
    # lifespan again, which starts another intercept listener, which serves the app
    # again — an infinite recursion that spawned servers until the port bind failed.
    # The primary listener already owns startup; this socket only needs to serve.
    cfg = uvicorn.Config(app, host=host, port=port, log_level="warning", lifespan="off")
    server = uvicorn.Server(cfg)
    server.install_signal_handlers = lambda: None    # secondary server: parent owns signals
    # Bind the socket OURSELVES and hand it to uvicorn. Two reasons, both learned the
    # hard way: (1) uvicorn logs a bind failure and calls sys.exit, raising SystemExit —
    # a BaseException that `except OSError` never sees, so the retry silently died;
    # (2) serve() binds internally, so anything printed before it announces success that
    # has not happened yet. Owning the socket makes "active" mean actually listening.
    retry_s = float(os.environ.get("ATELIER_INTERCEPT_RETRY_S", "30"))
    announced_wait = False
    while True:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
            sock.listen(2048)
            sock.setblocking(False)
        except OSError as e:
            sock.close()
            if not announced_wait:
                print(f"[governor] intercept cannot bind {host}:{port} yet ({e}). Ollama "
                      f"still holds it — turn OFF 'Expose Ollama to the network' in "
                      f"Ollama.app so it binds 127.0.0.1 only. Retrying every "
                      f"{int(retry_s)}s; the governor keeps working on its own port.",
                      flush=True)
                announced_wait = True
            await asyncio.sleep(retry_s)
            continue
        try:
            print(f"[governor] TRANSPARENT INTERCEPT listening on {host}:{port} → "
                  f"upstream {OLLAMA_URL} (LAN clients are now captured)", flush=True)
            await server.serve(sockets=[sock])
            return
        except asyncio.CancelledError:
            raise
        except BaseException as e:          # SystemExit included — never kill the governor
            print(f"[governor] intercept listener stopped: {e!r}", flush=True)
            return
        finally:
            try:
                sock.close()
            except Exception:
                pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    _load_history()   # restore task history BEFORE the log tailer adds live ones
    tasks = [asyncio.create_task(_poller()), asyncio.create_task(_log_tailer()),
             asyncio.create_task(_ollama_stats_watcher()), asyncio.create_task(_history_saver()),
             asyncio.create_task(_serve_intercept_port())]
    for nm, p in SIDECAR_LOGS.items():
        tasks.append(asyncio.create_task(_tail_sidecar(nm, p)))
    yield
    _save_history()
    for t in tasks:
        t.cancel()

app = FastAPI(lifespan=lifespan)

@app.get("/healthz")
def healthz():
    return {"ok": True, "service": "governor", "version": "0.8-proxy"}

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
    "pronounce": "Pronunciation scoring — espeak-ng IPA + CUPE-2i phonemes, reference-based (Italian)",
    "llamacpp": "LLM — llama.cpp/llama-server (Metal, GGUF), OpenAI-compatible",
    "fastmlx": "LLM/VLM — FastMLX (MLX-native), OpenAI-compatible [blocked: upstream]",
    "mlxlm": "LLM — Apple mlx_lm.server (MLX-native), OpenAI-compatible",
    "medner": "NER — medical entity extraction (GLiNER + d4data + scispaCy, MPS)",
    "colpali": "Retrieval — ColPali visual-document scoring/embeddings (MPS)",
    "tabfm": "Tabular — Tabular Foundation Models (TabPFN-3 + Google TabFM): predict/fit-cache/embed on parquet",
    "rerank": "Rerank — cross-encoder BAAI/bge-reranker-v2-m3 (query,passages→scores) (MPS)",
    "pyannote": "Diarization — speaker diarization (pyannote.audio): who-spoke-when (MPS)",
    "audio-llm": "Audio understanding — classify/describe an audio track (multi-model: Qwen2-Audio/Voxtral/Qwen3-Omni, MLX)",
}
AGENT_CAPABLE = {"whisper", "omnivoice", "kokoro", "dia", "llamacpp", "fastmlx", "mlxlm", "medner",
                 "tabfm", "colpali", "rerank", "pyannote", "audio-llm", "pronounce"}

# Present on the box but NOT governed sidecars (no admit/unload contract). Surfaced in /agent so
# an agent has the COMPLETE picture — but the governor CANNOT admit/evict these; their memory sits
# in baseline_gb. `probe` is a GET that returns 200 when the service is up (for live-state on expand).
OTHER_SERVICES = {
    "comfyui": {
        "base_url": "http://127.0.0.1:8188",
        "role": "Image/video generation (ComfyUI) — node-graph UI + API",
        "probe": "/system_stats",
        "note": "NOT a governed sidecar: no /admin/unload, so the governor can't idle-evict it and "
                "its VRAM/RAM counts as baseline_gb. Stop it manually if the box is under memory pressure.",
    },
}

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
    live = {t.get("name"): t for t in _state.get("tenants", [])}   # fold in live health (from the poller)
    sidecars = {}
    for name, base in SIDECAR_BASE.items():
        lt = live.get(name, {})
        sidecars[name] = {
            "base_url": base,
            "role": SIDECAR_ROLES.get(name, "sidecar"),
            "state": lt.get("state", "unknown"),   # cold | idle | busy | unreachable
            "mem_gb": lt.get("mem_gb"),
            "readyz": f"{base}/readyz",
            "agent": f"{base}/agent" if name in AGENT_CAPABLE else None,
        }
    other = {n: {"base_url": m["base_url"], "role": m["role"], "governed": False, "note": m["note"]}
             for n, m in OTHER_SERVICES.items()}
    if expand:
        token = os.environ.get("HUB_TOKEN")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        capable = [(n, SIDECAR_BASE[n]) for n in SIDECAR_BASE if n in AGENT_CAPABLE]
        async with httpx.AsyncClient(headers=headers) as client:
            manifests = await asyncio.gather(
                *[_fetch_agent_manifest(client, f"{b}/agent") for _, b in capable]
            )
            for n, m in OTHER_SERVICES.items():   # live-probe the ungoverned apps too
                try:
                    r = await client.get(m["base_url"] + m.get("probe", "/"), timeout=2)
                    other[n]["state"] = "up" if r.status_code < 500 else "down"
                except Exception:
                    other[n]["state"] = "down"
        for (n, _), manifest in zip(capable, manifests):
            sidecars[n]["manifest"] = manifest
    return {
        "service": "atelier-governor",
        "role": "hub supervisor — memory governor, LLM admission gate, telemetry, ETA predictor",
        "summary": "One LAN inference hub on a Mac Studio. The governor watches unified-memory "
                   "pressure across Ollama + sidecars, ADMITS/QUEUES LLM work so jobs share "
                   "memory instead of evicting each other, captures every proxied call, and "
                   "predicts ETAs.",
        "tip": "GET /agent?expand=true to inline every sidecar's full manifest in one fetch.",
        "how_to_start": (
            "1) GET /agent?expand=true — one round-trip: control plane + every sidecar's methods.\n"
            "2) To run an LLM, prefer the SMART FRONT DOOR: POST /llm/{backend}/{path} "
            "(backend = ollama|mlxlm|llamacpp). It admits you into the global memory queue "
            "(waits under pressure instead of OOM-ing or evicting a running model), auto-sizes "
            "the context to your prompt (no silent truncation), captures prompt+tokens, and "
            "forwards. Body = the backend's native schema (Ollama /api/chat or OpenAI "
            "/v1/chat/completions). E.g. POST /llm/ollama/api/chat {model, messages[]}.\n"
            "3) For TTS/ASR call the sidecar directly — see sidecars[] and docs.sidecar_calls.\n"
            "4) GET /pressure or /budget for memory + the queue; GET /estimate for an ETA.\n"
            "5) After a run, POST /report so the predictor sharpens."
        ),
        "llm_access": {
            "recommended": "POST /llm/{backend}/{path}",
            "what_it_does": "memory admission (queues under pressure, never evicts a running "
                            "model on arrival) + auto num_ctx sizing + prompt/token capture + routing",
            "backends": list(LLM_ROUTE_BASE),
            "examples": {
                "ollama": "POST /llm/ollama/api/chat  {model, messages:[...]}",
                "mlxlm/llamacpp (OpenAI)": "POST /llm/mlxlm/v1/chat/completions  {model, messages:[...]}",
            },
            "manual_gate": {
                "POST /admit": "{job_id, model, backend?=auto, est_gb?} → "
                               "grant{lease_id, backend, base_url} | queued{position}",
                "POST /release": "{lease_id|job_id} — drop the lease when the job finishes",
                "note": "only needed if you call a backend DIRECTLY; /llm does admit+release for you",
            },
            "client_helper": "clients/atelier_admit.py — `async with admission(model=..., backend='auto')`",
        },
        "control_plane": {
            "GET /pressure": "live memory level + tenants[] (busy/idle/cold, jobs, queue)",
            "GET /budget": "LLM admission budget — committed vs budget, leases per backend, wait queue",
            "GET /telemetry": "recent calls (prompt+tokens for proxied), synths (incl. [asr]), events",
            "GET /estimate": "ETA for a job — ?engine=whisper&audio_s=N | ?engine=<tts>&chars=N | "
                             "?model=<llm>&out_tokens=N",
            "GET /predictor/stats": "learned per-(kind,model) compute stats",
            "POST /report": "feed a completed run into the predictor",
            "POST /llm/{backend}/{path}": "capturing proxy — the recommended way to run LLMs",
            "POST /admit · POST /release": "manual memory lease (the proxy does this for you)",
            "GET /inventory": "who ACTUALLY holds memory — process RSS vs API self-report, "
                              "with the exact unload call per holder; flags what no API admits",
            "POST /unload": "free one target: {target: '<sidecar>'|'ollama:<model>'|'pid:<n>'}",
            "POST /make-room": "evict ONLY idle models to free memory",
            "POST /force-stop": "human-gated preempt of a BUSY model",
        },
        "sidecars": sidecars,
        "other_services": other,   # present on the box but NOT governed (comfyui, etc.) — see each note
        "context": {
            "why_it_matters": "num_ctx (context window) is a MEMORY decision on the Mac's unified RAM. "
                              "Too SMALL chokes/truncates a local model → bad or cut-off answers. Too BIG "
                              "blows the KV-cache past free memory → the box SWAP-DEATHS (e.g. ornith-35b's "
                              "native 262144 ≈ ~40 GB of KV). NEVER guess a num_ctx by hand.",
            "governed (anything heavy)": "Call THROUGH the governor (POST /llm/{backend}/{path}) and OMIT "
                                         "num_ctx — it auto-sizes to your prompt + live free memory, capped "
                                         "safe (never the naive native max). Point any Ollama client (e.g. "
                                         "Goose) at http://192.168.0.159:8799/llm/ollama and it auto-detects "
                                         "a safe window from the rewritten /api/show — zero manual pinning.",
            "raw :11434 callers": "For calls that bypass the governor, use the `ollama-ctx` dial "
                                  "(~/.local/bin/ollama-ctx: list | global <N> | set <model> <N|max> | "
                                  "research). Global default was raised 16384→131072 (a silent Ollama.app "
                                  "sqlite ceiling); per-model trained-max in ~/.config/ollama-ctx/registry.json.",
            "rule": "The governor is the ONE place that knows live memory — use it for anything heavy and "
                    "you never hand-tune num_ctx again. See R-AG5/R-AG6 in atelier-governor.md.",
        },
        "ollama": {"base_url": OLLAMA_URL, "role": "LLM + embeddings + VLM",
                   "list_loaded": f"{OLLAMA_URL}/api/ps",
                   "via_gate": "prefer POST /llm/ollama/... so calls are admitted + captured"},
        "docs": {
            "sidecar_calls": "docs/SIDECAR_CALLS.md — curl cookbook for every sidecar",
            "admission_queue": "docs/LLM_ADMISSION_QUEUE.md — the memory gate + proxy design",
        },
        "notes": "LAN-only. Unified memory (~64 GB) is the scarce resource — prefer /llm so the "
                 "gate can pack jobs and QUEUE overflow instead of evicting/OOM-ing. Respect /pressure.",
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
        # 1. idle sidecars. With a need_gb target, evict LRU-FIRST (most-idle first) and STOP
        #    once free ≥ target — minimal eviction preserves recently-used warm models. With
        #    need_gb=0 (the ALARM path) evict EVERY idle sidecar (aggressive, guaranteed room).
        idle_all = []
        for name, base in SIDECAR_BASE.items():
            try:
                d = (await client.get(f"{base}/readyz", timeout=3)).json()
            except Exception:
                continue
            if d.get("lifecycle") == "idle":
                idle_all.append((name, base, float(d.get("idle_seconds", 0) or 0),
                                 bool(d.get("keep_warm"))))   # respect the WARM TAG
        # WARM TAG: a sidecar advertising keep_warm=true has opted to stay resident (e.g. whisper
        # for chiron latency). Evict every NON-warm idle sidecar first; a warm-tagged one is
        # touched only as a last resort — target still unmet (need_gb), or still over the cliff.
        non_warm = [s for s in idle_all if not s[3]]
        warm = [s for s in idle_all if s[3]]
        if req.need_gb:
            non_warm.sort(key=lambda s: s[2], reverse=True)   # LRU: most-idle evicted first
            warm.sort(key=lambda s: s[2], reverse=True)
        for name, base, idle_s, kw in non_warm + warm:      # warm ones always come LAST
            if req.need_gb and not req.dry_run and read_vm()["free_gb"] >= req.need_gb:
                notes.append(f"target {req.need_gb}GB reached — stopped before {name}"
                             + (" (WARM-tagged, preserved)" if kw else " (LRU-preserved)"))
                break
            # ALARM sweep (need_gb=0): preserve a WARM-tagged sidecar unless we're STILL over the
            # cliff. This is a DECISION (shown in dry-run too), not just an action.
            if kw and not req.need_gb and read_vm()["resident_gb"] < CLIFF_GB:
                notes.append(f"{name} WARM-tagged + below cliff — preserved")
                continue
            if req.dry_run:
                freed.append({"tenant": "atelier", "name": name, "idle_s": round(idle_s),
                              "keep_warm": kw, "would_evict": True})
            else:
                try:
                    r = (await client.post(f"{base}/admin/unload", timeout=12)).json()
                    freed.append({"tenant": "atelier", "name": name, "idle_s": round(idle_s),
                                  "keep_warm": kw, "result": r})
                    await asyncio.sleep(0.8)   # let macOS reclaim before the next free re-check
                except Exception as e:
                    notes.append(f"{name} unload failed: {e}")
        # 2. Ollama loaded models — only if still short on room and not actively generating
        if req.need_gb and not req.dry_run and read_vm()["free_gb"] >= req.need_gb:
            notes.append("target reached via idle sidecars — ollama left loaded")
        elif _ollama_recently_active():
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


# ========== (h) inventory + the single unload door ==========
@app.get("/inventory")
async def inventory():
    """WHO IS ACTUALLY HOLDING MEMORY — process truth reconciled against every API claim.
    Read this instead of guessing from free_gb. `flagged` is the honest part: anything the
    self-reports got wrong."""
    async with httpx.AsyncClient() as client:
        return await build_inventory(client)


class UnloadReq(BaseModel):
    target: str = ""       # "<sidecar>" | "ollama:<model>" | "pid:<n>"
    force: bool = False    # sidecar only: preempt a BUSY model (framework refuses otherwise)
    confirm: bool = False  # required for pid: — killing a process is not reversible


@app.post("/unload")
async def unload(req: UnloadReq):
    """ONE door to free a specific thing, whatever holds it. Idle targets need nothing;
    a BUSY sidecar needs force=true (prefer /force-stop's human-gated handshake); a raw
    pid needs confirm=true because there is no graceful protocol for an orphan."""
    if not req.target:
        return {"ok": False, "error": "target required — <sidecar> | ollama:<model> | pid:<n>",
                "hint": "GET /inventory lists every target with its exact unload call"}
    before = read_vm()["free_gb"]
    result: dict = {}

    if req.target.startswith("pid:"):
        try:
            pid = int(req.target.split(":", 1)[1])
        except ValueError:
            return {"ok": False, "error": f"bad pid in '{req.target}'"}
        procs = _proc_table()
        row = next((p for p in procs if p[0] == pid), None)
        if not row:
            return {"ok": False, "error": f"pid {pid} not running"}
        cmd, gb = row[3], round(row[2] / 1048576, 2)
        if not any(h in cmd for h in _RUNNER_HINTS):
            return {"ok": False, "error": f"pid {pid} is not a model runner — refusing",
                    "process": cmd.split()[0], "note": "only model-holding processes are killable here"}
        if not req.confirm:
            return {"ok": True, "phase": "preview", "pid": pid, "gb": gb,
                    "process": cmd[:160],
                    "effect": "SIGTERM — no in-flight request is drained first",
                    "next": f're-POST with {{"target": "pid:{pid}", "confirm": true}}'}
        try:
            os.kill(pid, 15)
            result = {"method": "SIGTERM", "pid": pid, "gb_held": gb}
        except Exception as e:
            return {"ok": False, "error": f"kill {pid} failed: {e}"}

    elif req.target.startswith("ollama:"):
        model = req.target.split(":", 1)[1]
        async with httpx.AsyncClient() as client:
            try:
                await client.post(f"{OLLAMA_URL}/api/generate",
                                  json={"model": model, "keep_alive": 0}, timeout=20)
            except Exception as e:
                return {"ok": False, "error": f"ollama unload {model} failed: {e}"}
        result = {"method": "keep_alive=0", "model": model,
                  "note": "frees after the current request returns"}

    elif req.target in SIDECAR_BASE:
        base = SIDECAR_BASE[req.target]
        params = {"force": "true"} if req.force else {}
        async with httpx.AsyncClient() as client:
            try:
                r = (await client.post(f"{base}/admin/unload", params=params, timeout=15)).json()
            except Exception as e:
                return {"ok": False, "error": f"{req.target} unload failed: {e}",
                        "hint": "sidecar may have no /admin/unload — check GET /inventory"}
        if r.get("refused") == "busy":
            return {"ok": False, "refused": "busy", "target": req.target, "sidecar_result": r,
                    "hint": "pass force=true, or use /force-stop for the human-gated handshake"}
        result = {"method": "/admin/unload" + ("?force=true" if req.force else ""),
                  "sidecar_result": r}
    else:
        return {"ok": False, "error": f"unknown target '{req.target}'",
                "known": sorted(SIDECAR_BASE) + ["ollama:<model>", "pid:<n>"]}

    await asyncio.sleep(1.5)   # let macOS reclaim before re-reading
    after = read_vm()["free_gb"]
    return {"ok": True, "target": req.target, "before_gb": before, "after_gb": after,
            "freed_gb": round(after - before, 1), "result": result}


class IngestCall(BaseModel):
    """One LLM call that never touched this box — report it so the stream is complete."""
    model: str = ""
    backend: str = "external"        # e.g. "ollama-cloud", "openai", "anthropic"
    origin: str = ""                 # who ran it: "wuphf/researcher · task-1841"
    prompt: str = ""
    output: str = ""
    status: int = 200
    latency_s: float | None = None
    in_tok: int | None = None
    out_tok: int | None = None
    ts: float | None = None          # unix seconds; defaults to now
    detail: dict | None = None       # anything else worth keeping (agent, task, trace…)


@app.post("/telemetry/ingest")
async def telemetry_ingest(req: IngestCall):
    """Record a call the governor could not see.

    The office's frontier models run on Ollama Cloud, so those calls never reach this
    machine and can never appear in the task stream — which makes the stream a partial
    record and leaves cloud spend invisible next to local. Reporting them here puts local
    and cloud in ONE searchable place with the same shape, joined by `origin`.

    Marked via="reported" so a self-declared record is never mistaken for one the
    governor observed itself."""
    ts = req.ts or time.time()
    entry = {
        "at": time.strftime("%H:%M:%S", time.localtime(ts)), "ts": ts,
        "status": str(req.status),
        "latency": f"{req.latency_s:.2f}s" if req.latency_s is not None else "—",
        "path": "/external", "model": req.model or "?", "backend": req.backend,
        "via": "reported",
        "prompt": _clip(req.prompt), "output": _clip(req.output),
        "in_tok": req.in_tok, "eval_tokens": req.out_tok,
        "origin": (req.origin or _origin_label(req.detail or {}) or None),
        "origin_detail": req.detail or None,
        "capture_note": None,
    }
    _recent_calls.append(entry)
    return {"ok": True, "recorded": {"model": entry["model"], "origin": entry["origin"],
                                     "prompt_chars": len(entry["prompt"] or ""),
                                     "output_chars": len(entry["output"] or "")}}


@app.get("/telemetry/search")
async def telemetry_search(q: str = "", origin: str = "", model: str = "",
                           via: str = "", limit: int = 50):
    """Search the task stream. Substring, case-insensitive, across prompt/output/model/origin.

    The stream holds a rolling window of calls; scrolling it by eye to find "what did the
    researcher agent ask at 14:05" does not scale past a handful of rows."""
    ql, ol, ml, vl = q.lower(), origin.lower(), model.lower(), via.lower()
    out = []
    for c in reversed(_recent_calls):
        if ol and ol not in str(c.get("origin") or "").lower():
            continue
        if ml and ml not in str(c.get("model") or "").lower():
            continue
        if vl and vl not in str(c.get("via") or "").lower():
            continue
        if ql:
            hay = " ".join(str(c.get(k) or "") for k in
                           ("prompt", "output", "model", "origin", "backend", "client", "path"))
            if ql not in hay.lower():
                continue
        out.append(c)
        if len(out) >= max(1, min(limit, 200)):
            break
    return {"ok": True, "count": len(out), "query": {"q": q, "origin": origin,
            "model": model, "via": via}, "calls": out}


# ========== LLM admission gate — the request-path queue (docs/LLM_ADMISSION_QUEUE.md) ==========
# Clients call POST /admit BEFORE hitting a backend; run only on grant=true; POST /release
# when done. The gate packs jobs into ONE global memory budget across Ollama+mlxlm+llamacpp,
# queues what doesn't fit (never evicts a running model on arrival), and is backstopped by
# live vm_stat memory so it can't drive the machine over the cliff.

ADMIT_EVICT_COOLDOWN = float(os.environ.get("ATELIER_ADMIT_EVICT_COOLDOWN", "15"))
_last_admit_evict = 0.0


class AdmitReq(BaseModel):
    job_id: str = ""               # caller-stable id; re-poll with the same id to check/renew
    model: str = ""                # model name (e.g. "qwen3:32b")
    backend: str = "auto"          # "auto" | "ollama" | "mlxlm" | "llamacpp" — auto picks one
    est_gb: float = 0.0            # optional footprint hint; else estimated from /api/tags


class ReleaseReq(BaseModel):
    lease_id: str = ""
    job_id: str = ""


@app.get("/budget")
def budget():
    """Read-only view of the global LLM memory budget, active leases, and the wait queue."""
    snap = gate.snapshot()
    snap.update({"ok": True, "live_resident_gb": _state.get("resident_gb"),
                 "live_free_gb": _state.get("free_gb"), "cliff_gb": CLIFF_GB,
                 "live_floor_gb": LLM_LIVE_FLOOR_GB})
    return snap


@app.get("/top-processes")
def top_processes(limit: int = 10):
    """Highest-MEMORY processes on the host (RSS, descending) — the actual memory
    consumers, not top-CPU. Sorted here so the dashboard doesn't have to."""
    try:
        out = subprocess.run(["ps", "-axo", "pid,rss,comm"],
                             capture_output=True, text=True, timeout=4).stdout
    except Exception as e:
        return {"processes": [], "error": str(e)}
    rows = []
    for line in out.splitlines()[1:]:
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid, rss_kb = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        rows.append({"pid": pid, "rss_mb": round(rss_kb / 1024),
                     "name": parts[2].rsplit("/", 1)[-1]})
    rows.sort(key=lambda r: r["rss_mb"], reverse=True)
    return {"processes": rows[:max(1, min(limit, 50))]}


@app.post("/admit")
async def admit(req: AdmitReq):
    if not req.job_id or not req.model:
        return {"ok": False, "error": "job_id and model are required"}
    d = await gate.admit(req.job_id, req.model, backend=req.backend, est_gb=req.est_gb)
    # Phase 3: if the queue HEAD is held by memory pressure, try reclaiming IDLE models
    # once (make-room never touches a busy model), then re-decide. Rate-limited so a
    # polling client can't hammer make-room.
    global _last_admit_evict
    if not d.grant and d.needs_idle_evict and (time.time() - _last_admit_evict) > ADMIT_EVICT_COOLDOWN:
        _last_admit_evict = time.time()
        try:
            # Target only the room THIS admit needs → LRU-bounded eviction (make_room stops
            # once free ≥ need_gb), instead of blindly unloading every idle sidecar.
            _need = (req.est_gb or LLM_DEFAULT_EST_GB) + LLM_LIVE_FLOOR_GB
            res = await make_room(MakeRoomReq(dry_run=False, need_gb=_need))
            freed = [f.get("name") for f in res.get("freed", []) if f.get("evicted") or f.get("result")]
            if freed:
                # refresh the gate's untracked view immediately so the retry sees the room
                async with httpx.AsyncClient() as c:
                    tenants = await poll_ollama(c)
                gate.set_live(resident_gb=read_vm()["resident_gb"], free_gb=read_vm()["free_gb"])
                loaded = [{"backend": "ollama", "model": t.get("name"), "gb": t.get("mem_gb", 0.0)}
                          for t in tenants if t.get("name")]
                gate.set_untracked_gb(gate.untracked_from(loaded))
                d = await gate.admit(req.job_id, req.model, backend=req.backend, est_gb=req.est_gb)
                d.reason = (d.reason + f" (after idle-evict: {freed})").strip()
        except Exception as e:
            print(f"[governor] admit idle-evict failed: {e}", flush=True)
    if d.grant:
        return {"ok": True, "grant": True, "lease_id": d.lease_id, "backend": d.backend,
                "base_url": d.base_url, "routed": d.routed, "reserved_gb": d.reserved_gb,
                "reused": d.reused, "ttl_s": d.ttl_s, "reason": d.reason}
    if d.terminal:
        # never-fits: DON'T tell the client to retry — it would poll a queue it can never win.
        return {"ok": True, "grant": False, "terminal": True, "position": -1,
                "reason": d.reason, "retry": None}
    return {"ok": True, "grant": False, "position": d.position, "eta_s": d.eta_s,
            "reason": d.reason, "retry": "re-POST /admit with the same job_id to re-check"}


@app.post("/release")
async def release(req: ReleaseReq):
    if not req.lease_id and not req.job_id:
        return {"ok": False, "error": "lease_id or job_id required"}
    return await gate.release(lease_id=req.lease_id, job_id=req.job_id)


# ========== Phase 6: opt-in CAPTURING proxy ==========
# POST /llm/{backend}/{path} → admit (wait in queue if needed) → forward to the real backend
# → record {model, prompt, in_tok, out_tok, status, ms} into the task stream → release.
# OPT-IN: only callers who choose this URL flow through it; direct callers are untouched.
# Streams transparently (Ollama ndjson + OpenAI SSE), capturing the final token counts.
PROXY_MAX_WAIT_S = float(os.environ.get("ATELIER_PROXY_MAX_WAIT_S", "600"))

# --- auto-size num_ctx to the prompt (so long inputs aren't silently truncated) ---
PROXY_CTX_DEFAULT = int(os.environ.get("OLLAMA_CONTEXT_LENGTH", "16384"))  # the cheap baseline window
PROXY_CTX_CEILING = int(os.environ.get("ATELIER_PROXY_CTX_CEILING", "32768"))  # don't grow past this
PROXY_CTX_HEADROOM_RESERVE_GB = float(os.environ.get("ATELIER_PROXY_CTX_HEADROOM_RESERVE_GB", "2"))  # GB kept free above the KV cache
CHARS_PER_TOKEN = float(os.environ.get("ATELIER_CHARS_PER_TOKEN", "3.5"))  # rough, overestimates slightly
KV_DTYPE_BYTES = float(os.environ.get("ATELIER_KV_DTYPE_BYTES", "2"))      # f16 KV cache = 2 bytes/elem
_ollama_meta_cache: dict[str, dict] = {}


async def _ollama_meta(model: str) -> dict:
    """{native_ctx, kv_rate} for an Ollama model, from /api/show architecture. kv_rate is
    GB of KV cache per token = 2(K+V) × layers × kv_heads × head_dim × dtype_bytes. Cached;
    feeds the gate's per-model KV rate so est_gb scales with the chosen context window."""
    if model in _ollama_meta_cache:
        return _ollama_meta_cache[model]
    meta = {"native_ctx": 0, "kv_rate": 0.0}
    try:
        async with httpx.AsyncClient(timeout=4) as c:
            info = (await c.post(f"{OLLAMA_URL}/api/show",
                                 json={"model": model})).json().get("model_info", {}) or {}
        meta["native_ctx"] = int(next((v for k, v in info.items()
                                       if k.endswith("context_length")), 0) or 0)
        prefix = next((k[:-len(".block_count")] for k in info if k.endswith(".block_count")), None)
        if prefix:
            g = lambda s: info.get(f"{prefix}.{s}")
            n_layers = int(g("block_count") or 0)
            n_heads = int(g("attention.head_count") or 0)
            n_kv = int(g("attention.head_count_kv") or n_heads or 0)
            key_len = g("attention.key_length")
            head_dim = int(key_len) if key_len else (int(g("embedding_length") or 0) // n_heads if n_heads else 0)
            if n_layers and n_kv and head_dim:
                meta["kv_rate"] = (2 * n_layers * n_kv * head_dim * KV_DTYPE_BYTES) / 1e9
    except Exception:
        pass
    if meta["native_ctx"] or meta["kv_rate"]:
        _ollama_meta_cache[model] = meta
    if meta["kv_rate"]:
        gate.set_kv_rate(model, meta["kv_rate"])
    return meta


def _prompt_chars(body: dict) -> int:
    """Total characters of the FULL prompt (untruncated) — for token estimation."""
    msgs = body.get("messages")
    if isinstance(msgs, list):
        total = 0
        for mm in msgs:
            content = mm.get("content", "")
            if isinstance(content, list):
                content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
            total += len(str(content))
        return total
    return len(str(body.get("prompt", "")))


def _headroom_ctx_ceiling(model: str, native_max: int) -> int:
    """Largest num_ctx whose KV cache still fits the governor's LIVE free budget.
    Replaces a static ceiling so a model with memory to spare can grow to its full
    native window instead of a fixed 32K cap. KV is linear: est_gb = weights +
    kv_rate*ctx, so the memory-safe ceiling solves kv_rate*ctx <= free - weights -
    reserve. Falls back to the static PROXY_CTX_CEILING when the per-model KV rate
    is unknown (arch we couldn't parse) or memory is tight."""
    hard = native_max or PROXY_CTX_CEILING
    rate = gate._kv_rate_for(model)                       # GB per token
    if rate <= 0:
        return min(PROXY_CTX_CEILING, hard)
    kv_budget = gate.free_budget_gb() - gate.weights_gb(model) - PROXY_CTX_HEADROOM_RESERVE_GB
    if kv_budget <= 0:
        return min(PROXY_CTX_CEILING, hard)               # tight memory → stay conservative
    fit = int(kv_budget / rate)
    return max(PROXY_CTX_DEFAULT, min(fit, hard))         # never below default, never past native max


def _governor_safe_ctx(model: str, native_max: int) -> int:
    """The largest context window this model can safely use under normal operating
    memory. Sized from WARN_GB (a FIXED constant = the safe-operating LLM envelope),
    NOT the live gate.budget_gb (= cliff - headroom - transient baseline), which
    collapses when non-LLM memory spikes (macOS indexing, leaky sidecars) and would lock
    a tiny window into a client that auto-detects at a bad moment. Actual chats are
    clamped to LIVE memory in _autosize_ctx, so this report stays stable + optimistic."""
    hard = native_max or PROXY_CTX_CEILING
    rate = gate._kv_rate_for(model)
    if rate <= 0:
        return min(PROXY_CTX_CEILING, hard)
    kv_budget = WARN_GB - gate.weights_gb(model) - PROXY_CTX_HEADROOM_RESERVE_GB
    if kv_budget <= 0:
        return min(PROXY_CTX_CEILING, hard)
    fit = int(kv_budget / rate)
    return max(PROXY_CTX_DEFAULT, min(fit, hard))


def _autosize_ctx(body: dict, model: str, native_max: int, prompt_chars: int) -> int | None:
    """If the estimated prompt won't fit the default window, return a larger num_ctx
    (next power of two, bounded by the ceiling and the model's native max). None = leave
    the default. Respects a caller-supplied num_ctx."""
    opts = body.get("options") or {}
    caller_ctx = opts.get("num_ctx")
    if caller_ctx:
        # Respect a caller's num_ctx, but CLAMP it down to what fits LIVE memory — a
        # client (e.g. Goose auto-detecting a big window from /api/show) must never be
        # able to force a context the box can't hold. Returning the clamped value also
        # gives admission the correct estimate (est_gb below uses chosen_ctx).
        ceiling = _headroom_ctx_ceiling(model, native_max)
        return min(int(caller_ctx), ceiling)
    reserve = max(1024, int(opts.get("num_predict") or 0))   # room for the response
    est = int(prompt_chars / CHARS_PER_TOKEN) + reserve
    if est <= PROXY_CTX_DEFAULT:                 # fits the cheap window → leave it
        return None
    target = PROXY_CTX_DEFAULT
    while target < est:
        target *= 2
    ceiling = _headroom_ctx_ceiling(model, native_max)   # memory-aware, not a static 32K cap
    target = min(target, ceiling)
    return target if target > PROXY_CTX_DEFAULT else None


# How much of each call to keep. The task stream exists to answer "what did I actually
# send and what came back" — a 2000-char clip truncated mid-prompt answered neither, so
# the ceiling is generous and the UI scrolls. Both ends are capped so one runaway
# generation can't bloat the durable history file.
CAPTURE_CHARS = int(os.environ.get("ATELIER_CAPTURE_CHARS", "20000"))


def _clip(text, limit=None):
    """Trim to the capture ceiling, but SAY SO — a silently truncated body reads as a
    model that stopped early, which is a different (and alarming) bug."""
    limit = limit or CAPTURE_CHARS
    t = str(text or "")
    if len(t) <= limit:
        return t
    return t[:limit] + f"\n\n… [truncated {len(t) - limit:,} more chars of {len(t):,}]"


def _extract_prompt(body: dict) -> str:
    msgs = body.get("messages")
    if isinstance(msgs, list):
        parts = []
        for mm in msgs:
            content = mm.get("content", "")
            if isinstance(content, list):   # OpenAI structured content parts
                content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
            parts.append(f"{mm.get('role','?')}: {content}")
        return _clip("\n".join(parts))
    if body.get("prompt"):
        return _clip(body["prompt"])
    # EMBEDDINGS. /v1/embeddings and ollama /api/embed put the text in `input` — a string,
    # a list of strings, or (rarely) pre-tokenised id lists. Reading only messages/prompt
    # left every embedding call with an empty input pane while still reporting a token
    # count, which read as "capture is broken" when it was simply the wrong field.
    inp = body.get("input")
    if isinstance(inp, str):
        return _clip(inp)
    if isinstance(inp, list) and inp:
        if all(isinstance(x, str) for x in inp):
            return _clip("\n---\n".join(f"[{i}] {x}" for i, x in enumerate(inp)))
        return _clip(f"({len(inp)} pre-tokenised input(s) — ids, not text)")
    return ""


def _summarise_embeddings(j: dict) -> str:
    """Embeddings have no reply TEXT — the answer is vectors. Say what came back
    (how many, what width) instead of leaving the pane blank as if nothing arrived."""
    data = j.get("data")
    if isinstance(data, list) and data and isinstance(data[0], dict) and "embedding" in data[0]:
        dims = len(data[0]["embedding"] or [])
        head = ", ".join(f"{v:.4f}" for v in (data[0]["embedding"] or [])[:8])
        return (f"▣ {len(data)} embedding vector(s) × {dims} dims — no reply text.\n"
                f"first vector starts: [{head} …]")
    emb = j.get("embeddings") or ([j["embedding"]] if isinstance(j.get("embedding"), list) else None)
    if isinstance(emb, list) and emb and isinstance(emb[0], list):
        head = ", ".join(f"{v:.4f}" for v in emb[0][:8])
        return (f"▣ {len(emb)} embedding vector(s) × {len(emb[0])} dims — no reply text.\n"
                f"first vector starts: [{head} …]")
    return ""


def _extract_output(j: dict) -> str:
    """The assistant's reply text, from either dialect.

    ollama /api/chat -> message.content   ·  /api/generate -> response
    OpenAI-style     -> choices[0].message.content (or .text for completions)"""
    if not isinstance(j, dict):
        return ""
    emb = _summarise_embeddings(j)
    if emb:
        return emb
    ch = j.get("choices")
    if isinstance(ch, list) and ch:
        c0 = ch[0] or {}
        msg = c0.get("message") or c0.get("delta") or {}
        if isinstance(msg, dict):
            return _clip(_join_reasoning(msg.get("reasoning_content") or msg.get("reasoning"),
                                        msg.get("content")) or c0.get("text") or "")
        return _clip(c0.get("text") or "")
    msg = j.get("message")
    if isinstance(msg, dict) and (msg.get("content") or msg.get("thinking")):
        return _clip(_join_reasoning(msg.get("thinking"), msg.get("content")))
    return _clip(j.get("thinking") and _join_reasoning(j.get("thinking"), j.get("response"))
                 or j.get("response") or "")


def _join_reasoning(thinking, content) -> str:
    """Reasoning models split their reply: the chain-of-thought lands in `thinking`
    (ollama) / `reasoning_content` (OpenAI-style) and the answer in `content`.

    Reading only `content` showed an EMPTY output for every reasoning model — and worse,
    for a call cut short by num_predict the thinking is the only text that exists, so the
    pane looked broken when the model had in fact produced plenty. Keep both, labelled,
    so a truncated-in-thought call is legible instead of blank."""
    t, c = (thinking or "").strip(), (content or "").strip()
    if t and c:
        return f"[thinking]\n{t}\n\n[answer]\n{c}"
    if t:
        return f"[thinking — no answer text was emitted]\n{t}"
    return c


def _output_from_stream(buf: bytes) -> str:
    """Reassemble a streamed reply from its chunks.

    A stream arrives as hundreds of fragments; each carries a sliver of text in
    delta.content (OpenAI SSE) or message.content / response (ollama ndjson). Without
    this, every streamed call — which is most interactive ones — showed no output at all."""
    parts, think = [], []
    for line in buf.decode("utf-8", "ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("data:"):
            line = line[5:].strip()
            if line == "[DONE]":
                continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        ch = obj.get("choices")
        if isinstance(ch, list) and ch:
            d = (ch[0] or {}).get("delta") or (ch[0] or {}).get("message") or {}
            if isinstance(d, dict):
                if d.get("reasoning_content") or d.get("reasoning"):
                    think.append(d.get("reasoning_content") or d.get("reasoning"))
                if d.get("content"):
                    parts.append(d["content"])
            continue
        m = obj.get("message")
        if isinstance(m, dict):
            if m.get("thinking"):
                think.append(m["thinking"])
            if m.get("content"):
                parts.append(m["content"])
        elif obj.get("response"):
            parts.append(obj["response"])
    return _clip(_join_reasoning("".join(think), "".join(parts)))


def _usage_from_obj(j: dict):
    """Pull (in_tok, out_tok, tok_s) from an OpenAI or Ollama response object."""
    u = j.get("usage") or {}
    in_tok = u.get("prompt_tokens") if u.get("prompt_tokens") is not None else j.get("prompt_eval_count")
    out_tok = u.get("completion_tokens") if u.get("completion_tokens") is not None else j.get("eval_count")
    tok_s = None
    if out_tok and j.get("eval_duration"):
        try:
            tok_s = round(out_tok / (j["eval_duration"] / 1e9), 1)
        except Exception:
            tok_s = None
    return in_tok, out_tok, tok_s


def _usage_from_stream(buf: bytes, backend: str):
    """Parse the final token counts from a buffered stream body (best-effort)."""
    text = buf.decode("utf-8", "ignore")
    last = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("data:"):              # OpenAI SSE
            line = line[5:].strip()
            if line == "[DONE]":
                continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("usage") or obj.get("done") or obj.get("eval_count"):
            last = obj
    return _usage_from_obj(last) if last else (None, None, None)


def _extract_origin(request, body: dict) -> dict:
    """WHO/WHAT made this call — the join key between an agent's own logs and this stream.

    Without it every row from the LAN is just one bare IP, so a task-stream entry can
    only be matched back to the office by eyeballing timestamps — which breaks the moment
    two agents run at once. Callers attach identity in whatever way their stack allows, so
    read all of them and keep the first that answers:

      headers  X-Atelier-Origin / -Agent / -Task / -Session   (anything explicit)
               X-Title, HTTP-Referer                          (OpenRouter-style, free in many clients)
      body     user                                           (OpenAI standard field, survives LiteLLM)
               metadata.{agent,task,task_id,session,trace_id}  (LiteLLM passes metadata through)

    Everything is optional — a call with no identity is recorded exactly as before."""
    h = {k.lower(): v for k, v in request.headers.items()}
    d = {}
    for key, hdr in (("origin", "x-atelier-origin"), ("agent", "x-atelier-agent"),
                     ("task", "x-atelier-task"), ("session", "x-atelier-session"),
                     ("title", "x-title"), ("referer", "http-referer")):
        if h.get(hdr):
            d[key] = str(h[hdr])[:200]
    if isinstance(body, dict):
        if body.get("user"):
            d.setdefault("user", str(body["user"])[:200])
        md = body.get("metadata")
        if isinstance(md, dict):
            for k in ("agent", "task", "task_id", "session", "session_id", "trace_id", "origin"):
                if md.get(k):
                    d.setdefault(k, str(md[k])[:200])
    # WHO CONNECTED — always available, needs no cooperation from the caller. Once traffic
    # is routed through the governor, ollama's own access log only ever shows 127.0.0.1
    # (the governor forwarding), so the requester's real address exists ONLY here. This is
    # what separates a LAN gateway box from a local Cline (127.0.0.1)
    # when neither sets an identity header.
    try:
        if request.client and request.client.host:
            d["caller_ip"] = request.client.host
    except Exception:
        pass
    return d


def _origin_label(d: dict) -> str:
    """One short human string for the row, e.g. 'wuphf/researcher · task-1841'."""
    if not d:
        return ""
    who = d.get("origin") or d.get("agent") or d.get("user") or d.get("title") or d.get("referer")
    what = d.get("task") or d.get("task_id") or d.get("session") or d.get("session_id")
    return " · ".join(x for x in (who, what) if x)[:160]


def _record_proxy_call(path: str, model: str, backend: str, status: int,
                       latency_s: float, in_tok, out_tok, tok_s, prompt: str,
                       num_ctx=None, est_gb=None, output: str = "", origin: dict | None = None):
    ts = time.time()
    norm = path if path.startswith("/") else "/" + path
    entry = {"at": time.strftime("%H:%M:%S"), "ts": ts, "status": str(status),
             "latency": f"{latency_s:.2f}s", "path": norm, "model": model,
             "backend": backend, "via": "proxy", "prompt": prompt,
             # what actually came BACK — the half the task stream never had, so a bad
             # reply was invisible and only its token count showed up.
             "output": output,
             "in_tok": in_tok, "eval_tokens": out_tok, "tok_s": tok_s,
             "num_ctx": num_ctx, "est_gb": round(est_gb, 1) if est_gb else None,
             # who asked for it — the join back to the caller's own logs
             "origin": _origin_label(origin or {}) or None,
             "origin_detail": origin or None}
    _recent_calls.append(entry)
    _proxy_recent.append((norm, ts))


@app.post("/llm/{backend}/{path:path}")
async def llm_proxy(backend: str, path: str, request: Request):
    base = LLM_ROUTE_BASE.get(backend)
    if not base:
        return JSONResponse({"ok": False, "error": f"unknown backend '{backend}' "
                             f"(use {list(LLM_ROUTE_BASE)})"}, status_code=400)
    raw = await request.body()
    try:
        body = json.loads(raw) if raw else {}
    except Exception:
        body = {}
    model = body.get("model", "?")
    prompt = _extract_prompt(body)
    is_stream = bool(body.get("stream"))
    url = f"{base}/{path}"
    job_id = f"proxy-{backend}-{secrets.token_hex(4)}"

    # Honest-endpoint rewrite: ollama clients (Goose/OpenCode) auto-detect a model's
    # context window from /api/show's model_info.*.context_length. Left unmodified,
    # that's the model's NATIVE max — which can far exceed what the governor's memory
    # budget can actually hold, so a client-side auto-sized ctx can blow the box. Rewrite
    # every *.context_length to a memory-safe ceiling instead. Not gated through admission
    # (it's metadata, not an inference call) and never blocks — any failure falls back to
    # forwarding the upstream response verbatim.
    if backend == "ollama" and path == "api/show":
        await _ollama_meta(model)   # populate gate's kv_rate for this model
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(url, content=raw,
                                      headers={"content-type": "application/json"})
            data = r.json()
            info = data.get("model_info") or {}
            arch = info.get("general.architecture")
            native = int(info.get(f"{arch}.context_length") or 0) if arch else 0
            safe = _governor_safe_ctx(model, native)
            for k in list(info.keys()):
                if k.endswith("context_length"):
                    info[k] = safe
            data["model_info"] = info
            return JSONResponse(data, status_code=r.status_code)
        except Exception:
            # never let a rewrite bug break /api/show — forward unmodified
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(url, content=raw,
                                      headers={"content-type": "application/json"})
            return Response(content=r.content, status_code=r.status_code,
                            media_type=r.headers.get("content-type", "application/json"))

    # Auto-size the context window to the prompt so long inputs aren't silently truncated
    # at the cheap default. Ollama-only (num_ctx is Ollama's knob); OpenAI sidecars manage
    # their own context. If we grow it, re-serialize the body so the runner gets num_ctx.
    # Then size the admission estimate to weights + KV(chosen_ctx) — so a 32K-context call
    # reserves its real (much larger) footprint, not a flat markup.
    chosen_ctx = None
    est_hint = 0.0
    if backend == "ollama" and path in ("api/chat", "api/generate"):
        meta = await _ollama_meta(model)
        chosen_ctx = _autosize_ctx(body, model, meta["native_ctx"], _prompt_chars(body))
        if chosen_ctx:
            body.setdefault("options", {})["num_ctx"] = chosen_ctx
            raw = json.dumps(body).encode()
        est_hint = gate.est_gb(model, ctx=chosen_ctx or PROXY_CTX_DEFAULT)

    # Admit — wait in the queue until granted (fail-open after PROXY_MAX_WAIT_S).
    start = time.time()
    lease = None
    while True:
        d = await gate.admit(job_id, model, backend=backend, est_gb=est_hint)
        if d.grant:
            lease = d
            break
        if time.time() - start > PROXY_MAX_WAIT_S:
            break   # fail-open: proceed ungated rather than hang the caller
        await asyncio.sleep(1.0)

    fwd_headers = {"content-type": request.headers.get("content-type", "application/json")}
    # Pass identity headers through rather than swallowing them — a backend or a
    # downstream proxy may want the same attribution we are recording.
    for _h in ("x-atelier-origin", "x-atelier-agent", "x-atelier-task",
               "x-atelier-session", "x-title", "http-referer"):
        if request.headers.get(_h):
            fwd_headers[_h] = request.headers[_h]
    origin = _extract_origin(request, body)
    t0 = time.time()
    if is_stream:
        media = "text/event-stream" if path.startswith("v1/") else "application/x-ndjson"

        async def _gen():
            buf = bytearray()
            status = 0
            try:
                async with httpx.AsyncClient(timeout=None) as client:
                    async with client.stream("POST", url, content=raw, headers=fwd_headers) as resp:
                        status = resp.status_code
                        async for chunk in resp.aiter_bytes():
                            buf.extend(chunk)
                            yield chunk
            finally:
                in_tok, out_tok, tok_s = _usage_from_stream(bytes(buf), backend)
                _record_proxy_call(path, model, backend, status or 200,
                                   time.time() - t0, in_tok, out_tok, tok_s, prompt,
                                   num_ctx=chosen_ctx, est_gb=est_hint,
                                   output=_output_from_stream(bytes(buf)), origin=origin)
                if lease:
                    await gate.release(job_id=job_id)

        return StreamingResponse(_gen(), media_type=media)

    # Non-streaming: forward, capture exact usage, return the upstream body verbatim.
    try:
        async with httpx.AsyncClient(timeout=None) as client:
            r = await client.post(url, content=raw, headers=fwd_headers)
        out_text = ""
        try:
            j = r.json()
            in_tok, out_tok, tok_s = _usage_from_obj(j)
            out_text = _extract_output(j)
        except Exception:
            in_tok = out_tok = tok_s = None
        _record_proxy_call(path, model, backend, r.status_code, time.time() - t0,
                           in_tok, out_tok, tok_s, prompt, num_ctx=chosen_ctx,
                           est_gb=est_hint, output=out_text, origin=origin)
        return Response(content=r.content, status_code=r.status_code,
                        media_type=r.headers.get("content-type", "application/json"))
    except Exception as e:
        _record_proxy_call(path, model, backend, 502, time.time() - t0,
                           None, None, None, prompt, num_ctx=chosen_ctx, est_gb=est_hint,
                           output=f"▲ proxy→{backend} failed: {e}", origin=origin)
        return JSONResponse({"ok": False, "error": f"proxy→{backend} failed: {e}"}, status_code=502)
    finally:
        if lease:
            await gate.release(job_id=job_id)


# ---------- DROP-IN ollama surface: the governor answers ollama's own API shape ----------
# Asking every client to rewrite its URL to /llm/ollama/... is the wrong ask on a machine
# we own — and it is why traffic keeps escaping capture: Cline, LiteLLM and anything else
# that only exposes a "base URL" setting cannot add a path prefix. Ollama's own logs are no
# fallback: even at --log-verbosity 4 they record token COUNTS (task.n_tokens = 2156), never
# prompt text, so a bypassed call is genuinely unrecoverable after the fact.
#
# So the governor speaks ollama natively at its root. A client changes ONLY the port
# (11434 -> 8799) and every call is captured, with no path rewriting anywhere.
# For fully transparent capture (zero client changes), move ollama to 11435 and let the
# governor own 11434 — same code path, see docs.
@app.post("/api/{path:path}")
async def ollama_compat_post(path: str, request: Request):
    return await llm_proxy("ollama", f"api/{path}", request)


@app.get("/api/{path:path}")
async def ollama_compat_get(path: str, request: Request):
    return await llm_proxy_get("ollama", f"api/{path}", request)


@app.post("/v1/{path:path}")
async def openai_compat_post(path: str, request: Request):
    return await llm_proxy("ollama", f"v1/{path}", request)


@app.get("/v1/{path:path}")
async def openai_compat_get(path: str, request: Request):
    return await llm_proxy_get("ollama", f"v1/{path}", request)


@app.get("/llm/{backend}/{path:path}")
async def llm_proxy_get(backend: str, path: str, request: Request):
    """GET passthrough for the metadata/listing calls ollama clients make (api/tags,
    api/version, v1/models) — the POST-only proxy above 404s on these, which breaks
    client auto-configuration before it even gets to a chat/generate call."""
    base = LLM_ROUTE_BASE.get(backend)
    if not base:
        return JSONResponse({"ok": False, "error": f"unknown backend '{backend}' "
                             f"(use {list(LLM_ROUTE_BASE)})"}, status_code=400)
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(f"{base}/{path}", params=dict(request.query_params))
        return Response(content=r.content, status_code=r.status_code,
                        media_type=r.headers.get("content-type", "application/json"))
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)
