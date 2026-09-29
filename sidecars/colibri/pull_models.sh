#!/bin/bash
# pull_models.sh — download + prepare the Colibri model set on the Mac Studio, in order, resumably.
#
#   ~/Documents/code/atelier/sidecars/colibri/pull_models.sh            # run / resume everything
#   ~/Documents/code/atelier/sidecars/colibri/pull_models.sh deepseek_v4_flash   # one step
#
# Every download goes through pfetch (parallel ranges, sha256-verified): Hugging Face throttles
# each connection, and a single-connection fetch (hf download, the converters' own fetchers)
# measured ~45 KB/s. Re-running skips finished files and finished steps (marker files).
# Status is written to ~/.atelier/colibri-pull-status.json after every transition.
# Disk guard: a step does not start unless free space >= its need + 50 GB.
set -u
MODELS=${COLIBRI_MODELS_DIR:-$HOME/models/colibri}
TOOLS_PY=$HOME/services/colibri-sidecar/tools-venv/bin/python
CTOOLS=$HOME/services/colibri-sidecar/colibri-dev/c/tools
PFETCH=$HOME/.local/bin/pfetch
STATUS=$HOME/.atelier/colibri-pull-status.json
mkdir -p "$MODELS" "$(dirname "$STATUS")"

status() {  # status <step> <state> [detail]
  "$TOOLS_PY" - "$STATUS" "$1" "$2" "${3:-}" <<'PY'
import json, sys, time, os
p, step, state, detail = sys.argv[1:5]
d = json.load(open(p)) if os.path.exists(p) else {}
d[step] = {"state": state, "detail": detail, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
tmp = p + ".tmp"; json.dump(d, open(tmp, "w"), indent=1); os.replace(tmp, p)
PY
  echo "$(date +%H:%M:%S) [$1] $2 ${3:-}"
}

free_gb() { df -g "$MODELS" | awk 'NR==2{print $4}'; }

need_space() {  # need_space <step> <gb>
  local f; f=$(free_gb)
  if [ "$f" -lt $(( $2 + 50 )) ]; then
    status "$1" failed "disk guard: ${f} GB free < ${2} GB needed + 50 GB margin"; return 1
  fi
}

fetch() {  # fetch <step> <gb> <repo> <dir> [--revision R]
  local step=$1 gb=$2 repo=$3 dir=$4; shift 4
  need_space "$step" "$gb" || return 1
  status "$step" downloading "$repo -> $dir"
  if "$PFETCH" hf "$repo" --dir "$dir" --conns 24 "$@"; then
    return 0
  fi
  status "$step" failed "pfetch exited non-zero (re-run to resume)"; return 1
}

# A marker whose first word is SKIPPED records a deliberate drop, not a finished step:
# run_step reports it and moves on; delete the marker to fetch that model after all.
step_done() { [ -f "$MODELS/.$1.done" ]; }
step_skipped() { [ -f "$MODELS/.$1.done" ] && head -c 7 "$MODELS/.$1.done" | grep -q SKIPPED; }
mark_done() { touch "$MODELS/.$1.done"; status "$1" done "${2:-}"; }

run_step() {
  local s=$1
  if step_skipped "$s"; then echo "$(date +%H:%M:%S) [$s] skipped: $(cat "$MODELS/.$s.done")"; return 0; fi
  if step_done "$s"; then echo "$(date +%H:%M:%S) [$s] already done"; return 0; fi
  case $s in
    deepseek_v4_flash)
      fetch $s 167 deepseek-ai/DeepSeek-V4-Flash-0731 "$MODELS/deepseek_v4_flash" && mark_done $s ;;
    qwen38_flash_next)
      fetch $s 186 Qwen/Qwen3.8-Flash-Next-FP8 "$MODELS/qwen38_flash_next_fp8" \
            --revision bcd9f01ddc9cff2316eb84281bebcd5b058bddce && mark_done $s ;;
    qwen38_27b)
      fetch $s 110 Qwen/Qwen3.8-27B "$MODELS/qwen38_27b_src" || return 1
      status $s converting "convert_qwen36.py -> qwen38_27b_c"
      ( cd "$CTOOLS" && "$TOOLS_PY" convert_qwen36.py --model "$MODELS/qwen38_27b_src" \
            --out "$MODELS/qwen38_27b_c" ) || { status $s failed "conversion failed"; return 1; }
      [ -f "$MODELS/qwen38_27b_c/config.json" ] || { status $s failed "no config.json in output"; return 1; }
      rm -rf "$MODELS/qwen38_27b_src"          # the source is re-downloadable; the container is the product
      mark_done $s "converted; source removed" ;;
    glm53_flash)
      fetch $s 340 zai-org/GLM-5.3-Flash "$MODELS/glm53_flash_src" || return 1
      status $s converting "convert_glm53.py --indir (source shards deleted as converted)"
      ( cd "$CTOOLS" && "$TOOLS_PY" convert_glm53.py --indir "$MODELS/glm53_flash_src" \
            --outdir "$MODELS/glm53_flash_i4" --min-free-gb 30 ) \
        || { status $s failed "conversion failed (re-run resumes: .converted.json)"; return 1; }
      [ -f "$MODELS/glm53_flash_i4/config.json" ] || { status $s failed "no config.json in output"; return 1; }
      rm -rf "$MODELS/glm53_flash_src"
      mark_done $s "converted; source removed" ;;
    glm53)
      fetch $s 420 Justvugg/GLM-5.3-colibri-int4-g64 "$MODELS/glm53_i4" && mark_done $s ;;
    deepseek_v41_flash)
      fetch $s 511 deepseek-ai/DeepSeek-V4.1-Flash "$MODELS/deepseek_v41_flash" || return 1
      status $s preparing "prepare_dsv41.py"
      ( cd "$CTOOLS" && "$TOOLS_PY" prepare_dsv41.py --model "$MODELS/deepseek_v41_flash" ) \
        || { status $s failed "prepare_dsv41 failed"; return 1; }
      mark_done $s "prepared" ;;
    *) echo "unknown step $s"; return 2 ;;
  esac
}

# glm53_flash is defined but not in the default order (dropped 2026-09-29): pass it by name to fetch it.
ORDER="deepseek_v4_flash qwen38_flash_next qwen38_27b glm53 deepseek_v41_flash"
STEPS=${*:-$ORDER}
fails=0
for s in $STEPS; do
  run_step "$s" || fails=$((fails + 1))    # a failed step does not block the next one
done
echo "$(date +%H:%M:%S) finished with $fails failed step(s); status: $STATUS"
[ $fails -eq 0 ] && echo __DONE__ || echo __DONE_WITH_FAILURES__
