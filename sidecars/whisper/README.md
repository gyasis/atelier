# whisper-sidecar — ASR (speech → text)

`whisper-large-v3-turbo` via [`mlx-whisper`](https://github.com/ml-explore/mlx-examples/tree/main/whisper) on Apple MLX. Port **8766**. Part of the Mac Studio inference hub — see `docs/ARCHITECTURE.md` §5.2.

On an M1 Max: ~40–50× realtime (≈12 min of audio in ~15s).

## Install / deploy

Handled by `deploy/install.sh` (creates the venv, installs `requirements.txt`, symlinks `server.py` into `~/services/whisper-sidecar/`, drops the launchd plist, bootstraps the service).

One host prerequisite the other sidecars don't need — **ffmpeg**, which mlx-whisper shells out to for decoding mp3/m4a/etc.:

```sh
brew install ffmpeg
```

The model (~1.6 GB) auto-downloads into `HF_HOME` on the first `/transcribe`.

## Model — chosen per request (not hardcoded)

Pass `model=` to decide which whisper Atelier loads. The sidecar keeps **one model resident** and **hot-swaps** when a request asks for a different one (memory courtesy — `large-v3` is ~3 GB vs turbo's ~1.6 GB).

| `model=` | resolves to | trade-off |
|---|---|---|
| `turbo` *(default)* | `mlx-community/whisper-large-v3-turbo` | ~45× realtime — fast |
| `large` / `accurate` | `mlx-community/whisper-large-v3` | ~3× slower, best accuracy |
| `<hf/repo>` | any mlx-community whisper repo | explicit |

```sh
curl -s :8766/transcribe -F path=/path/a.wav                 # default (turbo)
curl -s :8766/transcribe -F path=/path/a.wav -F model=large  # max accuracy
curl -s :8766/models                                         # aliases + what's loaded
```

The governor records ETAs **per model** (from the `[asr] model=…` log line), so `/estimate?engine=whisper&model=whisper-large-v3&audio_s=720` gives a real, learned ETA — that's the signal for deciding which model to load for a given job. Omitting `model` falls back to `$WHISPER_MODEL_REPO`.

## Input — three ways the audio can arrive

Pick **exactly one** of `file`, `url`, `path` per request.

```sh
# 1) UPLOAD the bytes (multipart)
curl -s :8766/transcribe -F file=@meeting.m4a

# 2) PULL from a link — the sidecar downloads it itself
curl -s :8766/transcribe -F url=https://example.com/episode.mp3

# 3) POINT at a file already on the Mac (no copy, no upload)
curl -s :8766/transcribe -F path=/Users/gyasisutton/outputs/podcast/ep12.wav
```

## Output — what comes back, and where it's saved

The transcript is **always returned inline**; `response_format` picks the shape, and `save=true` *also* writes it to disk.

```sh
# default: lean JSON {text, language, segments[]}
curl -s :8766/transcribe -F path=/path/a.wav

# plain text / subtitles
curl -s :8766/transcribe -F path=/path/a.wav -F response_format=text
curl -s :8766/transcribe -F path=/path/a.wav -F response_format=srt
curl -s :8766/transcribe -F path=/path/a.wav -F response_format=vtt
# verbose_json = full whisper segments (tokens, logprobs, …)

# ALSO save to disk. Default location: $WHISPER_OUTPUT_DIR/<sha256>.<ext>
curl -s :8766/transcribe -F path=/path/a.wav -F save=true -F response_format=srt
# …or an explicit destination:
curl -s :8766/transcribe -F path=/path/a.wav -F save=true -F output_path=/Users/gyasisutton/outputs/a.json
```

Every response carries **`x-content-sha256`** (the cache key for that audio — matches the gateway's transcript cache in `ARCHITECTURE.md` §4) and `x-saved-path` when saved.

Optional form fields: `language` (ISO code, else auto-detect), `initial_prompt` (bias spelling/terms), `word_timestamps=true`.

## Long audio — async batch

For hour-long inputs, submit a job (source is `url` or `path`) and stream progress:

```sh
JOB=$(curl -s :8766/transcribe/batch -H 'content-type: application/json' \
  -d '{"path":"/Users/gyasisutton/outputs/long-show.wav","response_format":"srt"}' | jq -r .job_id)

curl -N :8766/jobs/$JOB/stream      # SSE: status → heartbeat → result
curl -s  :8766/jobs/$JOB/result     # the transcript once done
curl -s  -X DELETE :8766/jobs/$JOB  # cancel (only before it starts running)
```

Batch jobs `save=true` by default, so the result survives a client disconnect.

> Per-segment streaming progress is a v2 item — mlx-whisper has no progress callback in the stable release, so the SSE stream emits coarse status + heartbeats, then the final result.

## Health & lifecycle

| Endpoint | Purpose |
|---|---|
| `GET /healthz` | liveness |
| `GET /readyz` | warm/cold/busy, `active_jobs`, `queue_depth`, `batch_jobs` |
| `POST /admin/unload[?force=true]` | drop the model from memory (governor courtesy) |

Like the other sidecars, the model **idle-unloads after 5 min** (`IDLE_UNLOAD_SECONDS`, override with `KEEP_WARM=true`) so a 1.6 GB model doesn't squat on unified memory between bursts. The governor (`:8799`) tails this sidecar's `[asr]` log lines, so transcribe calls show up in the hub's telemetry and feed the ETA predictor (`kind=asr`, `audio_s` → compute seconds).

## Env vars

| Var | Default |
|---|---|
| `WHISPER_MODEL_REPO` | `mlx-community/whisper-large-v3-turbo` |
| `WHISPER_OUTPUT_DIR` | `~/outputs/transcripts` |
| `WHISPER_MAX_PULL_MB` | `512` (cap on url-pulled audio) |
| `IDLE_UNLOAD_SECONDS` | `300` |
| `KEEP_WARM` | `false` |
| `HUB_TOKEN` | unset (set → work endpoints require `Authorization: Bearer …`) |
| `HF_HOME` | shared hub cache |
