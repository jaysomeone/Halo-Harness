"""tests.helpers.provider_env_defaults -- H15 part 2 addendum 3.1 fallout:
`is_enabled()` now auto-detects from REAL credentials (`providers/
enablement.py`) instead of failing open with no `providers` block at all --
a test file that builds `or:`/`dbx:`/`ant:` model refs (directly via
`parse_model_ref`, or indirectly via a real `Session`/`Controller`) to
exercise something ELSE entirely (tool dispatch, hooks, compaction,
permissions, steering, ...) and never cared about enablement itself now
needs a believable (never real) credential present for `is_enabled()` to
see, or `parse_model_ref` refuses it outright.

Call `ensure_default_provider_credentials()` ONCE at module level (import
time, before any test registration) in such a file -- `os.environ.
setdefault` never overwrites a value already present (a live shell var, or
one an earlier-imported module already set), and never touches a value an
individual test later sets/clears itself for its OWN purpose (that test's
own save/restore still works exactly as before, just starting from this
default instead of unset). NEVER use this in a file that deliberately tests
enablement/credential-ABSENCE behavior itself (`test_h15_provider_
enablement.py`, `test_cc_models.py`, `test_dbx_tui_surface.py`, ...) --
those need the real absence, not a default that would mask it.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def ensure_default_provider_credentials() -> None:
    """Believable (never real) credentials for all three prefixed
    providers, `setdefault` so an already-set value (a live shell var, or
    one an earlier-imported module already left behind) is never
    overwritten, and an individual test's own later save/restore of these
    same names still works exactly as before, just starting from this
    default instead of unset."""
    os.environ.setdefault("OPENROUTER_API_KEY", "sk-or-test-default")
    os.environ.setdefault("DATABRICKS_HOST", "https://test-default.cloud.databricks.com")
    os.environ.setdefault("DATABRICKS_TOKEN", "test-default-token")
    os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-default")
    # 1.0.1 part 2 fixpass finding 10: every background catalog/balance
    # worker (the TUI's launch-time catalog refresh, the OpenRouter balance
    # worker, /model's own open-time refresh, headless.build_session's
    # session-start Databricks thread) returns immediately when this is
    # set -- without it, every FakeController/real-controller BridgeApp
    # mount in test_tui.py sent a REAL `GET https://openrouter.ai/api/v1/key`
    # with the fake key above (a developer's own real exported
    # OPENROUTER_API_KEY would survive the `setdefault` just above and its
    # balance would show up for real), and a real-controller pilot's launch
    # worker resolved `test-default.cloud.databricks.com` and hit
    # `https://api.anthropic.com/v1/models` with the fake ANTHROPIC_API_KEY
    # -- contradicting headless.py's own documented contract that building
    # a session never triggers first-time catalog discovery.
    os.environ.setdefault("BRIDGE_TEST_NO_BACKGROUND_NET", "1")
    # Halo 2.0.2 round C: `doctor._check_drain_tick_rate` measures for a
    # real 2 s by default (the brief's own ask) -- several tests call
    # `run_checks()`/`run_checks_structured()`, some more than once, so
    # left alone this would add real wall-clock seconds, repeatedly, to
    # the suite for no actual signal (a real `halo doctor` run never sets
    # this). `setdefault` so a test that deliberately wants the real
    # window (none do today) can still set it first.
    os.environ.setdefault("BRIDGE_TEST_DRAIN_TICK_WINDOW_S", "0.15")
    # W6b section E: a module that never sets `BRIDGE_TEST_CC_AUTH_STATUS`
    # itself (most don't -- only the ones that deliberately exercise the
    # claude.ai/cc: auth path do) left `claude_auth_status()`/connector
    # discovery free to spawn a REAL `claude auth status` and `claude mcp
    # list` against whatever `claude` happens to be on PATH. Harmless when
    # nothing is installed (a fast ClaudeCodeNotFoundError), but on a box
    # with a logged-in native `claude` (verified on WSL) that is a real,
    # slow (~22s) network-touching subprocess tree per module/child process
    # -- exactly the kind of non-hermetic dependency this file exists to
    # close off. `setdefault` so a test that wants the logged-in path (or
    # any other shape) by setting this var itself, before or after import,
    # is never overridden by this default.
    os.environ.setdefault("BRIDGE_TEST_CC_AUTH_STATUS", json.dumps({"loggedIn": False}))
    # Round 5i part 2: the `cx:`/`codex_models` counterpart of the
    # `BRIDGE_TEST_CC_AUTH_STATUS` default just above -- same reasoning,
    # this build host has a real (not logged in) `codex` on PATH, so a
    # module that never sets `BRIDGE_TEST_CODEX_LOGIN_STATUS` itself would
    # otherwise spawn a real `codex login status` subprocess.
    os.environ.setdefault("BRIDGE_TEST_CODEX_LOGIN_STATUS", "Not logged in")


def ensure_scoped_state_dir_once() -> None:
    """`is_enabled()`'s `providers` block (`enablement._providers_block`)
    reads `~/.halo/config.json` via plain `bridge_home()` -- NOT
    scoped by any `state_dir=` a test passes elsewhere -- so a test file
    that never sets `BRIDGE_TEST_HOME`/`BRIDGE_STATE_DIR` at all would read
    the REAL machine's own config.json for that check. A no-op once either
    var is already set (a file that scopes its own state dir per-test, or
    an earlier-imported module's own module-level default) -- never
    clobbers a more specific scoping scheme already in place."""
    if "BRIDGE_TEST_HOME" in os.environ or "BRIDGE_STATE_DIR" in os.environ:
        return
    os.environ["BRIDGE_TEST_HOME"] = str(Path(tempfile.mkdtemp(prefix="halo-scratch-")))
