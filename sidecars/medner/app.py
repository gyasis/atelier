"""medner — atelier sidecar: medical NER over HTTP (Apple-Silicon / MPS).

GOVERNED via the shared lifecycle framework (sidecars/_common/lifecycle.py) — it inherits the
3-law constitution automatically: no memory leaks (MPS self-restart reclaim), never unload while
a /ner call is running, and a request queue. This file holds ONLY medner's providers + endpoints;
all lifecycle, memory, busy-guard, queue, /healthz, /readyz, /admin/unload come from GovernedSidecar.

Providers (bundled into ONE SimpleNamespace by `_load()`, so a single unload frees all three;
each is loaded in its own try/except so one failing provider never blocks the others — the same
isolation guarantee the pre-governance module docstring promised):
  - gliner   : GLiNER-biomed, zero-shot typed NER (you pass the entity labels)
  - d4data   : d4data/biomedical-ner-all, granular HF token-classifier
  - scispacy : en_ner_bc5cdr_md (disease + chemical) spaCy NER
  - hf       : SCAFFOLD — stateless passthrough to the HF Inference API (off unless HF_TOKEN set;
               NOT part of the governed bundle — no local model, nothing to load/unload)

POST /ner      { "text": "...", "labels": [...]?, "providers": [...]? }
           ->  { "device": "mps", "results": { "<provider>": [ {tag,type,score?}, ... ] } }
GET  /health   -> per-provider load status (forces the bundle to load, same as pre-governance)
GET  /agent    -> self-describing manifest (atelier convention)
GET  /healthz /readyz · POST /admin/unload   (from the framework)

Env: MEDNER_PORT (8131) · MEDNER_DEVICE · IDLE_UNLOAD_SECONDS (600) · KEEP_WARM · HUB_TOKEN ·
     RECLAIM_THRESHOLD_GB · MEDNER_MAX_CONCURRENCY · HF_TOKEN / MEDNER_HF_MODEL (hf scaffold)
"""
import os
import traceback
from types import SimpleNamespace

from fastapi import FastAPI
from pydantic import BaseModel

from lifecycle import GovernedSidecar

PORT = int(os.environ.get("MEDNER_PORT", "8131"))

DEFAULT_LABELS = [
    "disease or disorder", "sign", "symptom", "medication or drug",
    "gene or genetic marker", "lab test or procedure", "anatomy",
    "microorganism", "clinical finding",
]


# ---------- per-provider cold-load (called once inside _load(), isolated by try/except) ----------
def _load_gliner():
    from gliner import GLiNER
    last = None
    # standard-architecture biomed models first; probe INFERENCE (some load but fail
    # forward() on this transformers, e.g. the bi-encoder's `token_lengths` kwarg).
    for repo in ["urchade/gliner_large_bio-v0.1", "urchade/gliner_medium-v2.1",
                 "Ihor/gliner-biomed-bi-large-v1.0"]:
        try:
            m = GLiNER.from_pretrained(repo)
            try:
                m = m.to(sc.device)
            except Exception:
                pass
            m.predict_entities("aspirin treats headache", ["medication", "symptom"], threshold=0.3)
            return m, repo
        except Exception as e:
            last = e
    raise RuntimeError(f"no working GLiNER model: {last}")


def _load_d4data():
    from transformers import AutoTokenizer, AutoModelForTokenClassification, pipeline
    name = "d4data/biomedical-ner-all"
    tok = AutoTokenizer.from_pretrained(name)
    mdl = AutoModelForTokenClassification.from_pretrained(name)
    # aggregation_strategy="first" merges subwords by the first token's label, which
    # keeps whole words ("dystonia") instead of fragments ("dyst"+"##onia").
    try:
        return pipeline("ner", model=mdl, tokenizer=tok, aggregation_strategy="first", device=sc.device)
    except Exception:
        return pipeline("ner", model=mdl, tokenizer=tok, aggregation_strategy="first")


def _load_scispacy():
    import spacy
    return spacy.load("en_ner_bc5cdr_md")


def _load():
    """Cold-load all three local providers into ONE namespace — dropping this single handle on
    unload (gc + empty_cache, both done by the framework) frees everything. Each provider loads
    in its own try/except so one failing model never blocks the others."""
    ns = SimpleNamespace(gliner=None, gliner_repo=None, gliner_error=None,
                         d4data=None, d4data_error=None,
                         scispacy=None, scispacy_error=None)
    try:
        ns.gliner, ns.gliner_repo = _load_gliner()
    except Exception as e:
        ns.gliner_error = str(e)
        print(f"[medner] gliner load failed: {e}", flush=True)
    try:
        ns.d4data = _load_d4data()
    except Exception as e:
        ns.d4data_error = str(e)
        print(f"[medner] d4data load failed: {e}", flush=True)
    try:
        ns.scispacy = _load_scispacy()
    except Exception as e:
        ns.scispacy_error = str(e)
        print(f"[medner] scispacy load failed: {e}", flush=True)
    return ns


sc = GovernedSidecar(
    "medner",
    role="medical NER (GLiNER-biomed + d4data + scispaCy, MPS)",
    load_fn=_load,
    model_name="gliner-biomed+d4data+scispacy",
    idle_unload_s=int(os.environ.get("IDLE_UNLOAD_SECONDS", "600")),
    # single-flight on unified memory; framework reads MEDNER_MAX_CONCURRENCY to override
)

