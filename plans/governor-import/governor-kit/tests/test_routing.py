"""Pivot / failover on top of the governor."""
import pytest

from governor import governor, routing


CONFIG = {
    "providers": {
        "openrouter": {"base_url": "https://openrouter.ai/api/v1"},
        "local": {"base_url": "http://127.0.0.1:11434"},
        "anthropic": {"base_url": "https://api.anthropic.com"},
    },
    "roles": {"worker": "openrouter"},
}


def _trip(prov: str, retry_after: float):
    entry = CONFIG["providers"][prov]
    key = governor.key_for(entry)
    p = governor.params_for(entry)
    h = governor.acquire(key, p, agent="w", role="worker")
    governor.report(h, ok=False, status=429, retry_after=retry_after, params=p)


def test_pivot_reroutes_when_primary_gateway_open(tmp_path, monkeypatch):
    monkeypatch.setenv("GOVERNOR_STATE_DIR", str(tmp_path / "st2"))
    cfg = {**CONFIG, "fallbacks": {"worker": ["local"]}}
    # healthy primary -> no pivot
    name, entry, pivoted, h = routing.resolve(cfg, "worker")
    assert name == "openrouter" and not pivoted
    # trip the primary's circuit (sustained overload)
    _trip("openrouter", 60)
    assert governor.health(governor.key_for(CONFIG["providers"]["openrouter"])) == "open"
    # now it pivots to the local fallback so agents keep working
    name2, entry2, pivoted2, h2 = routing.resolve(cfg, "worker")
    assert name2 == "local" and pivoted2 is True


def test_pivot_all_open_picks_least_cooled(tmp_path, monkeypatch):
    monkeypatch.setenv("GOVERNOR_STATE_DIR", str(tmp_path / "st3"))
    cfg = {**CONFIG, "fallbacks": {"worker": ["anthropic"]}}
    for prov, ra in (("openrouter", 60), ("anthropic", 10)):
        _trip(prov, ra)
    name, _, _, health = routing.resolve(cfg, "worker")
    assert name == "anthropic" and health == "open"             # least-cooled of the open gateways


def test_unconfigured_role_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("GOVERNOR_STATE_DIR", str(tmp_path / "st4"))
    with pytest.raises(ValueError):
        routing.resolve({"providers": {}, "roles": {}}, "worker")
