"""Atelier generation-time predictor — modular, feature-rich, learns per model.

Records every generation run with its full feature context into a persistent
SQLite store, then predicts ETA for a new call by blending a per-model-class
PRIOR with the EMPIRICAL history (Bayesian shrinkage: prior dominates when there
are few samples, data takes over as runs accumulate). Known fixed costs
(network RTT for cloud/Claude, cold-load for an unloaded model) are added on top.

Feature context captured per run (extensible — add columns + priors):
  kind        tts | llm
  model       voice/engine (tts) or model name (llm)
  location    local | cloud      (cloud = Claude / remote API)
  host        which box ran it   (mac-studio, linux, claude-api)
  device      mps | cuda | cpu
  state       warm | cold        (cold adds load time)
  net_latency_ms, queue_depth
  in_units    chars (tts) or prompt tokens (llm)
  out_units   audio-seconds (tts) or output tokens (llm)
  seconds     measured wall time
  rate        seconds/char (tts) or decode tokens/sec (llm)

predict() filters history by model, blends with the class prior, and returns an
ETA + a p90 upper band so callers can say "~50s (up to ~80s)".
"""
import sqlite3
import statistics
import time
from pathlib import Path

DB = Path.home() / ".atelier" / "predictor.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(
  ts REAL, kind TEXT, model TEXT, location TEXT, host TEXT, device TEXT,
  state TEXT, net_latency_ms REAL, queue_depth INTEGER,
  in_units INTEGER, out_units INTEGER, seconds REAL, rate REAL
);
CREATE INDEX IF NOT EXISTS idx_runs_model ON runs(kind, model);
"""

# Per-class PRIORS (Bayesian): used until empirical data for a model accumulates.
# rate = decode tok/s (llm) or sec/char (tts). out = typical output units. k = prior strength.
PRIORS = {
    "llm:thinking": {"rate": 12.0, "out": 2000, "k": 4},              # r1/qwq long reasoning
    "llm:large":    {"rate": 8.0,  "out": 400,  "k": 4},              # 27B-70B
    "llm:standard": {"rate": 20.0, "out": 350,  "k": 4},
    "llm:claude":   {"rate": 60.0, "out": 600,  "k": 3, "net_ms": 400},  # cloud: RTT-dominated
    "tts":          {"rate": 0.13, "out": None, "k": 3},              # sec/char
}
_THINKING = ("r1", "qwq", "reason", "thinking")
_LARGE = ("70b", "34b", "32b", "27b", "coder-next", "devstral")


def classify(kind: str, model: str | None, location: str) -> str:
    if kind == "tts":
        return "tts"
    if location == "cloud" or (model or "").lower().startswith("claude"):
        return "llm:claude"
    m = (model or "").lower()
    if any(t in m for t in _THINKING):
        return "llm:thinking"
    if any(t in m for t in _LARGE):
        return "llm:large"
    return "llm:standard"


def _db():
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(DB))
    c.executescript(_SCHEMA)
    return c


def record(*, kind, model, seconds, in_units=None, out_units=None, rate=None,
           location="local", host="mac-studio", device="mps", state="warm",
           net_latency_ms=0.0, queue_depth=0):
    """Persist one completed run. Computes rate if not given."""
    if rate is None and seconds:
        if kind == "tts" and in_units:
            rate = seconds / in_units                  # sec/char
        elif kind != "tts" and out_units:
            rate = out_units / seconds                 # decode tok/s
    c = _db()
    c.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (time.time(), kind, model, location, host, device, state,
               net_latency_ms, queue_depth, in_units, out_units, seconds, rate))
    c.commit()
    c.close()


def _col(c, kind, model, col):
    return [r[0] for r in c.execute(
        f"SELECT {col} FROM runs WHERE kind=? AND model=? AND {col} IS NOT NULL "
        f"ORDER BY ts DESC LIMIT 200", (kind, model)).fetchall() if r[0] is not None]


def _shrink(empirical, n, prior_val, k):
    """Bayesian shrinkage: weighted blend of empirical median and prior by sample count."""
    if not empirical:
        return prior_val, "prior-only"
    med = statistics.median(empirical)
    return (n * med + k * prior_val) / (n + k), "empirical+prior"


def predict(*, kind, model, in_units=0, out_units=None,
            location="local", state="warm", net_latency_ms=None, cold_load_s=0.0):
    """Predict ETA (+ p90 band) for a call. Blends per-model empirical history with
    the class prior; adds network + cold-load fixed costs."""
    cls = classify(kind, model, location)
    prior = PRIORS.get(cls, PRIORS["llm:standard"])
    k = prior["k"]
    c = _db()
    rate_samples = _col(c, kind, model, "rate")
    n = len(rate_samples)
    rate, basis = _shrink(rate_samples, n, prior["rate"], k)

    if kind == "tts":
        core = in_units * rate
        core_p90 = core * 1.3
        pred_out = None
    else:
        if out_units is None:
            out_samples = _col(c, kind, model, "out_units")
            pred_out, _ = _shrink(out_samples, len(out_samples), prior["out"], k)
            pred_out = round(pred_out)
            out_p90 = (round(statistics.quantiles(out_samples, n=10)[8])
                       if len(out_samples) >= 10 else round(pred_out * 1.6))
        else:
            pred_out = out_units
            out_p90 = out_units
        core = pred_out / rate if rate else None
        core_p90 = out_p90 / rate if rate else None
    c.close()

    net = (net_latency_ms if net_latency_ms is not None else prior.get("net_ms", 0)) / 1000.0
    cold = cold_load_s if state == "cold" else 0.0
    eta = round((core or 0) + net + cold, 1)
    eta_p90 = round((core_p90 or core or 0) + net + cold, 1)
    return {
        "model": model, "class": cls, "location": location, "state": state,
        "eta_seconds": eta, "eta_p90_seconds": eta_p90,
        "human": _human(eta, eta_p90),
        "rate": round(rate, 4) if rate else None,
        "predicted_output_units": pred_out,
        "samples": n, "basis": basis,
        "fixed_costs": {"net_s": round(net, 3), "cold_load_s": round(cold, 1)},
    }


def _human(eta, p90):
    def fmt(s):
        return f"{s:.0f}s" if s < 90 else f"{s/60:.1f} min"
    return f"~{fmt(eta)} (up to ~{fmt(p90)})"


def stats():
    """Summary of accumulated runs per (kind, model) — what the predictor has learned."""
    c = _db()
    rows = c.execute(
        "SELECT kind, model, COUNT(*), ROUND(AVG(rate),4), ROUND(AVG(seconds),1) "
        "FROM runs GROUP BY kind, model ORDER BY COUNT(*) DESC").fetchall()
    c.close()
    return [{"kind": r[0], "model": r[1], "runs": r[2], "avg_rate": r[3], "avg_seconds": r[4]}
            for r in rows]