app = FastAPI(title="medner sidecar", lifespan=sc.lifespan())
sc.attach(app)   # GET /healthz /readyz + POST /admin/unload


# ---------- run one provider against the loaded namespace ----------
def run_gliner(h, text, labels):
    if h.gliner is None:
        raise RuntimeError(h.gliner_error or "gliner unavailable")
    out, seen = [], {}
    for e in h.gliner.predict_entities(text, labels or DEFAULT_LABELS, threshold=0.45):
        k = e["text"].lower().strip()
        if k and (k not in seen or e["score"] > seen[k]):
            seen[k] = e["score"]
            out.append({"tag": e["text"].strip(), "type": e["label"], "score": round(float(e["score"]), 2)})
    return out


def run_d4data(h, text):
    if h.d4data is None:
        raise RuntimeError(h.d4data_error or "d4data unavailable")
    out, seen = [], set()
    for e in h.d4data(text):
        w = e["word"].replace("##", "").strip()
        k = w.lower()
        if len(k) < 3 or "##" in e["word"] or k in seen:  # drop subword fragments
            continue
        seen.add(k)
        out.append({"tag": w, "type": e["entity_group"], "score": round(float(e["score"]), 2)})
    return out


def run_scispacy(h, text):
    if h.scispacy is None:
        raise RuntimeError(h.scispacy_error or "scispacy unavailable")
    out, seen = [], set()
    for ent in h.scispacy(text).ents:
        k = ent.text.lower().strip()
        if k and k not in seen:
            seen.add(k)
            out.append({"tag": ent.text.strip(), "type": ent.label_})
    return out


def run_hf(text, labels):
    """SCAFFOLD: stateless passthrough to the HF Inference API (token-gated). No local model —
    lives outside the governed bundle, nothing here for the framework to load/unload."""
    token = os.environ.get("HF_TOKEN")
    if not token:
        return {"error": "HF passthrough not configured (set HF_TOKEN + MEDNER_HF_MODEL)"}
    import requests
    model = os.environ.get("MEDNER_HF_MODEL", "d4data/biomedical-ner-all")
    r = requests.post(f"https://api-inference.huggingface.co/models/{model}",
                      headers={"Authorization": f"Bearer {token}"}, json={"inputs": text}, timeout=60)
    r.raise_for_status()
    return [{"tag": e.get("word", ""), "type": e.get("entity_group", "?"),
             "score": round(float(e.get("score", 0)), 2)} for e in r.json()]


RUNNERS = {
    "gliner": lambda h, t, l: run_gliner(h, t, l),
    "d4data": lambda h, t, l: run_d4data(h, t),
    "scispacy": lambda h, t, l: run_scispacy(h, t),
    "hf": lambda h, t, l: run_hf(t, l),
}


class NerReq(BaseModel):
    text: str
    labels: list[str] | None = None
    providers: list[str] | None = None


@app.get("/agent")
def agent():
    """Self-describing manifest (atelier convention — the governor inlines these
    via GET :8799/agent?expand=true)."""
    return {
        "name": "medner",
        "role": "Medical NER — disease/sign/symptom/medication/gene entity extraction from clinical text",
        "port": PORT,
        "device": sc.device,
        "providers": {
            "gliner": "GLiNER zero-shot typed NER (urchade/gliner_large_bio) — you pass the labels",
            "d4data": "d4data/biomedical-ner-all — granular HF token-classifier (~100 types)",
            "scispacy": "scispaCy en_ner_bc5cdr_md — disease + chemical",
            "hf": "SCAFFOLD — HF Inference API passthrough (set HF_TOKEN + MEDNER_HF_MODEL)",
        },
        "default_labels": DEFAULT_LABELS,
        "endpoints": {
            "POST /ner": "{text, labels?, providers?} -> {device, results:{provider:[{tag,type,score?}]}}",
            "GET /readyz": "governed-sidecar liveness/observability (busy, queue_depth, rss_gb, ...)",
            "GET /health": "per-provider load status",
        },
    }


@app.get("/health")
async def health():
    """Per-provider load status. Forces the governed bundle to load (same behavior as before
    governance, when /health force-loaded each provider directly)."""
    async with sc.job() as h:
        status = {
            "gliner": (f"loaded ({h.gliner_repo})" if h.gliner is not None
                       else f"unavailable: {h.gliner_error}"),
            "d4data": "loaded" if h.d4data is not None else f"unavailable: {h.d4data_error}",
            "scispacy": "loaded" if h.scispacy is not None else f"unavailable: {h.scispacy_error}",
        }
    status["hf"] = "ready" if os.environ.get("HF_TOKEN") else "scaffold (no HF_TOKEN)"
    return {"ok": True, "device": sc.device, "providers": status}


@app.post("/ner")
async def ner(req: NerReq):
    want = req.providers or ["gliner", "d4data", "scispacy"]

    def _work(h):
        results = {}
        for p in want:
            fn = RUNNERS.get(p)
            if not fn:
                results[p] = {"error": "unknown provider"}
                continue
            try:
                results[p] = fn(h, req.text, req.labels)
            except Exception as e:
                results[p] = {"error": str(e)[:200], "trace": traceback.format_exc()[-400:]}
        return results

    async with sc.job() as h:              # queue slot + busy guard + lazy load (all 3 providers)
        results = await sc.run(_work, h)   # blocking model calls off the event loop
    return {"device": sc.device, "results": results}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
