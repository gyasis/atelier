# Active Context

**Last Updated**: 2026-06-10 12:15:34

## Current Focus
fix(governor): break self-perpetuating probe loop; make internal calls visible

The stats watcher's benchmark probe is itself an /api/generate, which the log
tailer recorded as 'a new call' → triggering the next probe → a 60s loop that
fired forever and pinned the last-loaded model resident via keep_alive (qwen3-vl,
24GB, never freed; reloaded seconds after any restart).

Fix:
- The watcher now ignores via='governor-probe' entries when deciding to probe, so
  its own probe can't re-trigger it. Probes fire ONLY after a real call now.
- The probe's GIN line is marked in _internal_skip and skipped by the log tailer,
  so it never masquerades as user traffic.
- The probe is recorded as a LABELED, visible entry (via='governor-probe', with a
  '▣ governor warm-up/benchmark probe …' prompt) so the dashboard shows exactly
  what the governor is doing — answering 'where are the prompts / what's happening'.

Verified: 0 probes over a 70s idle window (was ~1/min), qwen3-vl unloaded itself
(24GB reclaimed), and a real proxied call triggers exactly one labeled probe.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>

## Recent Changes
```
 .../activity_stream.md    |  12 +
 .../snapshot_latest.json  |   2 +-
 ...2026-06-10-37bba2bb.md | 230 +++++-
 .../activeContext.md      |  38 +-
 .../progress.md           |   2 +-
 5 files changed, 264 insertions(+), 20 deletions(-)
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
