# Active Context

**Last Updated**: 2026-06-16 09:04:42

## Current Focus
feat(governor): real per-sidecar memory (RSS) in tenants, not hardcoded guesses

Sidecars carried no mem_gb, so the dashboard donut filled it from a hardcoded
SIDECAR_MEM table (kokoro=4, whisper=2, dia=8 …) — and only when warm. Those were
fiction: kokoro (82M params) actually uses 0.1GB, not 4GB.

The poller now measures each WARM sidecar's real resident memory: find its listening
process (by --port / <name>-sidecar path) and sum its process subtree RSS — so a
proxy sidecar's llama-server / mlx child (which holds the model) is included. Feeds
mem_gb into the tenant data; the dashboard already prefers t.mem_gb over the estimate,
so the donut + Top Memory now show real sidecar footprints with no UI change.

Verified: kokoro listening PID RSS = 0.10GB, matches the reported value exactly.
Caveat: RSS may undercount GPU/Metal-buffer memory for MPS-based sidecars; the
llama.cpp/mlx child-process RSS (the big consumers) is accurate.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>

## Recent Changes
```
 .claude/activity_stream.md          |  12 +
 .../snapshot_latest.json            |   2 +-
 .../history/2026-06-10-37bba2bb.md  | 236 +++++++++++-
 .../gyasisutton/activeContext.md    |  35 +-
 .../private/gyasisutton/progress.md |   2 +-
 5 files changed, 267 insertions(+), 20 deletions(-)
```

## Modified Files
.claude/activity_stream.md
.claude/session_snapshots/snapshot_latest.json
.specstory/history/2026-06-10-37bba2bb.md
memory-bank/private/gyasisutton/activeContext.md
memory-bank/private/gyasisutton/progress.md

## Next Actions
- Continue implementation
- Run tests
- Create checkpoint
