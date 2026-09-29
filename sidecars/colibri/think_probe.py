#!/usr/bin/env python3
# think_probe.py — which request parameter actually turns thinking off for a colibri model?
import json, sys, time, httpx
alias = sys.argv[1]
TO = httpx.Timeout(connect=5, read=None, write=30, pool=None)
variants = {
    "none (server flags only)": {},
    "reasoning_effort=none": {"reasoning_effort": "none"},
    "enable_thinking=false": {"enable_thinking": False},
    "chat_template_kwargs.enable_thinking=false": {"chat_template_kwargs": {"enable_thinking": False}},
}
for label, extra in variants.items():
    body = {"model": alias, "messages": [{"role": "user", "content": "Reply with only the word OK."}],
            "max_tokens": 48, "temperature": 0, **extra}
    t = time.time()
    r = httpx.post("http://127.0.0.1:8783/v1/chat/completions", json=body, timeout=TO)
    dt = time.time() - t
    if r.status_code != 200:
        print(f"{label:45} HTTP {r.status_code} {r.text[:150]}"); continue
    m = r.json()["choices"][0]["message"]
    rc = m.get("reasoning_content") or m.get("reasoning") or ""
    print(f"{label:45} {dt:6.1f}s reasoning={len(rc):4d} chars  content={(m.get('content') or '')[:40]!r}")
print("__DONE__")
