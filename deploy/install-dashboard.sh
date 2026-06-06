#!/usr/bin/env bash
# Atelier dashboard — rebuild → install over /Applications → relaunch, in one step.
#
# WHY: `cargo tauri build` writes the .app to target/release/bundle/, but macOS
# LaunchServices launches the REGISTERED copy (usually /Applications). So a plain
# rebuild leaves you staring at the old installed app. This script replaces the
# installed copy with the fresh build so what you open is what you built.
#
# Usage:  deploy/install-dashboard.sh           # rebuild + install + relaunch
#         deploy/install-dashboard.sh --no-run  # rebuild + install only
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")"/.. && pwd)"
DASH_DIR="$REPO_DIR/dashboard"
APP_NAME="AtelierDashboard.app"
BUILT="$DASH_DIR/src-tauri/target/release/bundle/macos/$APP_NAME"
INSTALLED="/Applications/$APP_NAME"
RUN=1
[ "${1:-}" = "--no-run" ] && RUN=0

say() { echo -e "\033[1;36m[dashboard]\033[0m $*"; }

command -v cargo >/dev/null || { source "$HOME/.cargo/env" 2>/dev/null || true; }
command -v cargo >/dev/null || { echo "cargo not found — install Rust toolchain first"; exit 1; }

say "building (release)…"
( cd "$DASH_DIR" && cargo tauri build --bundles app )
[ -d "$BUILT" ] || { echo "build artifact missing: $BUILT"; exit 1; }

say "quitting running app…"
osascript -e 'tell application "AtelierDashboard" to quit' 2>/dev/null || true
pkill -f "$APP_NAME/Contents/MacOS/app" 2>/dev/null || true
sleep 2

say "installing over $INSTALLED …"
ditto "$BUILT" "$INSTALLED"
# re-register so LaunchServices opens the fresh copy
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$INSTALLED" 2>/dev/null || true

say "installed build: $(stat -f '%Sm' "$INSTALLED/Contents/MacOS/app")"
if [ "$RUN" = "1" ]; then
  say "relaunching…"
  open "$INSTALLED"
fi
say "done."
