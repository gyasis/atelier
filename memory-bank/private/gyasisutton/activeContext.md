# Active Context

**Last Updated**: 2026-06-10 11:27:41

## Current Focus
feat(governor): Phase 6 capturing proxy + est heuristic + busy-state fix

- POST /llm/{backend}/{path}: opt-in proxy that admits → forwards (stream +
  non-stream) → records {model, prompt, in_tok, out_tok, tok_s, status, ms} into
  recent_calls → releases. Tagged via:proxy; log tailer skips the dup GIN line so
  the rich entry wins. Verified live: mlxlm call captured prompt + 36/4 tokens.
- est_gb: parameter-count heuristic from the model name (…-0.5b→0.35GB, …-32b→
  22.4GB) for sidecar-served models absent from any catalog, instead of the 18GB
  blind default that wrongly held a tiny call behind the memory backstop.
- poll_ollama: stop labelling every resident model 'busy' (it keyed on expires_at,
  which every loaded model has) — 'busy' now means actually generating, so the
  dashboard card only pulses on real work; surfaces expires_at for the UI.
- bump /healthz version to 0.8-proxy; 14/14 admission tests pass.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>

## Recent Changes
```
 .../activity_stream.md    |  12 +
 .../snapshot_latest.json  |   2 +-
 ...2026-06-10-37bba2bb.md | 699 +++++-
 .../activeContext.md      |  36 +-
 .../progress.md           |   2 +-
 5 files changed, 729 insertions(+), 22 deletions(-)
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
