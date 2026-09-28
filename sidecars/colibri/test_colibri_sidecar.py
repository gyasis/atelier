#!/usr/bin/env python3
"""test_colibri_sidecar — contract + overnight-behaviour tests for the Colibri sidecar.

Runs ON THE MAC STUDIO (it inspects process groups with ps and freezes them with SIGSTOP).

    .venv/bin/python test_colibri_sidecar.py                 # fast suite (~1-2 min)
    .venv/bin/python test_colibri_sidecar.py --long          # + lease survival past the TTL (~17 min)
    COLIBRI_TEST_MODEL=colibri-qwen36 ... --only sync,unload # pick tests

A "slow job" is made on demand by SIGSTOP-ing the engine's process group mid-request: the
request hangs exactly like a very slow or stuck generation, deterministically, on any model.
Default model is the tiny OLMoE TEST FIXTURE (random weights; output is gibberish): these
tests exercise the real engine + gateway + sidecar + governor, never output quality.

Results: printed PASS/FAIL per test and written to ~/.atelier/colibri-test-results/<ts>.json.
Exit 1 if any test fails.
"""
import argparse
import json
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

import httpx

SC = os.environ.get("COLIBRI_URL", "http://127.0.0.1:8783")
GOV = os.environ.get("GOVERNOR_URL", "http://127.0.0.1:8799")
MODEL = os.environ.get("COLIBRI_TEST_MODEL", "colibri-olmoe-tiny")
CHILD_PORT = int(os.environ.get("COLIBRI_CHILD_PORT", "18783"))
TTL_S = float(os.environ.get("ATELIER_LLM_LEASE_TTL_S", "900"))
LONG_TO = httpx.Timeout(connect=5.0, read=None, write=30.0, pool=None)
RESULTS: list[dict] = []


class Skip(Exception):
    """A precondition of the box, not a defect — reported as SKIP with the numbers."""


def model_ram():
    return next(m["ram_gb"] for m in sc("GET", "/models").json()["data"] if m["id"] == MODEL)


def free_budget():
    return httpx.get(GOV + "/budget", timeout=10).json().get("free_budget_gb", 0)


def need_room():
    """Tests that need a governor grant cannot pass on a box without room — skip, don't fail."""
    ram, free = model_ram(), free_budget()
    if free < ram:
        raise Skip(f"governor free budget {free} GB < {MODEL} estimate {ram} GB")


def colibri_queue_entries():
    q = httpx.get(GOV + "/budget", timeout=10).json().get("queue", [])
    return [e for e in q if e.get("backend") == "colibri"]


def chat_body(n=16, text="Say hello."):
    return {"model": MODEL, "messages": [{"role": "user", "content": text}],
            "max_tokens": n, "temperature": 0}


def sc(method, path, **kw):
    kw.setdefault("timeout", 30)
    return httpx.request(method, SC + path, **kw)


def readyz():
    return sc("GET", "/readyz").json()


def budget_leases(backend="colibri"):
    d = httpx.get(GOV + "/budget", timeout=10).json()
    return [L for L in d.get("active_leases", []) if L.get("backend") == backend]


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def cmdline(pid):
    return subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True,
                          text=True).stdout.strip()


def signal_group(pids, sig):
    for p in pids:
        try:
            os.kill(p, sig)
        except ProcessLookupError:
            pass


def port_listening(port):
    out = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
                         capture_output=True, text=True).stdout
    return bool(out.strip())


def wait_for(pred, timeout, every=1.0, what="condition"):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = pred()
        if v:
            return v
        time.sleep(every)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


def job_status(job_id):
    return sc("GET", f"/jobs/{job_id}").json()


def warm():
    r = sc("POST", "/v1/chat/completions", json=chat_body(4), timeout=LONG_TO)
    assert r.status_code == 200, f"warm-up chat HTTP {r.status_code}: {r.text[:300]}"
    return readyz()


def test(name):
    def deco(fn):
        fn._test_name = name
        return fn
    return deco


# ---------------------------------------------------------------- tests
@test("health")
def t_health():
    h = sc("GET", "/healthz").json()
    assert h["ok"] and h["coli_present"], h
    assert MODEL in h["models"], f"{MODEL} not registered: {h['models']}"
    a = sc("GET", "/agent").json()
    assert a["service"] == "colibri" and a["methods"], "agent manifest incomplete"
    g = httpx.get(GOV + "/agent?expand=true", timeout=15).json()
    assert "colibri" in g.get("sidecars", {}), "governor /agent does not list colibri"
    return {"models": h["models"], "governor_state": g["sidecars"]["colibri"].get("state")}


