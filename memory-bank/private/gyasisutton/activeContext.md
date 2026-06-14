# Active Context

**Last Updated**: 2026-06-14 21:15:36

## Current Focus
feat(gate): KV-aware est_gb — budget scales with the context window

The gate estimated weights × 1.15 (a flat ~KV-at-16K markup), so when the proxy
auto-grew num_ctx the reserved memory stayed too low — the live backstop caught
OOM but the budget ran optimistic. Now est_gb = weights + KV(ctx):
- KV rate (GB/token) computed per model from /api/show architecture
  (2 × layers × kv_heads × head_dim × dtype), fed to the gate (set_kv_rate).
- est_gb(model, ctx) scales the KV term with the context; the proxy passes the
  auto-sized ctx so admission books the real footprint. Falls back to the flat
  markup when architecture is unknown.
- Poller warms KV rates for loaded models so direct /admit callers benefit too.
- est_gb surfaced in telemetry + dashboard hover ('memory reserved: 8.7 GB').

Validated against reality: mistral est @16K = 6.55GB ≈ 6.4GB observed loaded;
a live 32K-context call reserved 8.7GB (flat markup would've been ~4.7GB).
14/14 admission tests pass.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>

## Recent Changes
```
 .claude/activity_stream.md          |  12 +
 .../snapshot_latest.json            |   2 +-
 .../history/2026-06-10-37bba2bb.md  | 673 +++++++++++-
 .../gyasisutton/activeContext.md    |  40 +-
 .../private/gyasisutton/progress.md |   2 +-
 5 files changed, 706 insertions(+), 23 deletions(-)
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
