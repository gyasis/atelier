#!/usr/bin/env python3
"""Unit tests for the admission gate core (pure logic, no I/O). Run:
    .venv/bin/python -m pytest test_admission.py -q     # or just: python test_admission.py
"""
import asyncio
import admission


def gate(**kw):
    defaults = dict(budget_gb=44.0, default_est_gb=18.0, num_parallel=1,
                    est_overrides={}, default_ttl_s=900.0, cliff_gb=55.0, live_floor_gb=4.0)
    defaults.update(kw)
    g = admission.Gate(**defaults)
    g.set_live(resident_gb=20.0, free_gb=40.0)  # plenty of room by default
    return g


_loop = asyncio.new_event_loop()
def run(coro):
    return _loop.run_until_complete(coro)


def test_two_medium_models_coexist():
    """20 + 20 under a 44 budget → BOTH granted in parallel, no queue."""
    g = gate()
    g.set_tags({"a:20": 17.0, "b:20": 17.0})  # ~19.5 est each
    d1 = run(g.admit("j1", "a:20"))
    d2 = run(g.admit("j2", "b:20"))
    assert d1.grant and d2.grant, (d1, d2)
    assert len(g.queue) == 0
    print("✓ two medium models co-reside")


def test_third_big_model_queues_not_evicts():
    """Two loaded, a third that doesn't fit → QUEUED, and the two leases survive."""
    g = gate(budget_gb=44.0)
    g.set_tags({"a": 17.0, "b": 17.0})
    run(g.admit("j1", "a"))
    run(g.admit("j2", "b"))
    d3 = run(g.admit("j3", "big", est_gb=30.0))
    assert not d3.grant and d3.position == 0, d3
    assert len(g.leases) == 2, "arrival must NOT evict running models"
    print("✓ over-budget job queues; running models untouched")


def test_release_frees_room_for_queued():
    g = gate(budget_gb=44.0)
    g.set_tags({"a": 17.0, "b": 17.0})
    a = run(g.admit("j1", "a"))                        # ~19.5 GB
    run(g.admit("j2", "b"))                             # ~19.5 GB → committed ~39
    d3 = run(g.admit("j3", "big", est_gb=24.0))         # 39+24 > 44 → queued
    assert not d3.grant
    run(g.release(lease_id=a.lease_id, job_id="j1"))   # free ~19.5 → committed ~19.5
    d3b = run(g.admit("j3", "big", est_gb=24.0))        # 19.5+24 = 43.5 ≤ 44 → grant
    assert d3b.grant, d3b
    print("✓ release lets the queued job in")


def test_same_model_shares_when_parallel():
    g = gate(num_parallel=2)
    g.set_tags({"m": 17.0})
    d1 = run(g.admit("j1", "m"))
    d2 = run(g.admit("j2", "m"))   # same model, slot 2 → free grant
    assert d1.grant and d2.grant and d2.reused and d2.reserved_gb == 0.0, (d1, d2)
    print("✓ same-model second request shares the resident copy (0 GB)")


def test_live_backstop_blocks_even_when_budget_says_ok():
    """Budget accounting says fine, but measured free RAM is tiny → HARD block."""
    g = gate(budget_gb=44.0)
    g.set_tags({"x": 17.0})
    g.set_live(resident_gb=30.0, free_gb=6.0)   # only 6 GB real free, floor=4
    d = run(g.admit("j1", "x"))                  # est ~19.5 > 6-4 → blocked
    assert not d.grant and "backstop" in d.reason, d
    print("✓ live-memory backstop overrides optimistic budget accounting")


def test_over_cliff_blocks_all():
    g = gate()
    g.set_tags({"x": 1.0})
    g.set_live(resident_gb=56.0, free_gb=8.0)    # already over the 55 cliff
    d = run(g.admit("j1", "x"))
    assert not d.grant and "cliff" in d.reason, d
    print("✓ over-cliff measured memory blocks new grants")


def test_idempotent_admit_renews():
    g = gate()
    g.set_tags({"m": 17.0})
    d1 = run(g.admit("j1", "m"))
    d2 = run(g.admit("j1", "m"))   # same job re-polls
    assert d2.grant and d2.lease_id == d1.lease_id and d2.reused, (d1, d2)
    assert len(g.leases) == 1
    print("✓ re-admit of same job_id renews its single lease")


def test_reap_drops_expired():
    g = gate(default_ttl_s=0.0)   # everything expires immediately
    g.set_tags({"m": 17.0})
    run(g.admit("j1", "m"))
    import time
    out = run(g.reap(time.time() + 1))
    assert out["active_leases"] == 0, out
    print("✓ reaper drops leases from dead clients")


def test_fifo_no_jump():
    """A later job that FITS must not jump ahead of an earlier job that is queued
    waiting on memory to FREE (FIFO fairness for fittable jobs — still strict)."""
    g = gate(budget_gb=20.0)
    g.set_tags({})
    run(g.admit("hold", "h", est_gb=16.0))     # granted → commits 16 of 20
    run(g.admit("big", "m", est_gb=10.0))      # 16+10>20 but 10<=budget → QUEUED (waits), head
    d_small = run(g.admit("small", "n", est_gb=2.0))  # 16+2<=20 fits, but behind big
    assert not d_small.grant and d_small.position == 1, d_small
    print("✓ FIFO preserved — fittable small job does not jump a waiting job")


