import pytest

from governor import governor


@pytest.fixture(autouse=True)
def _fresh_state(tmp_path, monkeypatch):
    """Each test gets an empty state dir AND a cleared in-process mirror. The mirror
    exists as the unwritable-disk fallback; without clearing it, a test with a fresh
    state dir would inherit the previous test's in-memory state (e.g. its cooldown)."""
    governor._MEM_STATE.clear()
    governor.DEGRADED.update({"degraded": False, "reason": None})
    monkeypatch.setenv("GOVERNOR_STATE_DIR", str(tmp_path))
    return "host:gw.test"
