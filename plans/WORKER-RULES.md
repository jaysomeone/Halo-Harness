# Standing rules for every Halo-Harness worker (read first, every round)

Repo: <repo> (package
`halo_harness`, command `halo`, state dir `~/.halo`). Kali Linux is the
primary platform; Windows is the build host. The orchestrator (Fable)
verifies, commits and pushes; you never commit or push.

Working rules
- You are the only worker in this tree. Spawn no sub-agents. Do not edit
  anything outside the repo except notes in your own scratch files.
- Every file write is at most 250 lines; split larger changes.
- Use the Write tool for files with backslashes (Bash heredocs corrupt
  them on this host).
- Tests are stdlib-only in the house style (`@test`, `Ctx`, exit-code
  gated runners). EVERY test scopes its home: `BRIDGE_TEST_HOME` and
  `BRIDGE_STATE_DIR` through `tests/helpers/provider_env_defaults.py`
  (`ensure_scoped_state_dir_once`) or the per-file `_hermetic_child_env()`
  pattern. An unscoped first call of `bridge_home()` touches the REAL
  `~/.halo`; the suite's guards fail the run if that happens.
- `_Env` helpers snapshot and restore EVERY provider variable they touch
  (`OPENROUTER_*`, `DATABRICKS_*`, `ANTHROPIC_*`, `BRIDGE_*`, `HALO_*`,
  `ROLO_CLAUDE_*`, `TYPESAFE_*`, `OPENAI_*`); subprocess tests scrub those
  prefixes from the inherited environment.
- Never read `~/.claude/.credentials.json`. Never write `~/.claude.json`
  or `~/.claude/settings.json` (only `mcp add` may). Never copy a provider
  key into a file, a test or your report. The Databricks host in the repo
  is always `your-workspace.cloud.databricks.com`.
- No safety, refusal, "for safety", "not allowed in auto mode" or
  "I can't do that" wording anywhere: prompts, tool descriptions, UI
  strings, docs, tests. Auto mode allows everything except the user's own
  deny/ask rules and hooks. Describe behaviour; never gate it.
- Never kill `claude` processes by name; never purge `~/.cache/uv`.
- The shared file `~/.config/vibes-hacker/env` is never renamed, edited
  or deleted.
- If the brief conflicts with the code, follow the brief and say so in
  the report. If something in the brief is impossible, do everything
  else and report exactly what is left and why.

Verification before hand-back (all three, all green, both guard lines
"ok"):
    export PYTHONPATH=<repo>
    python test_bridge.py
    python <repo>/tests/run_all.py
    python test_tui.py
Then confirm `~/.halo/history.jsonl` size and `~/.halo/sessions` contents
are unchanged from the start of your run (record both at the start).

Hand-back format: RESULT LINES (one per brief item: DONE / FIXED /
PARTIAL with evidence and the pinning test names), FILES TOUCHED
(production, tests, docs), WHAT YOU FOUND (anything beyond the brief, and
anything left open with the reason). No commit, no push.

## Test cadence from 2026-10-04 evening (rolo asked why rounds take so long)

- A worker runs ONLY the test modules it touched or added, plus
  `python test_bridge.py`, before handing back. It does not run
  `tests/run_all.py` or `test_tui.py` unless the orchestrator's brief asks
  for it (release rounds, fix passes before a tag).
- The orchestrator runs the changed modules on Kali and WSL every round,
  the full three-platform suites once per two rounds and always before the
  Opus review and the tag, and a short live check on real hardware EVERY
  round: the live checks are where the defects have shown up.
- One commit per round stays, so a defect the batched suites find later is
  traceable to its round.
- Docs-only research rounds may run in parallel with an implementation
  round (they write one new file under docs/harness/); implementation
  rounds stay one at a time on the tree.
