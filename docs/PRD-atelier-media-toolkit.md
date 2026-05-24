# PRD — Atelier Media-Toolkit Skills (for later)

**Status:** Draft, parked for future work (2026-05-24)
**Project codename:** **Atelier** (formerly "atelier")
**Author:** drafted with Claude during the Dia 1.6B voice-clone deployment
**Important:** This PRD is **planning-only**. Implementation is deferred until explicitly scheduled. Captured here so the work doesn't get forgotten and so the next agent picking this up has the full design context.

---

## 1. Problem

In a single recent session we hit this pattern five times:

1. `ffprobe` to inspect Wan2.1 video output (resolution, fps, duration).
2. `ffmpeg` work to trim a Kokoro reference clip for Dia voice cloning.
3. Pending: download + trim a WSJ podcast clip for a LEO voice reference.
4. Pending: resize / format-convert ComfyUI image outputs.
5. Open question: extract audio from a YouTube episode for a TTS reference.

Each is **the same shape of problem** — small media-manipulation task, agent reaches for a different one-off shell command, no consistency, no memory of which package worked best, no skill file capturing it. Next session reinvents.

**The user's framing (2026-05-24):** *"these are media helper skills. Let's have a one-stop tool shop for Claude Code and any agent to use on a regular basis."*

## 2. Scope

Four skills, all driven through the same package-selection method:

| # | Skill | Domain | Example operations |
|---|---|---|---|
| 1 | `audio-edit` | Audio manipulation | trim, normalize, format-convert (mp3↔wav↔flac), splice/concat, silence-trim, voice-isolate, sample-rate-convert |
| 2 | `video-edit` | Video manipulation | trim, concat, transcode, extract-frames, change-res/fps, mux-audio, thumbnail-generate |
| 3 | `image-edit` | Image manipulation | resize, crop, rotate, format-convert, color-adjust, batch-ops, transparent-bg, watermark |
| 4 | `search-awesome-db` | Package discovery | query the local `data/github_awesome.db` (1038 projects, 35 videos) by topic/tags/description for relevant tools |

**Explicit non-goals (v1):**
- Heavy ML inference (TTS, ASR, image gen, video gen) — those live as Atelier sidecars, not local skills.
- A unified GUI / dashboard. Skills are CLI/script-shaped.
- Cloud-hosted media APIs (ElevenLabs, Cloudinary, etc.). Local + open-source only.
- Real-time / streaming pipelines. Batch ops only.
- Esoteric codec support — only what the picked libraries already handle.

## 3. Research method (the part the user explicitly asked for)

For each skill, follow a **three-source research process** before writing code:

1. **deeplakesearch multihop** — query the user's pkdb for prior notes/articles on the domain. Multi-hop because the first query usually hits adjacent topics; a refining second query lands the actual package names.
   - Example for `audio-edit`: `"python audio editing libraries comparison"` → refine with `"pydub vs librosa vs soundfile vs ffmpeg-python"`.
2. **Gemini deep research** — focused prompt for a 1500-2000 word state-of-the-art comparison of candidate packages, biased toward late-2025 / early-2026 maturity, Apple Silicon arm64 compatibility, license clarity, install simplicity. Same shape as the TTS + video-gen research we ran this week.
3. **`search-awesome-db`** (once that skill exists — bootstrap with raw SQL for now) — query the local awesome DB for projects matching the domain keywords. The user has curated 1038 projects, many are media tools. Should surface community-respected picks the other two sources miss.

**Synthesize the three sources** into a single "primary pick + backup pick + skip list" per skill, same format as the TTS research output. Cite each source in the skill's `SKILL.md`.

## 4. Skills — specifications

