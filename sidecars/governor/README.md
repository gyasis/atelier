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
