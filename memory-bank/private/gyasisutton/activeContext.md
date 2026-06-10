# Active Context

**Last Updated**: 2026-06-10 12:35:37

## Current Focus
fix(dashboard): donut shows real memory — label untracked 'system / other'

The memory donut charted only Atelier tenants and folded everything else into an
overstated 'free' wedge, so ~20GB of real usage (OS, other apps, the dashboard,
MLX caches) hid inside 'free' with no identifier — and the chart went stale when
nothing was loaded, leaving a phantom segment painted on (the unidentified blue
chunk the user circled: an already-unloaded model still drawn).

Now: segments = tenants + a labeled grey 'system / other' wedge for real-but-
untracked memory (resident_gb − tenant_sum) + ACTUAL free_gb. Always redraws, so
no stale phantom. Confirmed via redpen: donut said 'free 58GB' while real free
was ~39GB; the missing ~19GB now shows as 'system / other'.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>

## Recent Changes
```
 .../activity_stream.md   |   12 +
 .../snapshot_latest.json |    2 +-
 ...026-06-10-37bba2bb.md | 1046 +++++-
 .../activeContext.md     |   39 +-
 .../progress.md          |    2 +-
 5 files changed, 1073 insertions(+), 28 deletions(-)
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
