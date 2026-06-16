# Active Context

**Last Updated**: 2026-06-16 08:46:20

## Current Focus
fix(hud): donut live-updates + shows real memory (same fix as index.html)

The HUD's memory donut was gated on '&& loaded.length', so whenever nothing was
loaded it SKIPPED the redraw and froze on its last frame — which is why it looked
like the only component that wouldn't live-update (it had a refresh timer all
along; the update was just conditionally skipped). Now it always redraws from the
latest pressure: tenants + a labelled 'system / other' wedge for real-but-untracked
memory + the actual free_gb (not 64−tenants), with the center showing real resident
GB as the pressure signal.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>

## Recent Changes
```
 .claude/activity_stream.md          |  12 +
 .../snapshot_latest.json            |   2 +-
 .../history/2026-06-10-37bba2bb.md  | 595 +++++++++++-
 .../gyasisutton/activeContext.md    |  28 +-
 .../private/gyasisutton/progress.md |   2 +-
 5 files changed, 620 insertions(+), 19 deletions(-)
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
