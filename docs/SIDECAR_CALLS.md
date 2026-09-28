# How to call every Atelier sidecar (curl cookbook)

The canonical, always-current source is each sidecar's own `GET /agent` manifest (and the
hub-wide `GET http://127.0.0.1:8799/agent?expand=true` which inlines them all). This doc is a
copy-paste quick-reference distilled from those manifests. Ports are loopback; set
`URL=http://127.0.0.1:<port>`.

| Sidecar | Port | Role | Verified |
|---|---|---|---|
| kokoro | 8765 | TTS — fast, fixed voices | ✓ live |
| whisper | 8766 | ASR — speech-to-text (+ LLM structure/summarize) | ✓ live |
| dia | 8769 | TTS — expressive multi-speaker dialogue + cloning | ✓ live |
| omnivoice | 8770 | TTS — primary, instruct-driven accent/tone + cloning | ✓ live |
| llamacpp | 8771 | LLM — llama.cpp (GGUF, Metal), OpenAI-compatible | ✓ live |
| mlxlm | 8773 | LLM — Apple mlx_lm (MLX), OpenAI-compatible | ✓ live |
| colibri | 8783 | LLM — Colibri huge-MoE streamed from SSD; OVERNIGHT jobs + brio | ✓ live (tests: tiny fixture) |
| fastmlx | 8772 | LLM/VLM — MLX-native | ✗ blocked (upstream) |
| medner | 8131 | NER — medical entity extraction (GLiNER + d4data + scispaCy) | ✓ live (on-demand) |
| radiogen | 8774 | Imaging — synthetic chest X-ray (diffusers SD) → DICOM | ✓ live |
| maisi | 8775 | Imaging — synthetic 3D CT (MONAI MAISI on Modal A100) | ✓ live (cloud) |

Every sidecar also has: `GET /readyz` (warm/cold/busy + queue depth), `GET /agent`
(self-describing manifest), `POST /admin/unload` (free its memory now).

> **Memory note:** these all share one unified-memory pool. For concurrent/batched calls,
> go through the governor's admission gate first (`POST :8799/admit` → run → `POST :8799/release`,
> or `from atelier_admit import admission`) so jobs queue instead of clearing each other.
> See [`LLM_ADMISSION_QUEUE.md`](./LLM_ADMISSION_QUEUE.md).

---

## TTS

### kokoro (8765) — fast, fixed voices
```bash
curl -s http://127.0.0.1:8765/tts -H 'content-type: application/json' \
  -d '{"text":"Hello there","voice":"af_bella","speed":1.0,"lang":"en-us"}' --output out.wav
curl -s http://127.0.0.1:8765/voices          # list voice ids + default (am_michael)
```
Params: `text` (1–5000), `voice`, `speed` (0.5–2.0), `lang`. Returns `audio/wav` (PCM16);
`x-synth-seconds` header.

### omnivoice (8770) — primary; instruct-driven style + zero-shot cloning
```bash
# Instruct style:
curl -s http://127.0.0.1:8770/tts -H 'content-type: application/json' \
  -d '{"text":"Welcome","instruct":"British accent, bright feminine tone","speed":1.0}' --output out.wav
# Zero-shot clone from a 3–10s reference clip:
curl -s http://127.0.0.1:8770/tts -H 'content-type: application/json' \
  -d '{"text":"Cloned line","ref_audio":"/abs/ref.wav","ref_text":"transcript of ref"}' --output out.wav
```
Params: `text`, `language`, `instruct`, `ref_audio`+`ref_text` (clone), `speed`,
`num_step` (8–128 diffusion), `guidance_scale` (1–5), `class_temperature` (0–1.5),
`pitch_semitones` (-12..+12). Returns `audio/wav` (24kHz).

