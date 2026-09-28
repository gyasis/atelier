#!/usr/bin/env python3
"""bench_colibri — speed + memory baseline for one Colibri model behind the sidecar (Mac-side).

    .venv/bin/python bench_colibri.py [--model colibri-qwen36] [--gen 128] [--runs 2]

Measures, from a COLD start:
  - load time (first call wall time minus the warm call's time for the same request)
  - per-run wall time, completion tokens, tok/s (wall-based: includes prefill)
  - Brio closed-set scoring wall time (no generation)
  - memory: sidecar subtree RSS (the governor's own measurement), governor resident/free,
    and vm_stat file-backed / active / inactive pages before and after — i.e. how much of the
    growth is the process versus page cache the governor also counts as "resident".

Holds no lease of its own: calls go through the governor front door (/llm/colibri) so the load
is admitted like any other. Writes ~/.atelier/colibri-test-results/bench-<ts>-<model>.json.
"""
import argparse
import json
import re
import subprocess
import time
from pathlib import Path

import httpx

SC, GOV = "http://127.0.0.1:8783", "http://127.0.0.1:8799"
TO = httpx.Timeout(connect=5.0, read=None, write=30.0, pool=None)
PROMPT = ("You are reviewing an on-call handover. The payments service returned HTTP 502 for "
          "eleven minutes after a config push; the rollback fixed it; no data was lost; two "
          "customers were double-charged and refunded manually. Write a short incident summary "
          "with a cause, an impact line, and two follow-up actions.")


def vm():
    out = subprocess.run(["vm_stat"], capture_output=True, text=True).stdout
    ps = int(re.search(r"page size of (\d+)", out).group(1))
    g = lambda k: int(re.search(rf"{re.escape(k)}:\s+(\d+)", out).group(1)) * ps / 1e9
    return {k: round(g(v), 2) for k, v in {
        "free": "Pages free", "active": "Pages active", "inactive": "Pages inactive",
        "speculative": "Pages speculative", "wired": "Pages wired down",
        "file_backed": "File-backed pages", "anonymous": "Anonymous pages",
        "compressed": "Pages occupied by compressor"}.items()}


def gov_mem():
    b = httpx.get(f"{GOV}/budget", timeout=10).json()
    a = httpx.get(f"{GOV}/agent?expand=true", timeout=20).json()
    c = a.get("sidecars", {}).get("colibri", {})
    return {"live_resident_gb": b.get("live_resident_gb"), "live_free_gb": b.get("live_free_gb"),
            "free_budget_gb": b.get("free_budget_gb"), "colibri_mem_gb": c.get("mem_gb"),
            "colibri_state": c.get("state")}


def snap(tag):
    return {"tag": tag, "t": time.time(), "vm": vm(), "gov": gov_mem(),
            "readyz": {k: httpx.get(f"{SC}/readyz", timeout=10).json().get(k)
                       for k in ("state", "model", "child_cpu_seconds", "child_uptime_s")}}


def chat(model, gen):
    t0 = time.time()
    r = httpx.post(f"{GOV}/llm/colibri/v1/chat/completions", timeout=TO, json={
        "model": model, "messages": [{"role": "user", "content": PROMPT}],
        "max_tokens": gen, "temperature": 0})
    dt = time.time() - t0
    j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"raw": r.text}
    u = j.get("usage") or {}
    n = u.get("completion_tokens")
    return {"status": r.status_code, "wall_s": round(dt, 2), "usage": u,
            "tok_s_wall": round(n / dt, 3) if n else None,
            "colibri_headers": {k: v for k, v in r.headers.items() if k.startswith("x-colibri")},
            "text": (j.get("choices") or [{}])[0].get("message", {}).get("content", "")[:400],
            "extra_keys": sorted(set(j) - {"id", "object", "created", "model", "choices", "usage"})}


def brio(model):
    t0 = time.time()
    r = httpx.post(f"{GOV}/llm/colibri/v1/brio", timeout=TO, json={
        "model": model, "state": PROMPT, "question": "Was customer money affected?",
        "options": ["yes", "no"]})
    return {"status": r.status_code, "wall_s": round(time.time() - t0, 2), "reply": r.json()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="colibri-qwen36")
    ap.add_argument("--gen", type=int, default=128)
    ap.add_argument("--runs", type=int, default=2)
    a = ap.parse_args()
    res = {"model": a.model, "gen": a.gen, "started": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    httpx.post(f"{SC}/admin/unload", params={"force": "true"}, timeout=60)
    time.sleep(3)
    res["before"] = snap("cold")
    runs = []
    for i in range(a.runs):
        r = chat(a.model, a.gen)
        r["after"] = snap(f"after-run-{i}")
        runs.append(r)
        print(f"run {i}: HTTP {r['status']} wall {r['wall_s']}s tokens {r['usage'].get('completion_tokens')} "
              f"tok/s(wall) {r['tok_s_wall']}", flush=True)
    res["runs"] = runs
    res["brio"] = brio(a.model)
    print(f"brio: HTTP {res['brio']['status']} {res['brio']['wall_s']}s", flush=True)
    res["after_brio"] = snap("after-brio")
    if len(runs) >= 2 and runs[0]["status"] == runs[1]["status"] == 200:
        res["load_s_est"] = round(runs[0]["wall_s"] - runs[1]["wall_s"], 1)
    b, w = res["before"], runs[-1]["after"]
    res["delta_gb"] = {"governor_resident": round((w["gov"]["live_resident_gb"] or 0) - (b["gov"]["live_resident_gb"] or 0), 1),
                       "colibri_subtree_rss": w["gov"]["colibri_mem_gb"],
                       "vm_file_backed": round(w["vm"]["file_backed"] - b["vm"]["file_backed"], 2),
                       "vm_anonymous": round(w["vm"]["anonymous"] - b["vm"]["anonymous"], 2),
                       "vm_active": round(w["vm"]["active"] - b["vm"]["active"], 2)}
    out = Path.home() / ".atelier/colibri-test-results" / f"bench-{time.strftime('%Y%m%dT%H%M%S')}-{a.model}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, indent=1, default=str))
    print(json.dumps({"load_s_est": res.get("load_s_est"), "delta_gb": res["delta_gb"]}, indent=1))
    print(f"→ {out}")
    print("__DONE__")


if __name__ == "__main__":
    main()
