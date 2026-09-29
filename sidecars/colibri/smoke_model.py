#!/usr/bin/env python3
"""smoke_model — first-contact check for a newly registered Colibri model behind the sidecar (Mac-side).

    .venv/bin/python smoke_model.py <alias>

1) cold load + a short plain answer, 2) a pi-shaped two-turn native tool loop (turn 2 carries the
tool result, so prefix reuse shows as a short turn 2), 3) resident memory: sidecar process tree
(governor's own measurement) and governor resident. Writes
~/.atelier/colibri-test-results/smoke-<ts>-<alias>.json and prints __DONE__.
"""
import json, sys, time
from pathlib import Path
import httpx

SC, GOV = "http://127.0.0.1:8783", "http://127.0.0.1:8799"
TO = httpx.Timeout(connect=5, read=None, write=30, pool=None)
alias = sys.argv[1]
TOOLS = [{"type": "function", "function": {"name": "read", "description": "Read a file",
          "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
         {"type": "function", "function": {"name": "bash", "description": "Run a shell command",
          "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}}]
res = {"alias": alias, "started": time.strftime("%Y-%m-%dT%H:%M:%S%z")}


def mem():
    a = httpx.get(f"{GOV}/agent?expand=true", timeout=20).json()["sidecars"].get("colibri", {})
    b = httpx.get(f"{GOV}/budget", timeout=10).json()
    return {"colibri_rss_gb": a.get("mem_gb"), "gov_resident_gb": b.get("live_resident_gb"),
            "gov_free_gb": b.get("live_free_gb")}


def chat(msgs, tools=None, n=64):
    body = {"model": alias, "messages": msgs, "max_tokens": n, "temperature": 0}
    if tools:
        body["tools"] = tools
    t = time.time()
    r = httpx.post(f"{SC}/v1/chat/completions", json=body, timeout=TO)
    dt = round(time.time() - t, 1)
    try:
        j = r.json()
    except Exception:
        j = {"raw": r.text[:400]}
    out = {"http": r.status_code, "s": dt, "usage": j.get("usage")}
    if r.status_code == 200:
        m = j["choices"][0]["message"]
        out.update(finish=j["choices"][0].get("finish_reason"), content=(m.get("content") or "")[:200],
                   tool_calls=[(c["function"]["name"], c["function"]["arguments"]) for c in m.get("tool_calls") or []],
                   _msg=m)
    else:
        out["error"] = str(j)[:400]
    print({k: v for k, v in out.items() if k != "_msg"}, flush=True)
    return out


httpx.post(f"{SC}/admin/unload", params={"force": "true"}, timeout=60)
time.sleep(12)
res["mem_before"] = mem()
res["plain"] = chat([{"role": "user", "content": "In one sentence: what does a mixture-of-experts model do?"}], n=48)
time.sleep(12)
res["mem_loaded"] = mem()
msgs = [{"role": "system", "content": "You are a coding assistant. Use the tools."},
        {"role": "user", "content": "calc.py has a bug in add(). Read calc.py first."}]
t1 = chat(msgs, TOOLS, n=200)
res["tool_turn1"] = {k: v for k, v in t1.items() if k != "_msg"}
if t1.get("tool_calls"):
    m = t1["_msg"]
    msgs.append({"role": "assistant", "content": m.get("content") or "", "tool_calls": m["tool_calls"]})
    for c in m["tool_calls"]:
        # answer each call the way the real tool would, or turn 2 measures confusion, not the model
        args = json.loads(c["function"]["arguments"] or "{}")
        if c["function"]["name"] == "bash" and "find" in args.get("command", ""):
            result = "./calc.py\n"
        elif c["function"]["name"] == "bash" and args.get("command", "").startswith(("pwd", "ls")):
            result = "/work\ncalc.py\ntest_calc.py\n"
        else:
            result = "def add(a, b):\n    return a - b\n"
        msgs.append({"role": "tool", "tool_call_id": c["id"], "content": result})
    t2 = chat(msgs, TOOLS, n=200)
    res["tool_turn2"] = {k: v for k, v in t2.items() if k != "_msg"}
res["mem_after"] = mem()
out = Path.home() / ".atelier/colibri-test-results" / f"smoke-{time.strftime('%Y%m%dT%H%M%S')}-{alias}.json"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(res, indent=1, default=str))
print("mem:", res["mem_before"], "->", res["mem_loaded"], "->", res["mem_after"])
print("→", out)
print("__DONE__")