@test("cold")
def t_cold():
    r = sc("POST", "/admin/unload", params={"force": "true"}).json()
    d = readyz()
    assert d["state"] == "cold" and d["child_group_pids"] == [], d
    assert not port_listening(CHILD_PORT), "child port still listening after unload"
    return {"unload": r}


@test("sync")
def t_sync():
    t0 = time.time()
    r = sc("POST", "/v1/chat/completions", json=chat_body(16), timeout=LONG_TO)
    assert r.status_code == 200, f"HTTP {r.status_code}: {r.text[:300]}"
    j = r.json()
    assert j.get("choices"), j
    d = readyz()
    pids = d["child_group_pids"]
    cmds = {p: cmdline(p) for p in pids}
    assert d["state"] == "warm" and d["model"] == MODEL, d
    assert len(pids) >= 2, f"expected gateway + engine in the group, got {cmds}"
    engine = [p for p, c in cmds.items() if c and "python" not in c.split()[0].rsplit("/", 1)[-1]]
    assert engine, f"no engine binary in the child group: {cmds}"
    return {"seconds": round(time.time() - t0, 2), "group": cmds,
            "text": j["choices"][0]["message"]["content"][:80], "usage": j.get("usage")}


@test("busy")
def t_busy():
    """Freeze the engine mid-job: unload must refuse, CPU must read flat, 2nd job must queue."""
    need_room()
    pids = warm()["child_group_pids"]
    signal_group(pids, signal.SIGSTOP)
    try:
        a = sc("POST", "/jobs", json={"path": "v1/chat/completions", "body": chat_body(32),
                                      "label": "test-busy-A"}).json()
        b = sc("POST", "/jobs", json={"path": "v1/chat/completions", "body": chat_body(8),
                                      "label": "test-busy-B"}).json()
        wait_for(lambda: job_status(a["job_id"])["status"] == "running", 60, what="job A running")
        c1 = readyz()["child_cpu_seconds"]
        time.sleep(5)
        c2 = readyz()["child_cpu_seconds"]
        assert c1 == c2, f"cpu advanced while frozen ({c1}→{c2}) — liveness signal is wrong"
        refused = sc("POST", "/admin/unload").json()
        assert refused.get("refused") == "busy", f"unload not refused while busy: {refused}"
        assert job_status(b["job_id"])["status"] == "queued", "job B did not wait behind A"
        lease = budget_leases()
        assert lease, "no governor lease held while the job runs"
    finally:
        signal_group(pids, signal.SIGCONT)
    ja = wait_for(lambda: (s := job_status(a["job_id"]))["status"] in ("done", "failed") and s, 120,
                  what="job A finish")
    jb = wait_for(lambda: (s := job_status(b["job_id"]))["status"] in ("done", "failed") and s, 120,
                  what="job B finish")
    assert ja["status"] == "done" and jb["status"] == "done", (ja.get("error"), jb.get("error"))
    wait_for(lambda: not budget_leases(), 30, what="lease release")
    return {"cpu_flat_while_frozen": [c1, c2], "unload_reply": refused, "lease_while_running": lease,
            "job_a_s": ja.get("run_seconds"), "job_b_s": jb.get("run_seconds")}


@test("unload")
def t_unload():
    """Unload must take down the WHOLE group (gateway + engine), not orphan the engine."""
    pids = warm()["child_group_pids"]
    r = sc("POST", "/admin/unload").json()
    assert r["unloaded"], r
    left = [p for p in pids if alive(p)]
    assert not left, f"orphaned processes after unload: {[(p, cmdline(p)) for p in left]}"
    assert not port_listening(CHILD_PORT), "child port still listening"
    return {"killed": pids}


@test("proxy")
def t_proxy():
    """Through the governor front door: admitted → lease held while running → released."""
    need_room()
    pids = warm()["child_group_pids"]
    out = {}

    def call():
        r = httpx.post(GOV + "/llm/colibri/v1/chat/completions", json=chat_body(16), timeout=LONG_TO)
        out["status"], out["body"] = r.status_code, r.text[:300]

    signal_group(pids, signal.SIGSTOP)
    th = threading.Thread(target=call)
    try:
        th.start()
        lease = wait_for(budget_leases, 30, what="proxy lease")
    finally:
        signal_group(pids, signal.SIGCONT)
    th.join(120)
    assert out.get("status") == 200, out
    wait_for(lambda: not budget_leases(), 30, what="proxy lease release")
    return {"lease": lease, "status": out["status"]}


