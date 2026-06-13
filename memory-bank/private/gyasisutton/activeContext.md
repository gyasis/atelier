# Active Context

**Last Updated**: 2026-06-13 17:52:38

## Current Focus
feat(governor): /agent discovery advertises the gate, proxy, and docs

The hub manifest (GET /agent, ?expand=true) is the single entry point an agent
hits to learn Atelier — but it predated this session's work. Now it advertises:
- llm_access: the SMART FRONT DOOR (POST /llm/{backend}/{path}) with backends,
  examples, the manual /admit·/release gate, and the clients/atelier_admit.py helper
- control_plane: adds /llm, /budget, /admit, /release alongside the existing routes
- docs: pointers to SIDECAR_CALLS.md (curl cookbook) and LLM_ADMISSION_QUEUE.md
- how_to_start: rewritten to steer agents to /llm (admit+autoctx+capture) for LLMs
  and direct sidecar calls for TTS/ASR

So a discovering agent learns the memory-aware way to use the hub, not just the
raw backends. ?expand=true still inlines every sidecar's full method manifest.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>

## Recent Changes
```
 .claude/activity_stream.md          |  12 +
 .../snapshot_latest.json            |   2 +-
 .../history/2026-06-10-37bba2bb.md  | 539 +++++++++++-
 .../gyasisutton/activeContext.md    |  37 +-
 .../private/gyasisutton/progress.md |   2 +-
 5 files changed, 567 insertions(+), 25 deletions(-)
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
