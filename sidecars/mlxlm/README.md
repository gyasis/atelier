# mlxlm-sidecar — LLM via Apple mlx_lm.server (MLX-native, Metal)

A thin **managed wrapper** around [`mlx_lm.server`](https://github.com/ml-explore/mlx-lm) — Apple's official MLX LLM server. Port **8773**. The MLX-native LLM path; typically faster than GGUF on Apple Silicon, and it tracks the mlx-lm release exactly.

> This is the working MLX-native sidecar. The **fastmlx** sidecar (`:8772`) is scaffolded but blocked on upstream FastMLX lagging mlx-lm's API (it imports/calls removed symbols like `generate_step(temp=…)`); re-enable it when FastMLX catches up.

```
client → :8773/v1/chat/completions      (Atelier-fronted OpenAI API)
         :8773/healthz /readyz /agent /admin/unload
         └─ proxies → 127.0.0.1:18773    (mlx_lm.server child, MLX/Metal)
idle → wrapper stops the child → MLX model memory freed.
```

mlx_lm.server loads **one model at startup** (`--model`), so cold-start = model load; "warm" = model loaded.

## Install / deploy

`requirements.txt` pulls `mlx` + `mlx-lm` (the engine) + the FastAPI/httpx wrapper stack. `deploy/install.sh` creates the venv, symlinks `server.py`, and drops the plist. Set `MLXLM_MODEL` to an `mlx-community` repo or a local model dir.

## Use (OpenAI-compatible)

```sh
curl -s http://192.168.0.159:8773/v1/chat/completions -H 'content-type: application/json' \
  -d '{"model":"<MLXLM_MODEL ref>","messages":[{"role":"user","content":"hello"}]}'

# streaming
curl -sN http://192.168.0.159:8773/v1/chat/completions -H 'content-type: application/json' \
  -d '{"messages":[{"role":"user","content":"hi"}],"stream":true}'   # model optional → uses loaded one
```

> **Model field:** mlx_lm.server registers the model under its `--model` ref (repo name or local path) and also auto-discovers other models in `HF_HOME`. Send `"model"` = the exact `MLXLM_MODEL` ref, **or omit it** (uses the loaded model). Sending a different id makes it try to load that model.

## Lifecycle

| Endpoint | Purpose |
|---|---|
| `GET /healthz` | liveness (wrapper only) |
| `GET /readyz` | warm/cold/busy, child pid, model |
| `GET /agent` | machine-readable manifest for agents |
| `POST /admin/unload[?force=true]` | stop the engine to free MLX memory |
| `ANY /v1/{path}` | OpenAI proxy (cold-starts the engine on first call) |

Cold-starts on the first `/v1` call, **idle-unloads** after `IDLE_UNLOAD_SECONDS` (default 600). In-flight requests are tracked so idle-unload never interrupts a live generation.

## Env vars

| Var | Default |
|---|---|
| `MLXLM_MODEL` | — (required: mlx-community repo or local dir) |
| `MLXLM_ALIAS` | model basename (display name) |
| `MLXLM_PYTHON` | the wrapper's interpreter (has mlx-lm) |
| `MLXLM_PORT` | `8773` (public) |
| `MLXLM_CHILD_PORT` | `18773` (internal mlx_lm.server) |
| `MLXLM_ARGS` | extra mlx_lm.server args |
| `IDLE_UNLOAD_SECONDS` | `600` |
| `KEEP_WARM` | `false` |
| `HF_HUB_OFFLINE` | set `1` when `MLXLM_MODEL` is a local dir |
| `HUB_TOKEN` | unset |

> MLX-native path. For GGUF models use the **llamacpp** sidecar (`:8771`).
