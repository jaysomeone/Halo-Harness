# Live checks (2.0.1 W5/W5b)

Everything else in this tree runs against mocks (`tests/run_all.py`,
`test_tui.py`, `test_bridge.py` -- see `docs/DEVELOPMENT.md`'s "The three
suites"). This is the short list of things that can only be verified
against a REAL upstream, a REAL `claude` binary, or a REAL browser/Node
install, split by who runs them and where. None of this is required for
an ordinary change; it is the release-time and Kali-VM-round checklist.

Never paste a key into a terminal that logs history, a chat, or this
file. Nothing here ever prints one (not even a fingerprint).

## A. On a Databricks work box (owner-run)

1. `halo doctor --work --probe-all --tools` -- the full work matrix: one
   short "pong" per chat-shaped Databricks endpoint on its chosen path,
   plus one real Read tool-call per endpoint (`--tools`). Add `--both` to
   also probe the Claude/GLM/Kimi endpoints through the Anthropic-
   compatible gateway (the `ant:`-shaped path) in the same pass. Honest
   failures (VPN unreachable, an expired token) are expected output, not
   a bug -- read the line, don't just check the exit code.
2. One real passthrough tool call, by hand, on each route this box
   actually has:
   ```sh
   halo -p "Read halo_harness/__init__.py and reply with ONLY the exact \
   __version__ string, quotes included" --model ant:claude-sonnet-4-5 \
   --permission-mode auto
   halo -p "Read halo_harness/__init__.py and reply with ONLY the exact \
   __version__ string, quotes included" --model dbx:databricks-claude-sonnet-4-5 \
   --permission-mode auto
   ```
   Swap the model id for whatever this workspace actually exposes if
   those two guesses don't match it (`halo models`/`halo doctor --work`
   list what is real here). Expect the exact quoted version string back
   -- that's proof a real tool call round-tripped through the route, not
   just that the model answered in prose.
3. `halo stats --models --since 7d` -- the per-family edit-failure/tool-
   error/TTFF signal this box's own real usage feeds into
   `docs/harness/FAMILY-BASELINE-2026-09-29.md`'s next update. Paste the
   table (not raw logs) when handing it over.
4. The same three steps are also exactly what
   `tests/live/test_passthrough.py` automates for steps 1-2's "did a real
   tool call round-trip" question, once `HALO_LIVE=1` and the matching
   credential are both present:
   ```sh
   HALO_LIVE=1 python -m tests.live.test_passthrough
   ```
   It is never imported by `tests/run_all.py` (which only globs
   `tests/test_*.py`, one directory up) and every one of its checks is a
   clean SKIP, never a FAIL, when `HALO_LIVE` isn't set to exactly `"1"`
   or a credential is missing -- see that file's own docstring for the
   env vars (`HALO_LIVE_ANT_MODEL`/`HALO_LIVE_DBX_CLAUDE_MODEL`) if the
   default model-id guesses don't match this workspace.

## B. On the Kali VM (orchestrator-run, part of the three-platform round)

These three already live in the ordinary suites and are SKIPPED cleanly
(never FAILED) wherever their one real prerequisite is missing -- the
Kali VM round is simply where the prerequisite happens to be present, so
they stop being skips and start actually exercising the real path:

- **Real-binary Claude Code interop** (`tests/test_mcp_compat_matrix.py`,
  items 6-8): needs a real `claude` on PATH (`shutil.which("claude")` --
  see that file's own `_require_real_claude`). Skip reason when absent:
  "no `claude` binary on PATH -- items 6/7/8 (real-binary interop)
  skipped".
- **ripgrep backend parity** (`tests/test_tools_glob_grep.py`): needs a
  real `rg` on PATH. Skip reason when absent: "ripgrep (rg) is not
  installed on this host -- only the Python engine is exercised here".
  Install it first if a Kali/WSL worktree venv doesn't already have one
  (`sudo apt-get install -y ripgrep`, or `cargo install ripgrep`) so this
  one actually runs instead of skipping.
- **Playwright screenshot e2e**: unlike the two above, this is NOT an
  always-available-to-skip-into suite test -- `@playwright/mcp` and its
  browser binary are a real (multi-hundred-MB, network-fetching) install,
  so it is a deliberate, manual, once-per-release step rather than
  something `tests/run_all.py` ever tries on its own (a contributor's box
  merely having `npx` -- true on the Windows build host too -- must never
  silently trigger a Chromium download). Run it once per release, in the
  Kali worktree venv:
  ```sh
  npx -y @playwright/mcp@latest --help   # primes the npm/browser cache once
  halo -p "go to https://example.com and tell me the exact page title" \
    --playwright --permission-mode auto
  ```
  Expect "Example Domain" back -- that's a real navigate-and-read round
  trip, not just "the server started". `tests/test_cli_flags.py::
  test_playwright_flag_real_and_prompt_still_runs` and
  `tests/test_chrome_playwright.py` cover the config-building and
  graceful-degradation (no npx/node -> a clear error, never a crash or a
  hang) halves of this on every platform already; this manual step is
  only for the actual screenshot/navigate round trip.

See `docs/DEVELOPMENT.md`'s "Live and real-binary checks" for the one-line
pointer back to this file.