### dia (8769) — expressive multi-speaker dialogue (batch)
```bash
curl -s http://127.0.0.1:8769/tts -H 'content-type: application/json' \
  -d '{"text":"[S1] Welcome back. [S2] Glad to be here.","use_voice_clone":true}' --output out.wav

# slow, deliberate read with a measured tone:
curl -s http://127.0.0.1:8769/tts -H 'content-type: application/json' \
  -d '{"text":"[S1] Breathe. You are exactly where you need to be.","speed":0.85,"emotion":"measured"}' --output out.wav
```
Params: `text` (use `[S1]`/`[S2]` speaker tags), `use_voice_clone`,
**`speed`** (0.5–1.5, pitch-preserved pace — <1.0 = slower/enunciated, via ffmpeg `atempo`),
**`emotion`** (`neutral|calm|measured|warm|expressive` — overrides Dia's `temperature`+`guidance_scale`),
`max_new_tokens` (128–4096), `guidance_scale` (1–10), `temperature` (0.5–2.5), `top_p`, `top_k`.
Returns `audio/wav` (44.1kHz).

---

## ASR

### whisper (8766) — speech-to-text (+ optional LLM structure/summarize)
Supply **exactly one** source: `file` (multipart upload), `url` (sidecar downloads), or
`path` (absolute path already on this host).
```bash
# Inline transcription of a local file, with a 0.7-strength summary:
curl -s http://127.0.0.1:8766/transcribe -F path=/abs/a.wav -F model=turbo -F summarize=0.7

# Long audio → async job, then stream it:
JOB=$(curl -s http://127.0.0.1:8766/transcribe/batch \
  -d '{"path":"/abs/show.wav","summarize":0.5}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["job_id"])')
curl -s http://127.0.0.1:8766/jobs/$JOB/stream       # SSE: status → heartbeat → result

# Reformat / summarize ANY text you already have:
curl -s http://127.0.0.1:8766/structure -H 'content-type: application/json' -d '{"text":"...","hint":"interview"}'
curl -s http://127.0.0.1:8766/summarize -H 'content-type: application/json' -d '{"text":"...","weight":0.6}'
```
Key `/transcribe` params: `model` (turbo|large|accurate|HF repo), `language`, `initial_prompt`,
`word_timestamps`, `response_format` (json|text|srt|vtt|verbose_json), `save`, `normalize`,
`gain_db`, `structure`, `summarize` (0–1), `llm_model`. Jobs: `GET /jobs/{id}`,
`GET /jobs/{id}/stream` (SSE), `GET /jobs/{id}/result`, `DELETE /jobs/{id}`.
The `structure`/`summarize` options call Ollama under the hood (now gated via the admission queue).

---

## LLM (OpenAI-compatible)

Both speak the OpenAI schema at `…/v1`, support `stream=true`, and lazy-load on first call.

### llamacpp (8771) — GGUF / llama.cpp · model alias `gemma4-12b`
```bash
curl -s http://127.0.0.1:8771/v1/chat/completions -H 'content-type: application/json' \
  -d '{"model":"gemma4-12b","messages":[{"role":"user","content":"hi"}]}'
curl -s http://127.0.0.1:8771/v1/models
```

### mlxlm (8773) — MLX / Apple mlx_lm · model alias `qwen2.5-0.5b`
```bash
curl -s http://127.0.0.1:8773/v1/chat/completions -H 'content-type: application/json' \
  -d '{"model":"qwen2.5-0.5b","messages":[{"role":"user","content":"hi"}]}'
```
Also: `POST /v1/completions`, `GET /v1/models`, `POST /admin/unload`.

---

### colibri (8783) — huge MoE from SSD, OVERNIGHT · model alias `colibri-qwen36`

Slow by design (fractions of a token/s up to a few). Submit and collect later; never set a
read timeout. Full runbook: `sidecars/colibri/README.md`.

```bash
# overnight job (persisted, queued, governor lease renewed while it runs)
curl -s http://<mac-host>:8783/jobs -H 'content-type: application/json' \
  -d '{"path":"v1/chat/completions","body":{"messages":[{"role":"user","content":"hi"}],"max_tokens":256}}'
curl -s http://<mac-host>:8783/jobs/<job_id>
# closed-set scoring (no generation, returns entropy)
curl -s http://<mac-host>:8783/jobs -H 'content-type: application/json' \
  -d '{"path":"v1/brio","body":{"state":"...","question":"Ship?","options":["yes","no"]}}'
# short sync call through the governor
curl -s http://<mac-host>:8799/llm/colibri/v1/chat/completions -H 'content-type: application/json' \
  -d '{"model":"colibri-qwen36","messages":[{"role":"user","content":"hi"}],"max_tokens":32}'
```

## Imaging generation

### radiogen (8774) — synthetic chest X-ray → DICOM
```bash
curl -s $URL/generate -H 'content-type: application/json' \
  -d '{"prompt":"frontal chest X-ray, right lower lobe pneumonia"}' | jq '.uids'
```
diffusers Stable Diffusion (RoentGen-v2, gated — set `RADIOGEN_MODEL` + an HF token). Returns PNG +
DICOM (base64, Modality DX) bound to a FHIR ImagingStudy's UIDs.

### maisi (8775) — synthetic 3D CT (cloud passthrough)
```bash
curl -s $URL/generate -H 'content-type: application/json' \
  -d '{"prompt":"abdomen CT, normal anatomy","body_region":"abdomen"}' | jq
```
Thin local proxy → Modal A100 app `atelier-maisi` ($0 idle, scales to zero). `/readyz` reports
`backend=modal`, `device="A100 (modal cloud)"`, `passthrough=true`.

---

## NER

### medner (8131) — medical entity extraction (GLiNER + d4data + scispaCy)
```bash
# all three providers, default label set:
curl -s $URL/ner -H 'content-type: application/json' \
  -d '{"text":"A man on haloperidol develops oculogyric crisis and tongue protrusion."}' | jq

# pick a provider + your own GLiNER labels:
curl -s $URL/ner -H 'content-type: application/json' \
  -d '{"text":"...","providers":["gliner"],"labels":["disease or disorder","medication or drug","sign"]}' | jq
```
Returns `{device, results:{provider:[{tag,type,score?}]}}`. Providers: `gliner` (zero-shot typed),
`d4data` (granular), `scispacy` (disease/chemical), `hf` (scaffold). On-demand — start with
`sidecars/medner/run.sh`. For LLM-based NER use Ollama (`gemma4:12b`, `medgemma-27b-it`).

---

## Ollama (the multi-model LLM tenant, not a sidecar) · 11434
```bash
curl -s http://127.0.0.1:11434/api/chat -d '{"model":"qwen3:32b","stream":false,"messages":[{"role":"user","content":"hi"}]}'
curl -s http://127.0.0.1:11434/api/tags     # installed models
curl -s http://127.0.0.1:11434/api/ps       # currently loaded models
```

---

## Health-check them all at once
```bash
for p in 8765 8766 8769 8770 8771 8772 8773; do
  printf "%s " "$p"; curl -s --max-time 3 http://127.0.0.1:$p/readyz \
    | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d.get("lifecycle"),d.get("model","-"))' 2>/dev/null \
    || echo "UNREACHABLE"
done
```