### 4.1 `audio-edit`
- **Default backing packages (pending research):** `soundfile` + `librosa` + `ffmpeg-python`. Likely additions: `demucs` for voice isolation, `pydub` for high-level cuts.
- **Trigger keywords:** "trim audio", "convert mp3 to wav", "normalize audio", "extract voice from", "splice audio files", "audio editing".
- **Required ops (v1):** trim by start/end seconds, normalize loudness (EBU R128), transcode between mp3/wav/flac/ogg, concat list of clips, sample-rate convert, mono/stereo convert.
- **Voice isolation (v2):** optional `demucs --two-stems=vocals` for ref-clip prep.

### 4.2 `video-edit`
- **Default backing packages (pending research):** `ffmpeg-python` for fundamentals, `moviepy` for higher-level cuts, `opencv-python` only when frame-level manipulation is needed.
- **Trigger keywords:** "trim video", "convert mp4", "extract frames", "thumbnail of video", "concat videos", "change video resolution".
- **Required ops (v1):** trim by start/end seconds, concat, transcode, change resolution, change fps, extract single frame at timestamp, extract all frames as PNG sequence, mux-with-audio, demux audio.

### 4.3 `image-edit`
- **Default backing packages (pending research):** `Pillow` for 95% of cases, `opencv-python` for color-space / numpy interop, `pyvips` only if huge images.
- **Trigger keywords:** "resize image", "crop", "convert png to jpg", "image format", "batch resize", "thumbnails".
- **Required ops (v1):** resize (preserve-aspect option), crop by rect, rotate, format-convert (png/jpg/webp/avif), batch-apply over glob, color-adjust (brightness/contrast/saturation), watermark / overlay.

### 4.4 `search-awesome-db`
- **Backing:** direct SQLite query against `data/github_awesome.db`. No external dependency.
- **Trigger keywords:** "search awesome db", "what packages did we find for X", "find a tool in our database for Y".
- **Required ops (v1):**
  - `search(query, topic_filter?, limit=10)` → ranked list of `{name, repo_url, summary, tags, video_source}`.
  - Match against `description`, `name`, `tags`. SQLite FTS5 or just `LIKE` for v1.
  - Output as markdown table for inline use.
- **Why this skill is foundational:** every future package-research task benefits from grounding in the user's own curated corpus. The deeplake + Gemini path returns what the wider internet thinks; this returns what the user has *already vetted*. Becomes a standard step in the research pipeline of every other skill.

## 5. Deliverables (when this PRD is eventually executed)

Order of execution after un-parking:

1. `search-awesome-db` skill (no research needed — direct SQL).
2. For each of `audio-edit`, `video-edit`, `image-edit`:
   - Run the three-source research (deeplakesearch multihop + Gemini deep research + `search-awesome-db`).
   - Write `~/.claude/skills/<skill>/SKILL.md` with: package picks, install commands, trigger keywords, 5-10 concrete recipe examples.
   - Write Python helper modules at `~/.claude/skills/<skill>/scripts/` that the SKILL.md shells to.
3. Use the new `audio-edit` skill to finish the deferred Dia LEO-voice-clone fix (the WSJ-Knutson reference clip that prompted this PRD).
4. Update `~/Documents/code/atelier/docs/ARCHITECTURE.md` with a "§ Helper skills (Atelier toolkit)" subsection cross-referencing each skill.

## 6. Open questions

- **Skill file location:** `~/.claude/skills/<name>/SKILL.md` is the canonical Claude skills path. Alternative: nest under `~/.claude/skills/atelier/<name>/` for visibility grouping.
- **Naming:** `audio-edit` vs `audio` vs `media-audio`? Going with `*-edit` for clarity unless user prefers shorter.
- **Bundle or split?** All four skills in one Atelier-bundle OR four separate. Going with **four separate** because they have different trigger keywords and can evolve independently.
- **Licensing:** all picked packages must be Apache-2.0 / MIT / BSD compatible. No GPL/AGPL.

## 7. Success criteria

- Future sessions stop reinventing media operations. When Claude needs to trim audio, it invokes `audio-edit`. When it needs to resize an image batch, it invokes `image-edit`.
- **First real test:** use `audio-edit` to grab + trim the WSJ Knutson reference clip for Dia's LEO voice. The PRD is "done" when the Dia voice-clone v2 deploy ships using a clip produced by the new skill.
- The four skills are listed in the active-skills index and load on demand without prompting.

