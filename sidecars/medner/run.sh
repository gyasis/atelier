#!/usr/bin/env bash
# medner sidecar — on-demand start (launchd-ize later). Apple-Silicon ML env per
# ~/.claude/rules/tools/ollama-apple-silicon.md: MPS fallback + no aggressive
# pre-alloc, and caffeinate -i so macOS App Nap doesn't throttle the headless server.
set -euo pipefail
cd "$(dirname "$0")"

export PYTORCH_ENABLE_MPS_FALLBACK=1
export PYTORCH_MPS_HIGH_WATERMARK_RATIO=0.0
export HF_HUB_DISABLE_TELEMETRY=1
export TOKENIZERS_PARALLELISM=false

PORT="${MEDNER_PORT:-8131}"
echo "medner → http://0.0.0.0:${PORT}  (device picked at runtime: MPS if available)"
exec caffeinate -i .venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port "$PORT"