@test("persist")
def t_persist():
    """Restart the sidecar mid-job: the running job is marked interrupted (never silently
    retried), the queued job survives the restart and runs, and no engine is orphaned."""
    pids = warm()["child_group_pids"]
    signal_group(pids, signal.SIGSTOP)
    a = sc("POST", "/jobs", json={"path": "v1/chat/completions", "body": chat_body(16),
                                  "label": "test-persist-A"}).json()
    b = sc("POST", "/jobs", json={"path": "v1/chat/completions", "body": chat_body(8),
                                  "label": "test-persist-B"}).json()
    wait_for(lambda: job_status(a["job_id"])["status"] == "running", 60, what="job A running")
    uid = os.getuid()
    subprocess.run(["launchctl", "kickstart", "-k", f"gui/{uid}/io.macstudio.hub.colibri"], check=True)
    wait_for(lambda: _up(), 60, what="sidecar back up")
    orphans = [p for p in pids if alive(p)]
    signal_group(orphans, signal.SIGCONT)
    assert not orphans, f"engine group survived a sidecar restart: {[(p, cmdline(p)) for p in orphans]}"
    sa = job_status(a["job_id"])
    assert sa["status"] == "interrupted", f"running job after restart: {sa['status']}"
    sb = wait_for(lambda: (s := job_status(b["job_id"]))["status"] in ("done", "failed") and s, 180,
                  what="queued job B to run after restart")
    assert sb["status"] == "done", sb.get("error")
    wait_for(lambda: not budget_leases(), 60, what="lease release after restart")
    return {"a": sa["status"], "b": sb["status"], "b_run_s": sb.get("run_seconds"),
            "reaped_at_boot": readyz().get("reaped_at_boot")}


@test("cancel")
def t_cancel():
    """A queued job can be cancelled; a finished job cannot."""
    pids = warm()["child_group_pids"]
    signal_group(pids, signal.SIGSTOP)
    try:
        a = sc("POST", "/jobs", json={"path": "v1/chat/completions", "body": chat_body(4),
                                      "label": "test-cancel-A"}).json()
        b = sc("POST", "/jobs", json={"path": "v1/chat/completions", "body": chat_body(4),
                                      "label": "test-cancel-B"}).json()
        r = sc("DELETE", f"/jobs/{b['job_id']}").json()
        assert r.get("ok"), r
        forced = sc("DELETE", f"/jobs/{a['job_id']}")
        wait_for(lambda: job_status(a["job_id"])["status"] in ("waiting_admission", "loading", "running",
                                                                "done", "failed", "cancelled"), 60,
                 what="job A to start")
    finally:
        signal_group(pids, signal.SIGCONT)
    sa = wait_for(lambda: (s := job_status(a["job_id"]))["status"] in
                  ("done", "failed", "cancelled", "refused") and s, 180, what="job A to settle")
    sb = job_status(b["job_id"])
    assert sb["status"] == "cancelled", f"queued job B not cancelled: {sb['status']}"
    again = sc("DELETE", f"/jobs/{b['job_id']}")
    assert again.status_code == 409, "cancelling a finished job must be refused"
    return {"b": sb["status"], "a": sa["status"], "a_cancel_http": forced.status_code}