## 8. Risks + mitigations

- **License creep** — some media tools (demucs ML weights, etc.) have non-commercial clauses. *Mitigation:* research step captures license; skip non-Apache-2.0/MIT/BSD-clean packages for v1.
- **macOS arm64 wheels missing** — some Python media packages still ship CUDA-only or x86-only. *Mitigation:* research explicitly asks "does this install cleanly via `uv pip install` on macOS arm64 in 2026?".
- **Drift between skill keywords + actual triggers** — Claude might not naturally reach for `audio-edit` when a user says "shorten this clip". *Mitigation:* include 10+ realistic trigger phrasings in SKILL.md; refine after first month of use.

## 9. Related work

- **TTS deep-research (2026-05-24)** — see Atelier's pkdb cross-reference + Gemini research that drove Dia 1.6B's selection. Same shape as the per-skill research here.
- **Video-gen deep-research** — `docs/research/2026-05-22-video-gen-and-hub-architecture.md`. Template for per-skill research reports.
- **Architecture spec** — `docs/ARCHITECTURE.md`. The Atelier hub spec. This PRD is "skills layer" to that "compute layer".
- **Global rule** — `~/.claude/rules/tools/ollama-apple-silicon.md`. Skill packaging follows the same "Tier 2 tool rule" pattern.

## 10. Out-of-scope (deferred but tracked)

- `transcript-edit` skill (Whisper / subtitle SRT manipulation) — wait until ASR sidecar lands.
- `3d-mesh` skill (.obj / .glb manipulation) — speculative, no current need.
- `pdf-edit` — kami already covers PDF *generation*; manipulation can come later.
- Voice-cloning ref-clip *recording* (mic input) — only relevant if Atelier ever becomes interactive; for now we pull existing audio.

---

## When to un-park this PRD

Trigger to execute:
- The third ad-hoc media-manipulation task in any session (we've already had five) — pull this PRD off the shelf and ship `search-awesome-db` + `audio-edit` first.
- OR the user explicitly says "let's do the Atelier toolkit now."

Owner: the user. Estimated effort: ~1 session per skill (research + implement + test).

---

## Addendum 2026-05-24 — `audio-analyze` skill (agent listens to audio)

Today we hit a recurring gap: when diagnosing Dia 1.6B's gibberish output, the agent could not "listen" to the audio. Gemini API natively accepts audio (`inline_data` with `mime_type: audio/wav`) — but the installed `gemini-mcp` server only exposes `watch_video`, which rejects audio-only files. Workaround used today: wrap WAV in an mp4 with a black-frame video track via ffmpeg, then call `watch_video`. Works, but it is exactly the kind of one-off shell incantation this PRD exists to eliminate.

**Add to scope:**

| Skill | Op | Backing |
|---|---|---|
| `audio-edit` | (existing scope) trim, normalize, format-convert, voice-isolate | soundfile/ffmpeg |
| `audio-analyze` | **NEW** — agent semantic analysis of audio content | Google Gemini API direct (audio inline_data), with fallback to mp4-wrap + watch_video |

**Operations:**
- `transcribe(file)` → text — wraps whisper.cpp / whisper-mlx locally (we already have whisper.cpp on the Linux box, mlx-whisper planned for Mac).
- `analyze(file, prompt)` → text — sends the audio + a free-text prompt directly to Gemini, gets back a semantic response. (e.g., "describe this audio", "is this speech or noise?", "does this match the following script: ...".)
- `wrap_as_video(audio_file) → mp4` — utility for any tool that still needs video format.

**Implementation note:** the direct-Gemini-audio path is a 30-line Python script using `google-generativeai`. Should we build this as part of `atelier/` (since it bypasses the MCP layer) or as a standalone Claude skill at `~/.claude/skills/audio-analyze/`? Probably the latter — agents should reach for it without needing Atelier installed.
