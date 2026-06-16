# Active Context

**Last Updated**: 2026-06-16 08:43:15

## Current Focus
fix(dashboard): Top Memory table sorts by memory, not CPU

The 'Top Memory' table was fed by a Tauri command running 'ps … -r' (sort by
CPU) then take 10 — so it showed RSS of the top-CPU processes, not the actual
memory hogs. Add a governor GET /top-processes (ps -axo pid,rss,comm, sorted by
RSS desc) and fetch it directly from the dashboard (CSP null, no Rust rebuild);
falls back to the old Tauri command if the governor is down. The panel now shows
the real highest-memory consumers — what matters for memory pressure.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>

## Recent Changes
```
 .claude/activity_stream.md          | 12 +++
 .../snapshot_latest.json            |  2 +-
 .../history/2026-06-10-37bba2bb.md  | 64 ++++++++++++-
 .../gyasisutton/activeContext.md    | 25 +++--
 .../private/gyasisutton/progress.md |  2 +-
 5 files changed, 89 insertions(+), 16 deletions(-)
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
