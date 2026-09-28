#!/usr/bin/env python3
"""check_double_count — live check that a leased WARM LLM sidecar model is not also counted as
untracked memory by the governor (the 2026-09-28 self-blocking bug). Mac-side, one backend per arg.

    check_double_count.py llamacpp gemma4-12b http://127.0.0.1:8771
    check_double_count.py colibri colibri-qwen36 http://127.0.0.1:8783
"""
import sys
import time

import httpx

GOV = "http://127.0.0.1:8799"
backend, model, base = sys.argv[1:4]
TO = httpx.Timeout(connect=5, read=None, write=30, pool=None)


def budget():
    return httpx.get(f"{GOV}/budget", timeout=10).json()


def settle(pred, what, t=60):
    t0 = time.time()
    while time.time() - t0 < t:
        b = budget()
        if pred(b):
            return b
        time.sleep(3)
    raise SystemExit(f"FAIL {backend}: timed out waiting for {what}: {budget()}")


# 1. warm the model DIRECTLY on the sidecar (no lease), then wait for the governor to see it
r = httpx.post(f"{base}/v1/chat/completions", timeout=TO, json={
    "model": model, "messages": [{"role": "user", "content": "Say OK."}], "max_tokens": 4})
assert r.status_code == 200, f"warm-up HTTP {r.status_code}: {r.text[:200]}"
rss = None
t0 = time.time()
while time.time() - t0 < 60:
    a = httpx.get(f"{GOV}/agent?expand=true", timeout=20).json()["sidecars"].get(backend, {})
    if a.get("state") not in (None, "cold") and (a.get("mem_gb") or 0) > 0.5:
        rss = a["mem_gb"]
        break
    time.sleep(3)
assert rss, f"governor never saw {backend} warm"
time.sleep(12)                                     # one more poll so untracked includes it
before = budget()["untracked_gb"]

# 2. lease THAT model: must be a warm-reuse grant, and its RSS must leave "untracked"
job = f"double-count-check-{backend}"
d = httpx.post(f"{GOV}/admit", timeout=15, json={"job_id": job, "model": model,
                                                 "backend": backend, "est_gb": 1.0}).json()
try:
    assert d.get("grant"), f"not granted: {d}"
    after_b = settle(lambda b: b["untracked_gb"] <= before - rss * 0.8, "untracked to drop by the model's RSS")
    after = after_b["untracked_gb"]
    print(f"PASS {backend}: model {model} rss {rss} GB | untracked {before} → {after} GB while leased "
          f"| grant reserved {d.get('reserved_gb')} GB ({d.get('reason')})")
finally:
    httpx.post(f"{GOV}/release", json={"job_id": job}, timeout=15)
    httpx.post(f"{base}/admin/unload", timeout=60)
