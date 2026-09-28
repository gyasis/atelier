# colibri sidecar — huge MoE models for OVERNIGHT work (:8783)

Governed Atelier wrapper around [Colibrì](https://github.com/JustVugg/colibri) (`coli serve`),
which runs very large mixture-of-experts models by streaming experts from the SSD.
It is **slow by design**, so this sidecar is built for work nobody is waiting on:
batch jobs, overnight classification, long generations.

```
client ──► :8799/llm/colibri/v1/...   (governor front door: admitted, captured, lease renewed)
       └─► :8783/jobs                 (async overnight queue, persisted, collect later)
              │
              └─ :8783 uvicorn (this sidecar, launchd io.macstudio.hub.colibri)
                    └─ :18783 `coli serve` gateway   ┐ one process group,
                          └─ engine binary (qwen36…) ┘ started + killed together
```

## Calling it

**Overnight work → `POST /jobs`.** Do not hold an HTTP connection open for hours.

```bash
# submit (returns 202 + job_id immediately; the job is on disk before it runs)
curl -s http://<mac-host>:8783/jobs -H 'content-type: application/json' -d '{
  "label": "nightly-triage",
  "path":  "v1/chat/completions",
  "body":  {"model":"colibri-qwen36","messages":[{"role":"user","content":"..."}],"max_tokens":512}}'

curl -s http://<mac-host>:8783/jobs/<job_id>     # status + result
curl -s http://<mac-host>:8783/jobs              # recent jobs
```

Job states: `queued → waiting_admission → loading → running → done | failed | refused | interrupted | cancelled`.
While running, `GET /jobs/<id>` reports `elapsed_s` and `child_cpu_seconds`.
Cancel: `DELETE /jobs/<id>` (queued/waiting: immediate; loading/running: `?force=true`, kills the engine).

**Jobs yield to interactive work.** If the governor has no room, a job does NOT wait in the
governor's FIFO queue (where a 26 GB request at the head would block every small call behind it —
it did, 2026-09-28). It asks, withdraws (`/release`), unloads its own idle model, and asks again
every 30 s (`COLIBRI_POLITE_RETRY_S`). A job only waiting is not "busy".

**Closed-set decisions → Brio** (scores fixed options, generates nothing, returns entropy):

```bash
curl -s http://<mac-host>:8783/jobs -H 'content-type: application/json' -d '{
  "path": "v1/brio",
  "body": {"state":"<document>","question":"Which queue?","options":["billing","bugs","sales"]}}'
```
`v1/systemone` is the Jev-compatible shape of the same thing.

**Short sync calls** go through the governor: `POST :8799/llm/colibri/v1/chat/completions`.
Client timeout rule: short **connect** (~5 s), **no read timeout**. The governor never queues a
colibri call: no room now → immediate **503** with a hint to use `/jobs` (never a fail-open load).

## Timeouts: there are none, on purpose — watch liveness instead

A slow generation that is still working must not be killed. Nothing in this path has a
wall-clock read timeout (sidecar → child, governor proxy, `/jobs`). To tell **slow** from
**stuck**, watch `child_cpu_seconds` in `/readyz` or `/jobs/<id>`: rising = working, flat
while a job runs = stuck. Unstick with `POST /admin/unload?force=true` (kills the whole group).

## Governor integration (what makes long jobs safe)

| mechanism | why |
|---|---|
| **lease heartbeat** | governor leases expire after 900 s. Both `/jobs` (re-admits every 120 s) and the governor's `/llm/{backend}` proxy (renews every TTL/3) keep the lease alive for the whole call, so the gate never admits another model on top of a running Colibrì job. |
| **admission yields** | a `/jobs` job retries admission politely (outside the governor queue) instead of failing open. `COLIBRI_ADMIT_MAX_WAIT_S=0` (default) = keep trying forever. |
| **no-queue proxy** | `ATELIER_PROXY_NO_QUEUE_BACKENDS=colibri`: one admit attempt, else 503 — never the 600 s wait + ungated fail-open other backends get. |
| **resident = recognised** | the governor reports colibri's RSS under (colibri, loaded model) and adds it to the loaded set from each poll, so a warm model is reused at 0 GB instead of blocking its own admission. |
| **estimate** | registry `ram` is both Colibrì's `--ram` budget and the admission estimate. The proxy path uses the governor's `"colibri-"` override (10 GB). |
| **registration** | `SIDECAR_BASE/LABELS/LOGS/ROLES/AGENT_CAPABLE` + `LLM_ROUTE_BASE["colibri"]` → shows in `GET :8799/agent?expand=true`. |

## Process safety (the ghost-engine problem)

`coli serve` spawns the engine binary as its own child. Killing only the gateway would orphan
a multi-GB engine. So:

- the child runs in its **own session**; stop = `killpg` SIGTERM + **SIGCONT** (a stopped
  process cannot act on SIGTERM) → wait 10 s → SIGKILL → verify the group is empty;
