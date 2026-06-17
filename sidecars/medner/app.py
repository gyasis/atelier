"""medner — atelier sidecar: medical NER over HTTP (Apple-Silicon / MPS).

Providers (lazy-loaded, each isolated so one failing never breaks the others):
  - gliner   : GLiNER-biomed, zero-shot typed NER (you pass the entity labels)
  - d4data   : d4data/biomedical-ner-all, granular HF token-classifier
  - scispacy : en_ner_bc5cdr_md (disease + chemical) spaCy NER
  - hf       : SCAFFOLD — passthrough to the HF Inference API (off unless HF_TOKEN set)

POST /ner  { "text": "...", "labels": [...]?, "providers": [...]? }
        ->  { "device": "mps", "results": { "<provider>": [ {tag,type,score?}, ... ] } }
GET  /health -> which providers are importable + loaded
"""
import os, traceback
from functools import lru_cache
from fastapi import FastAPI
from pydantic import BaseModel

DEFAULT_LABELS = [
    "disease or disorder", "sign", "symptom", "medication or drug",
    "gene or genetic marker", "lab test or procedure", "anatomy",
    "microorganism", "clinical finding",
]

def _device():
    try:
        import torch
        if torch.backends.mps.is_available():
            return "mps"
    except Exception:
        pass
    return "cpu"

DEVICE = _device()
app = FastAPI(title="medner sidecar")


# ---------- providers (cached singletons) ----------
@lru_cache(maxsize=1)
def _gliner():
    from gliner import GLiNER
    last = None
    # standard-architecture biomed models first; probe INFERENCE (some load but fail
    # forward() on this transformers, e.g. the bi-encoder's `token_lengths` kwarg).
    for repo in ["urchade/gliner_large_bio-v0.1", "urchade/gliner_medium-v2.1",
                 "Ihor/gliner-biomed-bi-large-v1.0"]:
        try:
            m = GLiNER.from_pretrained(repo)
            try: m = m.to(DEVICE)
            except Exception: pass
            m.predict_entities("aspirin treats headache", ["medication", "symptom"], threshold=0.3)
            return (m, repo)
        except Exception as e:
            last = e
    raise RuntimeError(f"no working GLiNER model: {last}")

@lru_cache(maxsize=1)
def _d4data():
    from transformers import AutoTokenizer, AutoModelForTokenClassification, pipeline
    name = "d4data/biomedical-ner-all"
    tok = AutoTokenizer.from_pretrained(name)
    mdl = AutoModelForTokenClassification.from_pretrained(name)
    # aggregation_strategy="first" merges subwords by the first token's label, which
    # keeps whole words ("dystonia") instead of fragments ("dyst"+"##onia").
    try:
        return pipeline("ner", model=mdl, tokenizer=tok, aggregation_strategy="first", device=DEVICE)
    except Exception:
        return pipeline("ner", model=mdl, tokenizer=tok, aggregation_strategy="first")

@lru_cache(maxsize=1)
def _scispacy():
    import spacy
    return spacy.load("en_ner_bc5cdr_md")


# ---------- run one provider ----------
def run_gliner(text, labels):
    m, repo = _gliner()
    out, seen = [], {}
    for e in m.predict_entities(text, labels or DEFAULT_LABELS, threshold=0.45):
        k = e["text"].lower().strip()
        if k and (k not in seen or e["score"] > seen[k]):
            seen[k] = e["score"]
            out.append({"tag": e["text"].strip(), "type": e["label"], "score": round(float(e["score"]), 2)})
    return out

def run_d4data(text):
    pipe = _d4data()
    out, seen = [], set()
    for e in pipe(text):
        w = e["word"].replace("##", "").strip()
        k = w.lower()
        if len(k) < 3 or "##" in e["word"] or k in seen:  # drop subword fragments
            continue
        seen.add(k)
        out.append({"tag": w, "type": e["entity_group"], "score": round(float(e["score"]), 2)})
    return out

def run_scispacy(text):
    nlp = _scispacy()
    out, seen = [], set()
    for ent in nlp(text).ents:
        k = ent.text.lower().strip()
        if k and k not in seen:
            seen.add(k)
            out.append({"tag": ent.text.strip(), "type": ent.label_})
    return out

def run_hf(text, labels):
    """SCAFFOLD: passthrough to HF Inference API (token-gated). Wire a real model id later."""
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
    "gliner": lambda t, l: run_gliner(t, l),
    "d4data": lambda t, l: run_d4data(t),
    "scispacy": lambda t, l: run_scispacy(t),
    "hf": lambda t, l: run_hf(t, l),
}


class NerReq(BaseModel):
    text: str
    labels: list[str] | None = None
    providers: list[str] | None = None


@app.get("/readyz")
def readyz():
    """Liveness for the atelier governor (polls {base}/readyz). Fast — does NOT
    force model load (models lazy-load on first /ner). 200 = server is up."""
    return {"ok": True, "device": DEVICE}


@app.get("/agent")
def agent():
    """Self-describing manifest (atelier convention — the governor inlines these
    via GET :8799/agent?expand=true)."""
    return {
        "name": "medner",
        "role": "Medical NER — disease/sign/symptom/medication/gene entity extraction from clinical text",
        "port": int(os.environ.get("MEDNER_PORT", 8131)),
        "device": DEVICE,
        "providers": {
            "gliner": "GLiNER zero-shot typed NER (urchade/gliner_large_bio) — you pass the labels",
            "d4data": "d4data/biomedical-ner-all — granular HF token-classifier (~100 types)",
            "scispacy": "scispaCy en_ner_bc5cdr_md — disease + chemical",
            "hf": "SCAFFOLD — HF Inference API passthrough (set HF_TOKEN + MEDNER_HF_MODEL)",
        },
        "default_labels": DEFAULT_LABELS,
        "endpoints": {
            "POST /ner": "{text, labels?, providers?} -> {device, results:{provider:[{tag,type,score?}]}}",
            "GET /readyz": "liveness (fast, no model load)",
            "GET /health": "per-provider load status",
        },
    }


@app.get("/health")
def health():
    status = {}
    for name, loader in [("gliner", _gliner), ("d4data", _d4data), ("scispacy", _scispacy)]:
        try:
            loader(); status[name] = "loaded"
        except Exception as e:
            status[name] = f"unavailable: {str(e)[:120]}"
    status["hf"] = "ready" if os.environ.get("HF_TOKEN") else "scaffold (no HF_TOKEN)"
    return {"ok": True, "device": DEVICE, "providers": status}


@app.post("/ner")
def ner(req: NerReq):
    want = req.providers or ["gliner", "d4data", "scispacy"]
    results = {}
    for p in want:
        fn = RUNNERS.get(p)
        if not fn:
            results[p] = {"error": "unknown provider"}; continue
        try:
            results[p] = fn(req.text, req.labels)
        except Exception as e:
            results[p] = {"error": str(e)[:200], "trace": traceback.format_exc()[-400:]}
    return {"device": DEVICE, "results": results}
