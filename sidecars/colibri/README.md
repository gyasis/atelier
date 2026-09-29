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
| **estimate** | registry `ram` is the governor's admission ESTIMATE. It is also passed as `--ram`, but **the qwen36 engine ignores `--ram`** (upstream `docs/qwen36.md`): its memory is set by `--cap`. Keep `ram` ≥ the measured RSS for the chosen cap. The proxy path uses the governor's `"colibri-"` override (16 GB). |
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
   "colibri-qwen36":     {"path": "~/models/colibri/qwen36_i4_gs64", "ram": 16,
                         "args": ["--kv-slots","1","--ctx","65536","--no-think","--cap","256"],
                         "env": {"COLI_TOOL_FALLBACK": "1"}},
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
| cap now | `--cap 256`, estimate `ram: 16` (2026-09-28) — see the correction below |
| page cache | +0.1 GB — streaming did not inflate the governor's "resident" figure for this model |
| Brio, 1 yes/no question | ~47 s (prefill-bound) |

**Correction (2026-09-28).** This file previously said `ram` is "the `--ram` cap AND the governor
estimate, so the estimate cannot understate". That is **false for qwen36**: the engine never reads
`--ram` (upstream `docs/qwen36.md`, "`--ram` is not honoured by this engine"). Memory and speed are set
by **`--cap N`** — expert cache slots per layer (of 256). The default cap (8) streamed nearly every
expert from SSD. What follows: `ram` is only an estimate, so it must be set from a MEASURED RSS for
the cap in use, and a cap change needs a re-measure. An example registry is `colibri-models.example.json`.

| `--cap` | resident (measured) | generation | Brio, 1 question |
|---|---|---|---|
| 8 (default) | ~5 GB | ~1.05 tok/s | ~47 s |
| **256 (now)** | **~14 GB** | **~2.7 tok/s** | **~19 s** |

Upstream measures 12.8–15.7 tok/s at cap 256 on an AVX-512 x86 box; its batched CPU prefill is
AVX2-only, which is a likely (unmeasured) part of the M1 Max gap.

## Models on this box (measured 2026-09-29, Mac Studio M1 Max 64 GB, colibri dev `eefa57a`)

Downloaded with `pull_models.sh` (resumable, pfetch, hash-verified; ~80 MB/s that day). Every model
got `smoke_model.py` (load + answer + native tool call + memory) and, where it passed, the same pi
task (fix `add()`, run pytest). Numbers were taken while other models downloaded, so they are
pessimistic.

| alias | model (total / active) | disk | RSS measured | pi task | notes |
|---|---|---|---|---|---|
| `colibri-qwen36` | Qwen3.6-35B-A3B | 23 GB | ~14 GB (cap 256) | **12 min** (491/33/74/62/43/21 s) | best interactive option here |
| `colibri-qwen38-flash-next` | Qwen3.8-Flash-Next 125B(+51B n-gram) / 6B | 186 GB | ~11.8 GB (cap 32) | **29 min** (1401/165/98/76 s) | fastest later rounds; needs `request_defaults` (see below) |
| `colibri-dsv4-flash` | DeepSeek V4 Flash 284B / 13B | 167 GB | ~19.3 GB | **2 h 13 min** (5601/1215/848/329 s) | correct, overnight-class: prompt read ~0.4-1.3 tok/s on CPU |
| `colibri-qwen38-27b` | Qwen3.8-27B dense | 51 GB (converted) | ~14-15.5 GB | not run | slow here and prefix reuse diverges; **use it via Ollama instead** |
| `colibri-glm53` | GLM-5.3 744B / 40B | 419 GB | pending | pending | engine built with Metal (`METAL=1`, `COLI_METAL=1`) |
| `colibri-dsv41-flash` | DeepSeek V4.1 Flash 552B / 16B | 510 GB | pending | pending | needs `prepare_dsv41.py` (done by `pull_models.sh`) |

GLM-5.3-Flash was dropped (a ~25 h conversion, and ~20-44 s/token on this class of machine per its
docs); `pull_models.sh glm53_flash` still fetches it.

### Thinking must be switched off per request for the Qwen3.8 family

`--no-think` is **ignored** by the qwen38 engine (measured: 93 chars of reasoning, 30 s for "OK").
`enable_thinking: false` or `reasoning_effort: "none"` in the REQUEST works (0 chars, 0.4-5.8 s).
pi does not send either for a non-reasoning model, so the registry carries

```json
"request_defaults": {"enable_thinking": false}
```

which the sidecar fills into every `/v1` and `/jobs` request that does not set it (never overriding
the client). `think_probe.py <alias>` measures which switch a model honours.

