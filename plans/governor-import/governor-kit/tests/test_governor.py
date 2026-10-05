"""The cross-process adaptive governor — core behavior."""
import os
import socket
import time
import threading

import pytest

from governor import governor


def _params(rps=2.0, burst=3, inflight=2):
    return {"rate_rps": rps, "burst": burst, "max_inflight": inflight,
            "ramp_after": 2, "ramp_inc": 0.5, "backoff_mult": 0.5, "cooldown_base": 1.0}


def test_key_shared_by_gateway_host():
    e1 = {"base_url": "https://x.cloud.example.com/serving-endpoints", "name": "gw-model-a"}
    e2 = {"base_url": "https://x.cloud.example.com/serving-endpoints", "name": "gw-model-b"}
    assert governor.key_for(e1) == governor.key_for(e2)          # both models -> one bucket


def test_429_cuts_rate_and_sets_cooldown():
    key = "host:gw.test"
    p = _params(rps=2.0)
    seq = [429, 200]
    def do():
        s = seq.pop(0)
        return s, {"choices": [{"message": {"content": "ok"}}]}, ({"retry-after": "1"} if s == 429 else {})
    status, _ = governor.governed(key, p, do, agent="w1", role="worker", max_retries=2)
    st = governor.inspect(key)
    assert status == 200                                         # transparently retried past the 429
    assert st["rate"] <= 1.0                                     # multiplicative decrease from 2.0


def test_sustained_success_ramps_rate_back():
    key = "host:gw.test"
    p = _params(rps=4.0)
    # force rate down first
    h = governor.acquire(key, p, agent="w", role="worker")
    governor.report(h, ok=False, status=429, params=p)
    low = governor.inspect(key)["rate"]
    # ramp_after=2 successes -> additive increase
    for _ in range(4):
        h = governor.acquire(key, p, agent="w", role="worker", max_wait=5)
        governor.report(h, ok=True, status=200, params=p)
    assert governor.inspect(key)["rate"] > low


def test_priority_orchestrator_before_worker():
    key = "host:gw.test"
    p = _params()
    # register a worker waiter (earlier) and an orchestrator waiter (later) directly
    now = time.time()
    with governor._Lock(key):
        st = governor._load(key, p, now)
        st["waiters"] = {
            "wk": {"priority": governor.priority_for("worker"), "ts": now, "hb": now, "pid": os.getpid(), "agent": "w"},
            "or": {"priority": governor.priority_for("orchestrator"), "ts": now + 5, "hb": now, "pid": os.getpid(), "agent": "o"},
        }
        governor._save(key, st)
        # orchestrator is front despite arriving later (higher priority wins over FIFO)
        assert governor._is_front(st, "or") is True
        assert governor._is_front(st, "wk") is False


def test_concurrency_hard_cap_across_calls():
    key = "host:gw.test"
    p = _params(inflight=2)
    h1 = governor.acquire(key, p, agent="a", role="worker")
    h2 = governor.acquire(key, p, agent="b", role="worker")
    assert governor.inspect(key)["inflight"] == 2
    # a 3rd acquire must NOT be granted while 2 are in-flight, even fail-open
    got = {}
    def third():
        h3 = governor.acquire(key, p, agent="c", role="worker", max_wait=0.3)
        got["h"] = h3
    t = threading.Thread(target=third); t.start()
    time.sleep(0.6)
    assert "h" not in got, "3rd permit granted while concurrency cap full"
    governor.report(h1, ok=True, status=200, params=p)          # free a slot
    t.join(timeout=3)
    assert "h" in got, "3rd permit never granted after a slot freed"
    governor.report(h2, ok=True, status=200, params=p)
    governor.report(got["h"], ok=True, status=200, params=p)


def test_failopen_front_waiter_after_wait():
    key = "host:gw.test"
    p = _params(inflight=1)
    # put the gateway into a long cooldown
    h = governor.acquire(key, p, agent="w", role="worker")
    governor.report(h, ok=False, status=429, retry_after=30, params=p)
    assert governor.inspect(key)["cooldown_remaining"] > 5
    # a single front waiter, after max_wait, is let through (half-open probe)
    t0 = time.time()
    h2 = governor.acquire(key, p, agent="orch", role="orchestrator", max_wait=1.0)
    assert 0.9 <= time.time() - t0 < 6                          # waited ~max_wait then proceeded
    governor.report(h2, ok=True, status=200, params=p)


def test_transport_error_releases_neutrally():
    """A non-timeout transport failure frees the permit without touching the rate
    (counting it as success would ramp the rate up while the gateway is failing)."""
    key = "host:gw.test"
    p = _params(rps=2.0)
    def do():
        raise ConnectionError("dns is not evidence about load")
    with pytest.raises(ConnectionError):
        governor.governed(key, p, do, agent="w", role="worker")
    st = governor.inspect(key)
    assert st["inflight"] == 0
    assert st["rate"] == 2.0                                     # untouched
    assert st["consecutive_ok"] == 0 and st["consecutive_err"] == 0


def test_timeout_is_an_overload_signal():
    """A saturated gateway stops answering before it starts refusing — a timeout must
    CUT the rate, not ramp it."""
    key = "host:gw.test"
    p = _params(rps=2.0)
    def do():
        raise socket.timeout("gateway never answered")
    with pytest.raises(socket.timeout):
        governor.governed(key, p, do, agent="w", role="worker")
    st = governor.inspect(key)
    assert st["rate"] <= 1.0                                     # multiplicative cut happened
    assert st["cooldown_remaining"] > 0
    assert st["inflight"] == 0


def test_pid_alive():
    assert governor.pid_alive(None) is False
    assert governor.pid_alive(-1) is False
    assert governor.pid_alive(os.getpid()) is True
    assert governor.pid_alive(4194311) is False                  # not a real pid anywhere
