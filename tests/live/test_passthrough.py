"""tests.live.test_passthrough -- W5/W5b coverage: the `ant:` (direct
Anthropic) and Databricks Claude passthrough (`dbx:databricks-claude-*`)
routes are mock-only everywhere else in this tree (test_anthropic_native.py,
test_v2a_anthropic_gateway.py, ...) -- nothing here ever calls a real
upstream in the normal suites. This module is the deliberate exception,
and is deliberately NOT picked up by `tests/run_all.py` (which only globs
`tests/test_*.py` -- this package lives one directory below that, and
nothing imports it automatically).

Run it explicitly, from the repo root, with real credentials already
configured (env file / shell env / settings.json -- same resolution a
normal session uses) and the opt-in flag set:

    HALO_LIVE=1 python -m tests.live.test_passthrough

Every test is a clean SkipTest (never a FAIL) when `HALO_LIVE` isn't
exactly "1" or the matching credential isn't actually present --
`tests/helpers/runner.py`'s own SkipTest/FAIL split applies, so a plain
`python tests/run_all.py` on a box with no live creds configured would
still show these as SKIP if it ever did import this package (it doesn't).
See docs/harness/LIVE-CHECKS.md for the full runbook this belongs to.

Never reads `~/.claude/.credentials.json`; never prints a key (not even a
fingerprint) -- only pass/fail text and the model's own reply.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_DIR))

from tests.helpers.runner import Ctx, SkipTest, new_registry, print_results, run_all
from halo_harness.providers.enablement import credentials_present

test, TESTS = new_registry()

_LIVE_ON = os.environ.get("HALO_LIVE") == "1"
# Sensible guesses only -- the owner's real workspace may expose a
# differently-named row; override with the env var when it does (see
# LIVE-CHECKS.md). Never a model this repo claims is guaranteed to exist.
_ANT_MODEL = os.environ.get("HALO_LIVE_ANT_MODEL", "ant:claude-sonnet-4-5")
_DBX_CLAUDE_MODEL = os.environ.get("HALO_LIVE_DBX_CLAUDE_MODEL", "dbx:databricks-claude-sonnet-4-5")
_PROMPT = ("Read the file halo_harness/__init__.py with the Read tool and reply with ONLY the exact "
           "string assigned to __version__ there, quotes included, nothing else.")


def _require_live() -> None:
    if not _LIVE_ON:
        raise SkipTest("HALO_LIVE != 1 -- this module never runs unless explicitly opted into")


def _run_live(model: str, *, timeout: float = 60.0) -> subprocess.CompletedProcess:
    """A real `halo -p` subprocess against a REAL upstream -- no mock, no
    BRIDGE_TEST_HOME override (this intentionally resolves credentials
    exactly like the owner's own real shell would), `--permission-mode
    auto` since nothing is here to answer a card. `--cwd` pins the repo
    root so the Read tool call above has something real to read."""
    args = [sys.executable, "-m", "halo_harness", "-p", _PROMPT, "--model", model,
            "--permission-mode", "auto", "--cwd", str(REPO_DIR)]
    return subprocess.run(args, cwd=str(REPO_DIR), capture_output=True, text=True, timeout=timeout)


@test
def test_ant_route_one_turn_with_a_real_tool_call(ctx: Ctx):
    _require_live()
    if not credentials_present("anthropic"):
        raise SkipTest("no anthropic (ant:) credentials detected -- set ANTHROPIC_API_KEY or "
                        "a settings.json env block before running this live")
    result = _run_live(_ANT_MODEL)
    ctx.check(f"exit 0, got {result.returncode} stderr={result.stderr[-600:]!r}", result.returncode == 0)
    ctx.check("no traceback in stderr", "Traceback" not in result.stderr)
    ctx.check(f"the version string came back (a real tool call ran), got stdout={result.stdout!r}",
              bool(result.stdout.strip()))


@test
def test_databricks_claude_passthrough_one_turn_with_a_real_tool_call(ctx: Ctx):
    _require_live()
    if not credentials_present("databricks"):
        raise SkipTest("no Databricks credentials detected -- set DATABRICKS_TOKEN/DATABRICKS_HOST (or "
                        "a databrickscfg profile) before running this live")
    result = _run_live(_DBX_CLAUDE_MODEL)
    ctx.check(f"exit 0, got {result.returncode} stderr={result.stderr[-600:]!r}", result.returncode == 0)
    ctx.check("no traceback in stderr", "Traceback" not in result.stderr)
    ctx.check(f"the version string came back (a real tool call ran), got stdout={result.stdout!r}",
              bool(result.stdout.strip()))


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped, label="live checks"))