- its pgid is written to `~/.atelier/colibri-child.pgid`; if this sidecar is ever killed
  without shutting down (crash, SIGKILL), the **next boot reaps the orphaned group** and
  reports it in `/readyz` `reaped_at_boot`.

## Models (`~/.atelier/colibri-models.json`)

```json
{"default": "colibri-qwen36",
 "models": {
   "colibri-qwen36":     {"path": "~/models/colibri/qwen36_i4_gs64", "ram": 10, "args": ["--kv-slots","1"]},
   "colibri-olmoe-tiny": {"path": "~/models/colibri/olmoe_tiny",     "ram": 1,  "args": ["--kv-slots","1"]}}}
```

- `colibri-qwen36` — Qwen3.6-35B-A3B int4-gs64 (~23 GB), from
  `Kreuzzelg/qwen36-35b-a3b-colibri-i4-gs64`. Engine `make -C c qwen36`. **CPU path**: the
  qwen36 target does not link Colibrì's Metal backend.
- `colibri-olmoe-tiny` — **TEST FIXTURE ONLY.** Random weights (upstream
  `tools/make_olmoe_tiny.py` → `convert_olmoe_merged.py` → `make_edge_tiny_tokenizer.py`),
  gibberish output. Exercises the real engine + gateway for lifecycle tests.

Add a model = build its engine (`make -C colibri/c <family>`), download its container, add a
registry entry, `launchctl kickstart -k gui/$(id -u)/io.macstudio.hub.colibri`.

## Measured (Qwen3.6-35B-A3B int4-gs64, M1 Max 64 GB, CPU path, 2026-09-28)

| | |
|---|---|
| generation | ~1.0–1.1 tok/s (128 tokens ≈ 2 min) |
| cold load | ~7 s |
| resident | ~5 GB process tree at `--ram 26` (the cap is a ceiling for the expert cache, not the footprint) |
| cap now | `ram: 10` (2026-09-28) — cap and admission estimate lowered together; 26 was ~5x the measured footprint and kept the job waiting for room |
| page cache | +0.1 GB — streaming did not inflate the governor's "resident" figure for this model |
| Brio, 1 yes/no question | ~47 s (prefill-bound) |

`ram` is both the `--ram` cap (so resident can never exceed it) and the governor estimate — they
move together, so the estimate cannot understate. An example registry is `colibri-models.example.json`.

## Tests

```bash
cd ~/services/colibri-sidecar
.venv/bin/python ~/Documents/code/atelier/sidecars/colibri/test_colibri_sidecar.py          # ~1 min
.venv/bin/python …/test_colibri_sidecar.py --long                                          # +17 min
COLIBRI_TEST_MODEL=colibri-qwen36 .venv/bin/python …/test_colibri_sidecar.py --only sync,unload
```

Placeholders: `<mac-host>` is the Mac Studio's LAN address (kept out of this public repo).

A "slow job" is produced on demand by SIGSTOP-ing the engine group mid-request, so the
busy/queue/lease tests are deterministic on any model.

| test | proves |
|---|---|
| health | sidecar + registry + governor `/agent` listing |
| cold | force-unload leaves no group and no child port |
| sync | chat works; the group holds gateway **and** engine |
| busy | while a job runs: unload refused, 2nd job queues, lease held, CPU reads flat when frozen |
| unload | the whole group dies (no orphaned engine) |
| proxy | governor front door: lease taken while running, released after |
| requeue | a job still WAITING for admission survives a sidecar restart (requeued, not interrupted) |
| persist | sidecar restart: running job → `interrupted` (never auto-retried), queued job survives and runs |
| crash | SIGKILL the sidecar: the next boot reaps the orphaned engine group |
| cancel | queued job cancelled; a finished job cannot be |
| polite | with a placeholder lease making the model not fit: job waits with 0 governor-queue entries, not busy, cancellable |
| long | a proxied call and a job held past the 900 s lease TTL: lease renewed, never lost, both return |

Tests that need a grant **SKIP** (with the numbers) when the governor has no room — a full box is a
precondition, not a defect.

Results land in `~/.atelier/colibri-test-results/<ts>-<model>.json`.

## Layout

| what | where |
|---|---|
| source (this dir) | `~/Documents/code/atelier/sidecars/colibri/` |
| deployed | `~/services/colibri-sidecar/server.py` → symlink to the source |
| Colibrì checkout (v1.12.1) | `~/services/colibri-sidecar/colibri/` |
| venv | `~/services/colibri-sidecar/.venv` (fastapi, uvicorn, httpx; `coli` needs no deps) |
| launchd | `io.macstudio.hub.colibri` (`launchd/io.macstudio.hub.colibri.plist`, copied to `~/Library/LaunchAgents`) |
| logs | `~/Library/Logs/colibri-sidecar.{out,err}.log` |
| jobs | `~/.atelier/colibri-jobs/<job_id>.json` |
| registry | `~/.atelier/colibri-models.json` (example: `colibri-models.example.json`) |
| accounting check | `check_double_count.py <backend> <model> <url>` — a leased warm model must leave "untracked" |