@test("polite")
def t_polite():
    """A job that cannot be admitted must wait OUTSIDE the governor queue (never block other
    callers), must not count as busy, and must stay cancellable. The "full box" is made on
    purpose: a placeholder governor lease sized so this model no longer fits (no real memory)."""
    sc("POST", "/admin/unload", params={"force": "true"})
    wait_for(lambda: readyz()["state"] == "cold", 30, what="cold")
    time.sleep(12)                                    # let the governor poll see it cold
    ram, free = model_ram(), free_budget()
    est = round(free - ram + 1, 1)                    # leaves ram-1 GB: just too little
    if est <= 0 or est > free:
        raise Skip(f"cannot size a placeholder (free {free}, est {ram})")
    blocker = "colibri-test-polite-placeholder"
    g = httpx.post(GOV + "/admit", json={"job_id": blocker, "model": "colibri-test-placeholder",
                                        "backend": "colibri", "est_gb": est}, timeout=15).json()
    if not g.get("grant"):
        raise Skip(f"placeholder lease not granted: {g.get('reason')}")
    j = None
    try:
        j = sc("POST", "/jobs", json={"path": "v1/chat/completions", "body": chat_body(4),
                                      "label": "test-polite"}).json()
        wait_for(lambda: (job_status(j["job_id"]).get("admission") or {}).get("attempts", 0) >= 1, 60,
                 what="first (refused) admission attempt")
        samples = []
        for _ in range(8):
            samples.append(len(colibri_queue_entries()))
            time.sleep(2)
        busy = readyz()["busy"]
        st = job_status(j["job_id"])
        assert st["status"] == "waiting_admission", f"job should be waiting: {st['status']}"
        assert busy is False, "a job waiting for admission must not count as busy"
        assert samples.count(0) >= 6, f"colibri entry sat in the governor queue: {samples}"
    finally:
        if j:
            sc("DELETE", f"/jobs/{j['job_id']}")
        httpx.post(GOV + "/release", json={"job_id": blocker}, timeout=15)
    s2 = wait_for(lambda: (s := job_status(j["job_id"]))["status"] in ("cancelled", "done", "refused") and s,
                  30, what="cancel to take effect")
    assert s2["status"] == "cancelled", s2["status"]
    assert not colibri_queue_entries(), "colibri entry left in the governor queue after cancel"
    return {"free_gb": free, "placeholder_gb": est, "est_gb": ram, "queue_samples": samples,
            "attempts": (s2.get("admission") or {}).get("attempts"), "final": s2["status"]}


@test("requeue")
def t_requeue():
    """A job that was only WAITING for admission when the sidecar restarted never touched the
    model: it must come back queued/waiting, not 'interrupted'. The wait is made on purpose with
    a placeholder governor lease sized so the model no longer fits (no real memory used)."""
    sc("POST", "/admin/unload", params={"force": "true"})
    wait_for(lambda: readyz()["state"] == "cold", 30, what="cold")
    time.sleep(12)
    ram, free = model_ram(), free_budget()
    est = round(free - ram + 1, 1)
    if est <= 0 or est > free:
        raise Skip(f"cannot size a placeholder (free {free}, est {ram})")
    blocker = "colibri-test-requeue-placeholder"
    g = httpx.post(GOV + "/admit", json={"job_id": blocker, "model": "colibri-test-placeholder",
                                        "backend": "colibri", "est_gb": est}, timeout=15).json()
    if not g.get("grant"):
        raise Skip(f"placeholder lease not granted: {g.get('reason')}")
    j = None
    try:
        j = sc("POST", "/jobs", json={"path": "v1/chat/completions", "body": chat_body(4),
                                      "label": "test-requeue"}).json()
        wait_for(lambda: job_status(j["job_id"])["status"] == "waiting_admission", 60, what="waiting")
        subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/io.macstudio.hub.colibri"],
                       check=True)
        wait_for(_up, 60, what="sidecar back up")
        st = job_status(j["job_id"])["status"]
        assert st in ("queued", "waiting_admission"), f"waiting job became {st!r} after restart"
    finally:
        if j:
            wait_for(_up, 60, what="sidecar up for cancel")
            sc("DELETE", f"/jobs/{j['job_id']}")
        httpx.post(GOV + "/release", json={"job_id": blocker}, timeout=15)
    s2 = wait_for(lambda: (s := job_status(j["job_id"]))["status"] in ("cancelled", "done") and s, 60,
                  what="cancel after restart")
    assert not colibri_queue_entries(), "colibri entry left in the governor queue"
    return {"after_restart": st, "final": s2["status"]}


@test("crash")
def t_crash():
    """SIGKILL the sidecar itself (no shutdown runs). launchd restarts it, and the new process
    must reap the orphaned engine group from the pgid file — the reaper's only real path."""
    pids = warm()["child_group_pids"]
    out = subprocess.run(["lsof", "-nP", "-iTCP:8783", "-sTCP:LISTEN", "-t"], capture_output=True,
                         text=True).stdout.split()
    assert out, "could not find the sidecar's listening pid"
    os.kill(int(out[0]), signal.SIGKILL)
    wait_for(lambda: not _up(), 10, every=0.2, what="sidecar to die")
    wait_for(lambda: _up(), 60, what="launchd to restart the sidecar")
    d = readyz()
    left = [p for p in pids if alive(p)]
    assert not left, f"orphans survived the crash restart: {[(p, cmdline(p)) for p in left]}"
    assert d["reaped_at_boot"], "no orphan was reaped — the crash did not exercise the reaper"
    assert not port_listening(CHILD_PORT), "child port still held after reap"
    return {"orphans_before": pids, "reaped_at_boot": d["reaped_at_boot"]}


