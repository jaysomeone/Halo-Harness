# H9b brief — fix pass for the whole-tree review, then v0.3.0 (rolo-claude)

Repo: `C:\Users\user\Documents\vibes\appDev\rolo-claude\` (Windows build host; **Kali Linux is the
primary platform**). Baseline = the H9 commit on master, all three suites green on Windows and WSL.
You are the ONLY worker on the tree. Do not commit, push or tag (Fable verifies, commits and tags).

## Read first
1. `docs/harness/review-findings-h9-tree.md` — the spec: 34 findings (2 critical, 13 major, 19
   minor) + "No findings in" + the "H9b must-do" list. Line numbers refer to the `d653a78`
   snapshot; H9 changed files since — re-locate by symbol.
2. The H9 worker's report items handed to you in the spawn prompt (anything H9 could not finish).
3. `~/.claude/plans/typed-tickling-squirrel.md` → "Decisions" (no safety heuristics), "Auto mode =
   uninterrupted + steering", "Primary use case" (Kali), "Closing milestone H9" (exit criteria:
   zero known critical/major findings open; suites green on Linux + Windows; acceptance table
   complete; README/INSTALL current; tag v0.3.0).
4. `docs/harness/ACCEPTANCE-2026-09-25.md` (H9's table) — keep it current when your fixes change
   observed output; `CHANGELOG.md` — add an entry per finding closed.

## Scope — every finding closed with a pinning test (`test_h9b_f<NN>_…`, real Session where the
finding involves the loop)
- **Criticals first.** F1: tool ids from the upstream that are empty or `call_…` must be replaced
  by ids unique across the WHOLE session (never a per-call counter restarting at 0; Kimi's own
  `functions.<name>:<idx>` ids are kept verbatim and namespaced internally per assistant message
  wherever the harness keys anything by id: prune stub set, spill filenames, pairing check, TUI
  cards, permission waiters). Test: Kimi profile with empty upstream ids through six Reads past the
  prune window — every fresh result reaches the model in full. F2: permission waiters keyed by
  (agent_id, tool_id), never by bare tool id; two parallel children both opening with
  `functions.Bash:0` get distinct, correctly routed cards; approving one never runs the other.
- **Background-job contract as one unit** (findings 3, 6–10, 30): jobs die with the process on
  Linux (own process group under the harness's session, killed on quit, Esc-to-quit, SIGTERM AND
  SIGHUP; sub-agent job registries owned by the parent and killed on quit); BashOutput works past
  300k chars (paged/spilled with the saved-file hint); the completion notice carries a bounded
  summary + the spill path, never the whole output; the loop breaker treats BashOutput/TaskStop
  polling as non-identical (or exempt) so polling never trips 5/8; `-p --output-format json`
  prints exactly ONE JSON object and exits non-zero after a failed turn; Esc while idle never
  kills background sub-agents and never reports them "finished". Verify on WSL with `pgrep` after a
  normal `-p` exit with a sub-agent job and after SIGHUP.
- **Secrets** (findings 4, 5): ONE sanitizer used by `export --sanitize`, `/export --sanitize` and
  stats; it must scrub `sk-or-v1-…`, `sk-ant-…`, `dapi…`, `ghp_…`, quoted `export KEY="…"` lines,
  settings `env` blocks, JSON keys, Bearer headers; delete the dead `session_cli.py` sanitizer and
  point its tests at the wired one; `CLAUDE_ENV_FILE` is sourced with `tool_child_env()` (provider
  keys stripped), test: a hook-written env-file line that prints `$OPENROUTER_API_KEY` sees nothing.
- **Linux PATH** (finding 12): settings `env.PATH` and env-file additions survive Debian/Kali
  `/etc/profile` under `bash -lc` (apply the session env AFTER the login profile, e.g. run the
  profile first then re-export the harness env inside the same shell); test with the bind-mounted
  Debian profile via `unshare -rm` on WSL (no sudo), and on the Kali VM if reachable.
- **TUI** (findings on child event mixing, status bar, `resources/list`): parallel children get
  separate nested streams; the parent's status bar never shows a child's cost/model; children never
  fire the parent's `on_turn_done`; `resources/list` for `@server:` completion runs off the UI
  thread with a short timeout and a cache — a hung server never blocks prompt submission.
- **Cost**: sub-agent usage rolls up into the parent's cost meter, `--max-budget-usd` and `stats`.
- **NotebookEdit**: pre-4.5 notebooks (no cell ids → positional addressing like Claude Code
  2.1.282), outputs cleared on replace.
- **All remaining majors and minors** in the findings file (SessionStart hooks not re-fired per
  sub-agent, Read continuation hint math, offline Databricks default profiles, INSTALL recipes that
  fail as written, README claims), plus every unresolved item from the H9 report.
- **Release prep**: `__version__` 0.3.0 confirmed, CHANGELOG entries for every finding, README and
  INSTALL re-checked line by line against `--help` and the code, `docs/harness/ACCEPTANCE-2026-09-25.md`
  updated with the re-run lines your fixes touch.

## Tests
Suites exit 0 on Windows AND WSL (`export PYTHONPATH="C:/Users/user/Documents/vibes/appDev/rolo-claude"`;
WSL recipe in `docs/harness/INSTALL.md`, venv `~/rolo-claude-wt-venv`). Linux failures fixed for
real, never skipped. `python -X dev -W error::ResourceWarning tests/run_all.py` stays clean.

## Acceptance (Fable re-runs)
All suites green both platforms; live DeepSeek V4.1 Flash: pong; a 600-line Read answered from ONE
full Read; two parallel sub-agents with correct counts and separate TUI streams (pilot); background
`sleep 20` then README line count then the completion notice, and `pgrep sleep` empty after exit
on WSL; `export --sanitize` on a session containing a fake `sk-or-v1-` key shows none of it; proxy
pong; `~/.claude/settings.json` and `~/.claude.json` byte-identical before/after.

Report ≤ 60 lines: per finding "closed + test name" or "NOT closed + why". Rules as in the other
briefs (≤ 250 lines per write, no heredocs with backslashes on Windows, no commits/tags, no safety /
refusal / "cyber" language, never write `~/.claude.json` or `settings.json`).