### Model swaps through the governor

The sidecar holds one model. A request for a different model used to get a 503 even with plenty of
memory: admission counts the idle model against the budget, while `make_room` looks at physical free
memory and saw no need to evict. The governor now unloads the colibri sidecar's idle model when a
different colibri model is requested (the swap would free it anyway), and converts any remaining
budget shortfall into a physical target for `make_room`. Its estimate per model comes from this
sidecar's `/models` (`ram`), not a flat 16 GB.

## Driving a coding agent (pi) with Colibri

**Run it on Colibri's `dev` branch** (built from `eefa57a`, 2026-09-29): it has native Qwen3.6 tool
calling ([#1794](https://github.com/JustVugg/colibri/pull/1794)) and prefix reuse that survives a
chat client's round trip ([#1767](https://github.com/JustVugg/colibri/pull/1767)). Neither is in the
v1.12.1 release.

pi provider (`~/.pi/agent/models.json`), through the governor so the call is admitted and its lease
renewed:

```json
"atelier-colibri": {"baseUrl": "http://<mac-host>:8799/llm/colibri/v1", "api": "openai-completions",
  "apiKey": "unused-local", "models": [{"id": "colibri-qwen36", "reasoning": false,
  "input": ["text"], "contextWindow": 65536, "maxTokens": 4096}]}
```
```bash
pi -p --provider atelier-colibri --model colibri-qwen36 --thinking off \
   --no-extensions --no-skills --no-prompt-templates -a "<task>"
```

Same one-bug task (fix `add()`, run pytest), same flags, `--cap 256`, both runs fixed and verified:

| | v1.12.1 + tool-fallback patch | **dev `eefa57a`** |
|---|---|---|
| total | 2,248 s (37 min), 4 rounds | **725 s (12 min), 6 rounds** |
| round 1 (nothing to reuse) | 489 s | 491 s |
| later rounds | 565–597 s each (whole conversation re-read) | **21–74 s each** |
| engine | — | `[PREFIX] reusing 2047 of 2116` … `2888 of 2924` (94–99%) |
| Mac | 84–86% free, swap flat | 84% free, swap flat |

Controlled A/B on `dev`, same 2-turn tool loop, identical outputs: turn 2 = **29.6 s** with reuse,
**111.7 s** with `COLI_KV_PREFIX=0`.

### Why later rounds used to cost a full re-read

pi (any OpenAI-style client) resends the whole conversation every turn; the server is supposed to
notice the new prompt starts with what it already processed and read only the tail. Qwen3.6 is hybrid:
30 of 40 layers are DeltaNet, which keep one running state instead of a per-token cache, so the state
**cannot be rewound** — reuse needs the new prompt to match what the engine processed token for token,
or nothing is reused. The engine generates each reply after an empty `<think></think>` header; the
official chat template stripped that block from past turns, so the history came back different at the
first assistant turn and every round paid a full prefill. #1767 renders past turns with the block
(`preserve_thinking`), so the history round-trips exactly. `COLI_PREFIX_LOG=1` (set in the registry
`env`) prints the decision per request: `reusing N of M` or `no reuse … (diverged)`.

### What each setting is for

| setting | why |
|---|---|
| `--ctx 65536` | engine default 8192 < a pi conversation (native max 262,144; only 10 of 40 layers hold KV) |
| `--no-think` | hybrid thinking model at ~2.7 tok/s |
| `--cap 256` | expert cache slots/layer: ~14 GB, ~2.7 tok/s (cap 8: ~5 GB, ~1 tok/s) |
| `env COLI_PREFIX_LOG=1` | make the reuse decision observable |
| pi's 10-min HTTP timeout | not hit: the governor streams headers at once (a 19-min round completed) |

### Switching Colibri builds

The sidecar runs whatever `~/services/colibri-sidecar/colibri-active` points at (plist `COLIBRI_HOME`):

```bash
cd ~/services/colibri-sidecar
ln -sfn colibri-dev colibri-active      # dev (current)
ln -sfn colibri     colibri-active      # v1.12.1 release (fallback)
launchctl kickstart -k gui/$(id -u)/io.macstudio.hub.colibri
# update dev: git -C colibri-dev pull && make -C colibri-dev/c qwen36 olmoe
```

On **v1.12.1** only: qwen36 refuses tools, so it needs `env COLI_TOOL_FALLBACK=1` **and**
`patches/0001-qwen36-tool-fallback.patch` (its fallback prompt showed a bare `{function-name}` template
that Qwen3.6 copied literally, so every pi call was rejected). On `dev` the fallback no longer applies
to qwen36 and the patch is not needed.

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
