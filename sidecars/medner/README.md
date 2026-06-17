# medner-sidecar — medical NER

Server-side medical **named-entity recognition** over HTTP. Port **8131**. Extracts clinical entities
— diseases, signs, symptoms, medications, genetic markers, anatomy, microorganisms — from free text
(question stems, answer options, explanations) so they can be used as drill-down study tags. Three
engines, picked per call; runs on Apple-Silicon **MPS**. Lazy-loads models on first `/ner`.

> **Lifecycle:** currently **on-demand** (`./run.sh`), not yet under launchd. Registered with the
> governor (`SIDECAR_BASE["medner"]`, `SIDECAR_ROLES`, `AGENT_CAPABLE`) so it shows in atelier; it
> reports `cold` until started. launchd autostart (`io.macstudio.hub.medner.plist`) is the next step.

## Use

```sh
URL=http://192.168.0.159:8131
# all three providers, default label set:
curl -s $URL/ner -H 'content-type: application/json' \
  -d '{"text":"A man on haloperidol develops oculogyric crisis and tongue protrusion."}' | jq

# one provider + your own GLiNER labels:
curl -s $URL/ner -H 'content-type: application/json' \
  -d '{"text":"...","providers":["gliner"],"labels":["disease or disorder","medication or drug","sign"]}' | jq
```

Response: `{ "device":"mps", "results": { "<provider>": [ {"tag","type","score"?}, … ] } }`
(each provider isolated — one failing returns `{"error":…}` in its slot, never breaks the others).

## Endpoints

| Endpoint | Purpose |
|---|---|
| `GET /readyz` | liveness (fast — does NOT force model load) |
| `GET /health` | per-provider load status (`loaded` / `unavailable: …`) |
| `GET /agent` | machine-readable manifest (governor inlines via `:8799/agent?expand=true`) |
| `POST /ner` | `{text, labels?, providers?}` → extracted entities per provider |

## Models & providers

| Provider | Model | Notes |
|---|---|---|
| `gliner` | `urchade/gliner_large_bio-v0.1` (falls back to `gliner_medium-v2.1`, `Ihor/gliner-biomed-bi-large`) | zero-shot **typed** NER — you pass the entity labels; cleanest typing. Loader probes inference + skips models incompatible with the pinned transformers. |
| `d4data` | `d4data/biomedical-ner-all` | granular HF token-classifier (~100 types); `aggregation_strategy="first"` + `##`-fragment filter. Highest coverage, noisier. |
| `scispacy` | `en_ner_bc5cdr_md` (v0.5.4) | spaCy disease + chemical NER. Needs **numpy<2** (ABI). |
| `hf` | *(scaffold)* | passthrough to the HF Inference API — off unless `HF_TOKEN` (+ `MEDNER_HF_MODEL`) set. For later. |

Default GLiNER labels: disease/disorder, sign, symptom, medication/drug, gene/genetic-marker,
lab-test/procedure, anatomy, microorganism, clinical-finding.

## Env (set by `run.sh`)

- `PYTORCH_ENABLE_MPS_FALLBACK=1`, `PYTORCH_MPS_HIGH_WATERMARK_RATIO=0.0` — Apple-Silicon MPS (see `~/.claude/rules/tools/ollama-apple-silicon.md`).
- `MEDNER_PORT` (default `8131`), `TOKENIZERS_PARALLELISM=false`, `HF_HUB_DISABLE_TELEMETRY=1`.
- `HF_TOKEN`, `MEDNER_HF_MODEL` — only for the `hf` passthrough provider.
- **Pinned:** `transformers<5` (the 5.x line breaks the token-classification pipeline), `numpy<2` (scispaCy/spaCy ABI).

## Pkg / run

```sh
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -r requirements.txt
uv pip install --python .venv/bin/python https://s3-us-west-2.amazonaws.com/ai2-s2-scispacy/releases/v0.5.4/en_ner_bc5cdr_md-0.5.4.tar.gz
./run.sh        # on-demand; caffeinate -i uvicorn on :8131
```

> Built for the SSM exam-tag NER bake-off (GLiNER vs d4data vs scispaCy vs Gemini/medgemma).
> For LLM-based NER use the Mac's **ollama** (`gemma4:12b`, `medgemma-27b-it`) at `:11434`.