def _up():
    try:
        return sc("GET", "/healthz", timeout=3).status_code == 200
    except Exception:
        return False


@test("long")
def t_long():
    """Hold a proxied call AND a job past the lease TTL. The lease must be renewed (age_s
    resets) and never vanish, and both calls must still return. Freeze = a slow generation."""
    hold = TTL_S + 60
    pids = warm()["child_group_pids"]
    out = {}

    def call():
        r = httpx.post(GOV + "/llm/colibri/v1/chat/completions", json=chat_body(8), timeout=LONG_TO)
        out["status"] = r.status_code

    signal_group(pids, signal.SIGSTOP)
    th = threading.Thread(target=call)
    th.start()
    job = sc("POST", "/jobs", json={"path": "v1/chat/completions", "body": chat_body(8),
                                    "label": "test-long"}).json()
    ages, gaps, t0 = [], 0, time.time()
    try:
        while time.time() - t0 < hold:
            ls = budget_leases()
            if not ls:
                gaps += 1
            ages.append([round(time.time() - t0), [L["age_s"] for L in ls]])
            time.sleep(15)
    finally:
        signal_group(pids, signal.SIGCONT)
    th.join(300)
    js = wait_for(lambda: (s := job_status(job["job_id"]))["status"] in ("done", "failed") and s, 300,
                  what="long job finish")
    max_age = max((a for _, al in ages for a in al), default=0)
    assert gaps == 0, f"lease disappeared in {gaps} samples while the call was running"
    assert max_age < TTL_S, f"lease age reached {max_age}s ≥ TTL {TTL_S}s — not renewed"
    assert out.get("status") == 200 and js["status"] == "done", (out, js.get("error"))
    return {"held_s": hold, "max_lease_age_s": max_age, "samples": ages[-6:],
            "proxy_status": out["status"], "job": js["status"]}


ALL = [t_health, t_cold, t_sync, t_busy, t_unload, t_proxy, t_cancel, t_polite, t_requeue, t_persist, t_crash]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--long", action="store_true", help="also run the >TTL lease survival test")
    ap.add_argument("--only", default="", help="comma list of test names")
    a = ap.parse_args()
    tests = ALL + ([t_long] if a.long else [])
    if a.only:
        want = set(a.only.split(","))
        tests = [t for t in ALL + [t_long] if t._test_name in want]
    print(f"model={MODEL} sidecar={SC} governor={GOV}")
    for t in tests:
        t0 = time.time()
        try:
            info = t()
            RESULTS.append({"test": t._test_name, "ok": True, "s": round(time.time() - t0, 1), "info": info})
        except Skip as e:
            RESULTS.append({"test": t._test_name, "ok": True, "skipped": str(e), "s": round(time.time() - t0, 1)})
            print(f"SKIP {t._test_name:8} {time.time()-t0:6.1f}s  {e}")
            continue
            print(f"PASS {t._test_name:8} {time.time()-t0:6.1f}s  {json.dumps(info, default=str)[:220]}")
        except Exception as e:
            RESULTS.append({"test": t._test_name, "ok": False, "s": round(time.time() - t0, 1),
                            "error": f"{type(e).__name__}: {e}"})
            print(f"FAIL {t._test_name:8} {time.time()-t0:6.1f}s  {type(e).__name__}: {e}")
    outdir = Path.home() / ".atelier/colibri-test-results"
    outdir.mkdir(parents=True, exist_ok=True)
    f = outdir / f"{time.strftime('%Y%m%dT%H%M%S')}-{MODEL}.json"
    f.write_text(json.dumps({"model": MODEL, "results": RESULTS}, indent=1, default=str))
    failed = [r for r in RESULTS if not r["ok"]]
    skipped = [r for r in RESULTS if r.get("skipped")]
    print(f"{len(RESULTS)-len(failed)-len(skipped)} passed, {len(skipped)} skipped, "
          f"{len(failed)} failed → {f}")
    print("__DONE__")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
