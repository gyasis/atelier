# governor-sidecar — hub supervisor

The Atelier hub's brain. Port **8799**. Watches unified-memory pressure across Ollama + all sidecars, evicts **idle** models to make room, records every run, and predicts ETAs. It's the single entry point for agents (`/agent`) and the dashboard's main data source.

## What it does

- **Memory governor** — polls `/api/ps` (Ollama) + each sidecar's `/readyz`, computes a pressure level, and on ALARM auto-runs idle eviction. Preempting a *busy* model is human-gated.
- **Telemetry** — tails the Ollama log + sidecar logs (`[tts]`, `[asr]`) into one observable pane.
- **Predictor** — Bayesian per-(kind, model) ETA estimator (`predictor.py`, persistent SQLite at `~/.atelier/predictor.db`).

## Endpoints

| Endpoint | Purpose |
|---|---|
| `GET /agent[?expand=true]` | **hub manifest** — control plane + every sidecar (inline with `expand`) |
| `GET /pressure` | `{level, free_gb, resident_gb, tenants[], alerts, recommendation}` |
| `GET /telemetry` | recent calls, synths, events, live Ollama perf |
| `GET /estimate` | predicted ETA — `?engine=whisper&audio_s=N` / `?engine=<tts>&chars=N` / `?model=<llm>&out_tokens=N` |
| `GET /predictor/stats` · `/predictor/export` | learned compute stats / portable dataset |
| `GET /benchmark?model=<m>` | fire a real generate, measure decode tok/s, record it |
| `POST /report` | feed a completed run into the predictor |
| `POST /make-room` | evict ONLY idle models (safe, agent-callable) |
| `POST /force-stop` | two-phase, human-gated preempt of a BUSY model |

## Registry

New sidecars are added to the dicts near the top of `server.py`: `SIDECAR_BASE` (port), `SIDECAR_LABELS` (launchd label, for force-stop), `SIDECAR_ROLES` + `AGENT_CAPABLE` (hub manifest). Monitoring, idle-eviction, and `/agent` discovery then include them automatically.

## Env

`ATELIER_POLL_SECONDS` (10), `OLLAMA_URL`, `OLLAMA_LOG`, `ATELIER_AUTO_MAKE_ROOM` (1), `ATELIER_AUTO_COOLDOWN` (60), `ATELIER_PROBE_COOLDOWN` (60), `HUB_TOKEN`.

## num_ctx auto-sizing — memory-aware ceiling (2026-07-24)

The `/llm/ollama/{api/chat,api/generate}` proxy auto-injects `options.num_ctx`, sized to the
prompt **and** to live free memory. `_autosize_ctx(body, model, native_max, prompt_chars)` grows
num_ctx by powers of two until the prompt fits, then caps at `_headroom_ctx_ceiling(model, native_max)`:

    ceiling = (gate.free_budget_gb() - gate.weights_gb(model) - RESERVE) / gate._kv_rate_for(model)

bounded by the model's native max. This replaced a **static** `ATELIER_PROXY_CTX_CEILING` (32768),
which choked long-context calls even when memory was free. Falls back to the static cap when the
per-model KV rate is unknown or memory is tight; a caller-supplied `options.num_ctx` is always respected.

- Env: `ATELIER_PROXY_CTX_HEADROOM_RESERVE_GB` (default 2) — GB kept free above the KV cache.
- Verified: with ~20 GB free, ornith-9b auto-loaded at ctx=65536 (was 32768); 35B/70B stay conservative
  under pressure (swap-death guard).
- Companion (RAW :11434 callers, which bypass the proxy): the Ollama.app global default
  (`db.sqlite settings.context_length` = 65536) + the `ollama-ctx` dial + `~/.config/ollama-ctx/registry.json`.
