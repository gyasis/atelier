#!/usr/bin/env bash
# Atelier — service completeness + health audit.
#
# Catches the failure class behind the 2026-05-25 incident: a sidecar
# (OmniVoice) was running via `nohup`, never codified as a launchd service in
# this repo, and silently died on a Mac reboot — degrading the podcast to the
# Kokoro fallback. This audit makes that gap loud instead of silent.
#
# Checks, in order:
#   1. coverage  — every sidecars/<name>/ has a launchd/io.macstudio.hub.<name>.plist
#   2. installed — every repo plist is copied into ~/Library/LaunchAgents
#   3. loaded    — every plist's Label is loaded in launchd
#   4. healthy   — every service answers /healthz on its port
#
# Exit 0 = all green. Exit 1 = at least one gap (CI/cron-friendly).
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")"/.. && pwd)"
LAUNCHAGENTS_DIR="$HOME/Library/LaunchAgents"
UID_GUI="$(id -u)"
GREEN=$'\033[1;32m'; RED=$'\033[1;31m'; YEL=$'\033[1;33m'; CYAN=$'\033[1;36m'; NC=$'\033[0m'
fail=0

# Canonical service -> health port registry. Add a line here when a new model
# joins the hub; the audit then enforces it has a plist, is loaded, and is up.
port_for() {
  case "$1" in
    kokoro)    echo 8765 ;;
    dia)       echo 8769 ;;
    omnivoice) echo 8770 ;;
    comfyui)   echo 8188 ;;
    *)         echo "" ;;
  esac
}

# ComfyUI isn't a FastAPI sidecar — it has no /healthz. Use its native probe.
health_path_for() {
  case "$1" in
    comfyui) echo "/system_stats" ;;
    *)       echo "/healthz" ;;
  esac
}

echo "${CYAN}== Atelier service audit ==${NC}"

# 1. Every sidecar with code MUST have a launchd plist (the OmniVoice gap).
echo "${CYAN}-- coverage: sidecar code -> launchd plist --${NC}"
for d in "$REPO_DIR"/sidecars/*/; do
  [ -d "$d" ] || continue
  name=$(basename "$d")
  if [ -f "$REPO_DIR/launchd/io.macstudio.hub.$name.plist" ]; then
    echo "  ${GREEN}OK${NC}      $name"
  else
    echo "  ${RED}MISSING${NC} $name has sidecar code but NO launchd/io.macstudio.hub.$name.plist — will NOT survive reboot"
    fail=1
  fi
done

# 2-4. Every repo plist: installed, loaded, healthy.
echo "${CYAN}-- plist -> installed / loaded / healthy --${NC}"
for plist in "$REPO_DIR"/launchd/*.plist; do
  [ -f "$plist" ] || continue
  base=$(basename "$plist")
  label=$(/usr/libexec/PlistBuddy -c "Print :Label" "$plist" 2>/dev/null || echo "")
  name=${label##*.}

  if [ -f "$LAUNCHAGENTS_DIR/$base" ]; then inst="${GREEN}installed${NC}"; else inst="${RED}NOT-installed${NC}"; fail=1; fi
  if launchctl print "gui/$UID_GUI/$label" >/dev/null 2>&1; then loaded="${GREEN}loaded${NC}"; else loaded="${RED}NOT-loaded${NC}"; fail=1; fi

  port=$(port_for "$name")
  if [ -n "$port" ]; then
    hpath=$(health_path_for "$name")
    if curl -sf --max-time 3 "http://localhost:$port$hpath" >/dev/null 2>&1; then
      health="${GREEN}healthy :$port${NC}"
    else
      health="${RED}unhealthy :$port${NC}"; fail=1
    fi
  else
    health="${YEL}no port in registry${NC}"
  fi
  echo "  ${name}: ${inst} · ${loaded} · ${health}"
done

if [ "$fail" -eq 0 ]; then
  echo "${GREEN}== all services codified, loaded, and healthy ==${NC}"
else
  echo "${RED}== gaps found — see above ==${NC}"
  exit 1
fi
