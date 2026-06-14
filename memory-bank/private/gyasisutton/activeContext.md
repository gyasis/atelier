# Active Context

**Last Updated**: 2026-06-14 18:44:52

## Current Focus
feat(cli): add 'atelier discover' — human-friendly hub discovery

Stdlib-only CLI wrapping the governor's /agent manifest:
  atelier discover         hub overview — LLM front door, control plane, sidecars, docs
  atelier discover <name>  drill into one sidecar's methods/params + curl examples
  atelier discover --json  raw manifest (pipeable to jq)
  atelier discover --plain no colour

Reads ATELIER_GOVERNOR_URL (default :8799); clean error if the governor is down.
Installed by symlinking cli/atelier into ~/.local/bin (matches the gentle-eye CLI
convention). Same data an agent gets from GET /agent?expand=true, formatted for a human.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>

## Recent Changes
```
 .claude/activity_stream.md          |  12 +
 .../snapshot_latest.json            |   2 +-
 .../history/2026-06-10-37bba2bb.md  | 270 +++++++++++-
 .../gyasisutton/activeContext.md    |  33 +-
 .../private/gyasisutton/progress.md |   2 +-
 5 files changed, 296 insertions(+), 23 deletions(-)
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
