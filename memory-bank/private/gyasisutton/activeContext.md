# Active Context

**Last Updated**: 2026-06-13 14:33:05

## Current Focus
feat(proxy): auto-size num_ctx to the prompt (stop silent truncation)

The 16K context cap silently truncates long inputs (e.g. whisper summarizing a
long transcript — the tail just vanishes). The capturing proxy now estimates the
prompt's token count and, for Ollama chat/generate, grows num_ctx to fit — next
power of two, bounded by ATELIER_PROXY_CTX_CEILING (32768) and the model's native
max (from /api/show, cached). Short prompts stay at the cheap 16K; a caller's own
num_ctx is always respected. The chosen window is recorded in telemetry and shown
in the dashboard hover ('context window: 32768 (auto-sized)').

Verified live: an ~18.2K-token prompt ingested fully (prompt_eval_count=18217)
with the runner reloaded at n_ctx=32768 — previously it would have clipped to 16384.

Note: the gate's est_gb doesn't yet add the larger KV footprint of a grown ctx;
the live-memory backstop still prevents OOM (holds/queues if real RAM is short).

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>

## Recent Changes
```
 .../activity_stream.md    |  12 +
 .../snapshot_latest.json  |   2 +-
 ...2026-06-10-37bba2bb.md | 121 +++++-
 .../activeContext.md      |  32 +-
 .../progress.md           |   2 +-
 5 files changed, 153 insertions(+), 16 deletions(-)
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