def test_never_fits_fails_fast():
    """A job whose est exceeds the WHOLE LLM ceiling fails FAST (terminal), is NOT queued,
    and does not head-of-line-block a smaller job that fits (the gemma4-vs-ornith deadlock)."""
    g = gate(budget_gb=20.0)
    g.set_tags({})
    d_big = run(g.admit("big", "m", est_gb=30.0))     # 30 > 20 ceiling → can never fit
    assert not d_big.grant and d_big.terminal, d_big
    assert not g.queue, ("a terminal job must not be queued", g.queue)
    d_small = run(g.admit("small", "n", est_gb=2.0))  # fits, no longer blocked
    assert d_small.grant, d_small
    print("✓ never-fits fails fast (terminal) and doesn't block a fitting job")


def test_est_gb_param_heuristic():
    g = gate()
    g.set_tags({})  # nothing catalogued → fall through to the name heuristic
    assert g.est_gb("qwen2.5-0.5b") == 0.35, g.est_gb("qwen2.5-0.5b")
    assert g.est_gb("something-32b") == 22.4, g.est_gb("something-32b")
    assert g.est_gb("no-size-here") == g.default_est_gb
    print("✓ est_gb reads parameter count from the model name")


def test_route_honors_explicit_backend():
    g = gate()
    g.set_catalog({"ollama": ["qwen3:32b"], "mlxlm": ["qwen2.5-0.5b"]},
                  {"ollama": "http://o", "mlxlm": "http://m"})
    d = run(g.admit("j", "qwen3:32b", backend="ollama", est_gb=1))
    assert d.grant and d.backend == "ollama" and d.base_url == "http://o", d
    print("✓ explicit backend honored, base_url returned")


def test_route_auto_picks_capable_backend():
    g = gate()
    g.set_catalog({"ollama": ["qwen3:32b"], "mlxlm": ["qwen2.5-0.5b"]},
                  {"ollama": "http://o", "mlxlm": "http://m"})
    d = run(g.admit("j", "qwen2.5-0.5b", backend="auto", est_gb=1))
    assert d.grant and d.backend == "mlxlm" and "auto→mlxlm" in d.routed, d
    print("✓ auto routes to the only capable backend")


def test_route_auto_prefers_already_loaded():
    g = gate()
    # both can serve 'shared'; mlxlm already has it warm → prefer mlxlm (free)
    g.set_catalog({"ollama": ["shared"], "mlxlm": ["shared"]},
                  {"ollama": "http://o", "mlxlm": "http://m"})
    g.set_loaded({("mlxlm", "shared")})
    d = run(g.admit("j", "shared", backend="auto", est_gb=1))
    assert d.grant and d.backend == "mlxlm" and "already loaded" in d.routed, d
    print("✓ auto prefers a backend that already has the model loaded")


def test_route_auto_unknown_model_defaults_ollama():
    g = gate()
    g.set_catalog({"mlxlm": ["qwen2.5-0.5b"]}, {"ollama": "http://o", "mlxlm": "http://m"})
    d = run(g.admit("j", "brand-new:99b", backend="auto", est_gb=1))
    assert d.grant and d.backend == "ollama" and "may pull" in d.routed, d
    print("✓ auto falls back to ollama for an uncatalogued model")


def _warm_gate(loaded_name="big:q4"):
    """The 2026-09-14 per-call-reload shape: one big model loaded but IDLE, its memory
    counted by the poller as untracked, and too little budget left for a second copy."""
    g = gate(budget_gb=30.0)
    g.set_untracked_gb(26.0)
    g.set_loaded({("ollama", loaded_name)})
    return g


def test_idle_loaded_model_is_reused_not_queued():
    g = _warm_gate()
    d = run(g.admit("j1", "big:q4", est_gb=23.0))
    assert d.grant and d.reused and d.reserved_gb == 23.0, d
    other = run(g.admit("j2", "other:q4", est_gb=23.0))   # a DIFFERENT model still doesn't fit
    assert not other.grant, other
    print("✓ idle loaded model is reused instead of waiting for it to unload")


def test_busy_single_slot_model_still_queues():
    g = _warm_gate()
    assert run(g.admit("j1", "big:q4", est_gb=23.0)).grant
    d2 = run(g.admit("j2", "big:q4", est_gb=23.0))        # num_parallel=1, slot taken
    assert not d2.grant, d2
    print("✓ a busy single-slot model still queues the next request")


def test_idle_reuse_does_not_jump_queue():
    g = _warm_gate()
    q = run(g.admit("waiting", "other:q4", est_gb=23.0))  # head, needs the idle model evicted
    assert not q.grant
    d = run(g.admit("j1", "big:q4", est_gb=23.0))
    assert not d.grant, "reuse must not starve a job waiting for this model's eviction"
    print("✓ idle reuse does not jump a queued job")


def test_reuse_needs_exact_model_name():
    g = _warm_gate("qwen3:1.7b")
    assert not run(g.admit("j", "qwen3", est_gb=23.0)).grant, "substring is not a match"
    g2 = _warm_gate("phi4-mini:latest")
    assert run(g2.admit("j", "phi4-mini", est_gb=23.0)).grant, ":latest is the same model"
    print("✓ reuse matches exact model names (bare name == :latest)")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\nAll {len(tests)} admission tests passed.")
