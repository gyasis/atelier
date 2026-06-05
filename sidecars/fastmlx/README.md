# fastmlx-sidecar — LLM/VLM via FastMLX (MLX-native, Metal)

A thin **managed wrapper** around [FastMLX](https://github.com/Blaizzy/fastmlx) — the MLX-native OpenAI server. Port **8772**. The MLX complement to the GGUF/llama.cpp path; typically faster than GGUF on Apple Silicon.

The wrapper subprocess-manages FastMLX on an internal port, proxies `/v1/*`, and adds the Atelier contract (`/healthz`, `/readyz`, `/admin/unload`, `/agent`, **idle-unload**).

```
client → :8772/v1/chat/completions      (Atelier-fronted OpenAI API)
         :8772/healthz /readyz /agent /admin/unload
         └─ proxies → 127.0.0.1:18772    (FastMLX child, MLX/Metal)
idle → wrapper stops the child → MLX model memory freed.
```

Unlike `llama-server` (one model loaded at startup), FastMLX **loads MLX models lazily** by name from `mlx-community` on first request — so "warm" here means the server is up; the model loads on demand.

## Install / deploy

```sh
# the engine + MLX come in via requirements.txt (fastmlx, mlx, mlx-lm, mlx-vlm)
# Apple Silicon only.
```

`deploy/install.sh` creates the venv, installs `requirements.txt` (which provides the `fastmlx` CLI), symlinks `server.py`, and drops the launchd plist.

## Use (OpenAI-compatible)

```sh
curl -s http://192.168.0.159:8772/v1/chat/completions -H 'content-type: application/json' \
  -d '{"model":"mlx-community/Llama-3.1-8B-Instruct-4bit",
       "messages":[{"role":"user","content":"hello"}]}'

# pre-load a model (else it loads on first chat call)
curl -s -X POST "http://192.168.0.159:8772/v1/models?model_name=mlx-community/Llama-3.1-8B-Instruct-4bit"
```

Set `model` to any `mlx-community` repo. `stream:true` for SSE; `tools:[…]` for tool calling; image content for VLMs. Point any OpenAI client at `http://192.168.0.159:8772/v1`.

## Lifecycle

| Endpoint | Purpose |
|---|---|
| `GET /healthz` | liveness (wrapper only) |
| `GET /readyz` | warm/cold/busy, child pid, idle seconds |
| `GET /agent` | machine-readable manifest for agents |
| `POST /admin/unload[?force=true]` | stop the engine to free MLX memory |
| `ANY /v1/{path}` | OpenAI proxy (cold-starts the engine on first call) |

Engine **cold-starts on first `/v1` call**, **idle-unloads** after `IDLE_UNLOAD_SECONDS` (default 600). In-flight requests are tracked so idle-unload can't interrupt a live generation.

## Env vars

| Var | Default |
|---|---|
| `FASTMLX_BIN` | `fastmlx` (venv CLI) |
| `FASTMLX_PORT` | `8772` (public) |
| `FASTMLX_CHILD_PORT` | `18772` (internal FastMLX) |
| `FASTMLX_DEFAULT_MODEL` | informational, for `/agent` |
| `FASTMLX_ARGS` | extra args, e.g. `--workers 1` |
| `IDLE_UNLOAD_SECONDS` | `600` |
| `KEEP_WARM` | `false` |
| `HUB_TOKEN` | unset |

> MLX-native path. For GGUF models use the **llamacpp** sidecar (`:8771`).
