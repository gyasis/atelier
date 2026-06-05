# llamacpp-sidecar — LLM via llama.cpp (Metal, GGUF)

A thin **managed wrapper** around llama.cpp's `llama-server`. Port **8771**. Part of the Mac Studio inference hub.

`llama-server` already speaks the OpenAI API and Metal-accelerates GGUF models — this wrapper subprocess-manages it on an internal port, proxies `/v1/*`, and adds the Atelier contract (`/healthz`, `/readyz`, `/admin/unload`, `/agent`, **idle-unload**) so the governor can monitor and idle-evict it like every other sidecar.

```
client → :8771/v1/chat/completions      (Atelier-fronted OpenAI API)
         :8771/healthz /readyz /agent /admin/unload
         └─ proxies → 127.0.0.1:18771    (llama-server child, Metal/GGUF)
idle → wrapper stops the child → model memory freed.
```

## Install / deploy

The wrapper needs only FastAPI + httpx (in `requirements.txt`); the **engine installs separately**:

```sh
brew install llama.cpp          # provides `llama-server`
# put a GGUF model at ~/models/gguf/ and point LLAMACPP_MODEL at it
```

`deploy/install.sh` creates the venv, symlinks `server.py`, and drops the launchd plist. Edit the plist's `LLAMACPP_MODEL` to your `.gguf`.

## Use (OpenAI-compatible)

```sh
curl -s http://192.168.0.159:8771/v1/chat/completions -H 'content-type: application/json' \
  -d '{"model":"<alias>","messages":[{"role":"user","content":"hello"}]}'

# streaming
curl -sN http://192.168.0.159:8771/v1/chat/completions -H 'content-type: application/json' \
  -d '{"model":"<alias>","messages":[{"role":"user","content":"hi"}],"stream":true}'
```

Point any OpenAI client at base URL `http://192.168.0.159:8771/v1` (api_key = your `HUB_TOKEN`, or anything if unset).

## Lifecycle

| Endpoint | Purpose |
|---|---|
| `GET /healthz` | liveness (wrapper only — never starts the engine) |
| `GET /readyz` | warm/cold/busy, child pid, model, idle seconds |
| `GET /agent` | machine-readable manifest for agents |
| `POST /admin/unload[?force=true]` | stop the engine to free memory |
| `ANY /v1/{path}` | OpenAI proxy (cold-starts the engine on first call) |

The engine **cold-starts on the first `/v1` call** and **idle-unloads** after `IDLE_UNLOAD_SECONDS` (default 600) so a forgotten chat model doesn't squat 8–30 GB. `KEEP_WARM=true` for an always-hot assistant. In-flight requests are tracked so idle-unload never kills a live generation.

## Env vars

| Var | Default |
|---|---|
| `LLAMACPP_BIN` | `llama-server` (PATH / Homebrew) |
| `LLAMACPP_MODEL` | — (required: path to a `.gguf`) |
| `LLAMACPP_ALIAS` | model file stem (name reported to clients) |
| `LLAMACPP_PORT` | `8771` (public) |
| `LLAMACPP_CHILD_PORT` | `18771` (internal `llama-server`) |
| `LLAMACPP_ARGS` | extra args, e.g. `--ctx-size 8192 -ngl 99 --flash-attn` |
| `IDLE_UNLOAD_SECONDS` | `600` |
| `KEEP_WARM` | `false` |
| `HUB_TOKEN` | unset |

> GGUF/Metal path. For MLX-native models use the **fastmlx** sidecar (`:8772`). Both complement Ollama, which also wraps llama.cpp but with less control.
