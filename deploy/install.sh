#!/usr/bin/env bash
# Atelier — fresh-Mac bootstrap.
# Creates venvs, installs deps, symlinks sidecar code from this repo into
# ~/services/, copies launchd plists, downloads models, loads services.
# Idempotent — re-running on an already-installed machine is safe.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")"/.. && pwd)"
SERVICES_DIR="$HOME/services"
MODELS_DIR="$HOME/models"
LAUNCHAGENTS_DIR="$HOME/Library/LaunchAgents"
LOGS_DIR="$HOME/Library/Logs"
UID_GUI="$(id -u)"

say() { echo -e "\033[1;36m[atelier]\033[0m $*"; }
die() { echo -e "\033[1;31m[atelier] ERROR:\033[0m $*" >&2; exit 1; }

# ---------- 1. Prerequisites ----------
say "checking prerequisites…"
command -v /opt/homebrew/bin/uv >/dev/null || die "uv not found at /opt/homebrew/bin/uv — install Homebrew + 'brew install uv'"
command -v /opt/homebrew/bin/brew >/dev/null || die "Homebrew not found at /opt/homebrew/bin"
xcode-select -p >/dev/null 2>&1 || die "Xcode Command Line Tools not installed — run 'xcode-select --install'"
# whisper-sidecar decodes audio (mp3/m4a/etc.) via ffmpeg. Warn, don't die —
# the other sidecars don't need it.
command -v /opt/homebrew/bin/ffmpeg >/dev/null || say "  WARNING: ffmpeg not found — whisper-sidecar needs it to decode audio. Run 'brew install ffmpeg'."
# llamacpp-sidecar wraps llama.cpp's `llama-server`. Warn, don't die — optional sidecar.
command -v /opt/homebrew/bin/llama-server >/dev/null || say "  WARNING: llama-server not found — llamacpp-sidecar needs it. Run 'brew install llama.cpp' and point LLAMACPP_MODEL at a .gguf."

# ---------- 2. Directories ----------
say "creating dirs…"
mkdir -p "$SERVICES_DIR" "$LAUNCHAGENTS_DIR" "$LOGS_DIR"
mkdir -p "$MODELS_DIR"/{kokoro,voice-refs,hf-cache,unet,clip,vae,loras,video,whisper,checkpoints,gguf}
mkdir -p "$HOME/outputs/transcripts"

# ---------- 3. Sidecar venvs ----------
for SC in kokoro dia omnivoice whisper llamacpp fastmlx mlxlm governor; do
  SC_DIR="$SERVICES_DIR/${SC}-sidecar"
  case "$SC" in
    kokoro)    SC_DIR="$SERVICES_DIR/kokoro-sidecar" ;;
    dia)       SC_DIR="$SERVICES_DIR/voice-clone-sidecar" ;;
    omnivoice) SC_DIR="$SERVICES_DIR/omnivoice-sidecar" ;;
    whisper)   SC_DIR="$SERVICES_DIR/whisper-sidecar" ;;
    llamacpp)  SC_DIR="$SERVICES_DIR/llamacpp-sidecar" ;;
    fastmlx)   SC_DIR="$SERVICES_DIR/fastmlx-sidecar" ;;
    mlxlm)     SC_DIR="$SERVICES_DIR/mlxlm-sidecar" ;;
    governor)  SC_DIR="$SERVICES_DIR/governor-sidecar" ;;
  esac
  say "setting up sidecar: $SC -> $SC_DIR"
  mkdir -p "$SC_DIR"
  if [ ! -d "$SC_DIR/.venv" ]; then
    /opt/homebrew/bin/uv venv --python 3.12 "$SC_DIR/.venv"
  fi
  # Install requirements pinned in repo
  source "$SC_DIR/.venv/bin/activate"
  /opt/homebrew/bin/uv pip install -q -r "$REPO_DIR/sidecars/$SC/requirements.txt"
  deactivate
  # Symlink all .py modules from the repo (server.py + helpers e.g. predictor.py)
  for PY in "$REPO_DIR/sidecars/$SC"/*.py; do
    [ -e "$PY" ] && ln -sfn "$PY" "$SC_DIR/$(basename "$PY")"
  done
done

# ---------- 4. launchd plists ----------
say "installing launchd plists…"
for PLIST in "$REPO_DIR"/launchd/*.plist; do
  BASENAME=$(basename "$PLIST")
  cp "$PLIST" "$LAUNCHAGENTS_DIR/$BASENAME"
done

# ---------- 5. Models ----------
say "downloading models (if missing)…"

# Kokoro model + voices
if [ ! -f "$MODELS_DIR/kokoro/kokoro-v1.0.fp16.onnx" ]; then
  say "  fetching kokoro-v1.0.fp16.onnx (169 MB)"
  curl -sSL -o "$MODELS_DIR/kokoro/kokoro-v1.0.fp16.onnx" \
    https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.fp16.onnx
fi
if [ ! -f "$MODELS_DIR/kokoro/voices-v1.0.bin" ]; then
  say "  fetching voices-v1.0.bin (27 MB)"
  curl -sSL -o "$MODELS_DIR/kokoro/voices-v1.0.bin" \
    https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin
fi

# Dia downloads on first run via HF_HOME — no install-time fetch needed.
# OmniVoice (k2-fsa/OmniVoice) likewise auto-downloads its 13 model files on
# first startup via HF_HOME — no install-time fetch needed.
# Whisper (mlx-community/whisper-large-v3-turbo, ~1.6 GB) auto-downloads into
# HF_HOME on first /transcribe — no install-time fetch needed.

# Voice refs (LEO + SARAH) for Dia cloning
if [ ! -f "$MODELS_DIR/voice-refs/leo_ref.wav" ]; then
  say "  WARNING: $MODELS_DIR/voice-refs/leo_ref.wav not found."
  say "    Provide a 5-10 sec deep-male reference clip + matching transcript before first Dia run."
fi
if [ ! -f "$MODELS_DIR/voice-refs/sarah_ref.wav" ]; then
  say "  WARNING: $MODELS_DIR/voice-refs/sarah_ref.wav not found."
  say "    Provide a 5-10 sec female reference clip + matching transcript before first Dia run."
fi

# ComfyUI is a separate concern (it has its own install path + Wan2.1 weights are 50+ GB).
# See docs/ARCHITECTURE.md §5.4 for that one.

# ---------- 6. launchctl bootstrap ----------
say "bootstrapping services with launchctl…"
for PLIST in "$LAUNCHAGENTS_DIR"/io.macstudio.hub.*.plist; do
  LABEL=$(/usr/libexec/PlistBuddy -c "Print :Label" "$PLIST" 2>/dev/null || true)
  if [ -z "$LABEL" ]; then continue; fi
  # bootout first in case already loaded (silently)
  launchctl bootout "gui/$UID_GUI/$LABEL" 2>/dev/null || true
  launchctl bootstrap "gui/$UID_GUI" "$PLIST"
done

# ---------- 7. Verify ----------
say "verifying services (give them ~30s to warm)…"
sleep 30
for PORT in 8765 8766 8769 8770 8771 8772 8773 8799; do
  if curl -sf --max-time 3 "http://localhost:$PORT/healthz" >/dev/null; then
    echo -e "  \033[1;32m✓\033[0m http://localhost:$PORT/healthz"
  else
    echo -e "  \033[1;31m✗\033[0m http://localhost:$PORT/healthz"
  fi
done

say "done. tail logs at $LOGS_DIR/*-sidecar.err.log for warmup progress."
