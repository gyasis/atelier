# PRD — Dia Chunked Podcast (parked, for later)

**Status:** Parked. Not active work. Triggered when user explicitly schedules.
**Date:** 2026-05-24
**Codename:** Atelier (Dia upgrade tier)
**Context:** Today we deployed Dia 1.6B as the voice-clone sidecar at `:8769` and wired the webapp to send whole-episode scripts to it. **It produced gibberish** — Gemini's blind audio analysis confirmed: model collapses past ~20 sec of expected output, hidden states diverge into "chipmunk speech → mechanical stuttering → digital screeching." Reverted to Kokoro per-line for live podcast playback (works fine, voices are flat). This PRD captures the path to making Dia actually useful for podcasts.

---

## Problem

Dia 1.6B produces **2-host dialogue with natural prosody, voice cloning, and `(laughs)/(sighs)` cues** — vastly better than Kokoro's per-line flat synth. But it's **architecturally limited** to ~20 sec of output per call. Our podcast episodes are 2-3 minutes each.

The Gemini diagnosis on 2026-05-24 (`docs/research/2026-05-24-dia-failure-mode.md`, will create) confirmed empirically that a single 2784-char script collapses Dia into garbage after ~3 sec of coherent output.

## Hypothesis

**Chunk the script into 3-5 turn windows** (each producing ≤20 sec of expected audio), generate each via Dia, splice client-side. Trade-offs:

- ✅ Each chunk fits Dia's stable window → coherent multi-speaker output with voice cloning + nonverbals
- ✅ Chunks can share the same prefix audio for voice cloning consistency
- ✅ Splicing yields one continuous WAV, indistinguishable from a single-shot result to the listener
- ❌ Loses cross-chunk dialogue context (Dia might say "as I was saying" but the previous chunk didn't say that)
- ❌ More Dia calls per episode → longer total wall-time (10 chunks × 60s = 10 min instead of one 100s call)
- ❌ Potential pacing / volume discontinuities at chunk boundaries

## Proposed architecture (when un-parked)

### Stage 1: Webapp chunks the script

In `src/lib/server/dia.ts`, add `splitScriptIntoChunks(turns, maxTurnsPerChunk=4)`:
- Walk the turn list, accumulate turns until either:
  - Chunk has hit `maxTurnsPerChunk` turns, OR
  - Cumulative `text.length` for the chunk exceeds ~400 chars (≈ ~15s of audio at normal pace)
- Each chunk gets a deterministic `chunk_index` so cache keys are reproducible.
- Optional: bias chunk boundaries to fall on speaker changes for cleaner splice points.

### Stage 2: Generate chunks via Dia (parallel or serial)

For each chunk:
- POST to existing `/api/podcast/episode` (or new `/api/podcast/chunk`).
- Dia sees 3-5 turns, produces a coherent ~15 sec WAV with voice cloning ON.
- Cache as `data/podcast-chunk-cache/<sha256>.wav` (separate cache from full-episode for clarity).

Serial vs parallel:
- **Serial** — one chunk at a time on Dia (sidecar is single-flight via Semaphore anyway, parallelism is fake)
- Total wall: `chunks * dia_chunk_time` (e.g. 10 chunks × 60s = 10 min for one episode)

### Stage 3: Splice chunks on the server, return single episode WAV

Server-side (or client-side — TBD): use ffmpeg or Web Audio API to concat the chunk WAVs into one continuous episode WAV. Write to `data/podcast-episode-cache/<episode_hash>.wav` with metadata. Player just plays the spliced result like today.

Boundary smoothing: optional 100-200ms crossfade between chunks to hide any volume discontinuity.

### Stage 4: Background upgrade tier (the "two-tier" architecture the user proposed)

User clicks Play → Kokoro generates per-line WAVs in ~15 sec → episode plays immediately with flat voices. In parallel, a background job picks up the same script + dispatches to Dia-chunked. When the chunked Dia WAV finishes, the cache entry is upgraded:
- `data/podcast-episode-cache/<hash>.wav` is replaced (or both kept, tagged by `engine` in metadata)
- Player's "↻ Replay" picks up the better version on next read

Background job control:
- `/api/podcast/episodes/upgrade` POST endpoint queues a Dia upgrade for a given cache key
- `/api/podcast/episodes/upgrades` GET endpoint shows the queue + status
- UI: each episode shows a "🎙️ Kokoro" or "✨ Dia (upgrading 3/10)" or "✨ Dia (ready)" badge

## Open questions

- **Cross-chunk continuity** — does Dia work if we pass the LAST 1-2 turns of the previous chunk as additional reference? README hints at conversational context being a thing. Worth experimenting.
- **Splice volume normalization** — chunks may have different volume baselines. Normalize each before concat?
- **Cache invalidation** — if we tweak chunking params (max turns, max chars), old cache entries become stale. Version the cache key with `chunk_v=1`.
- **Worst-case Dia time** — even chunked, 10 minutes per episode for Dia upgrade. For a 10-episode queue that's 1.5 hr background work. Need a UI affordance that surfaces "still upgrading, X% done" without locking the user.

## Success criteria

- 19-turn / 2784-char podcast generates **coherent intelligible English** end-to-end via Dia chunked path.
- Multi-speaker voice cloning is preserved (LEO + SARAH timbres consistent across chunks).
- Wall-time for a 10-episode queue's full Dia upgrade ≤ 90 min on M1 Max.
- User can listen to Kokoro version immediately while Dia version generates in background.

## Out of scope

- Migrating off Dia entirely (it's still the best open-source multi-speaker option as of 2026-05-24).
- Long-form models (Dia2 needs CUDA per its README; not viable on M1 Max as of today).
- Per-turn synth via Dia (Dia's strength is the multi-speaker interleave within a chunk; per-turn would lose that and be Kokoro-with-more-overhead).

## Trigger to un-park

When user says "do the Dia chunking" OR when the limitation of Kokoro's flat voices becomes the user-experience blocker on the podcast feature.
