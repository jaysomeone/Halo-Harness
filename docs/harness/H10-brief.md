# H10 brief — telemetry (`stats --models`) + human-gated `/improve` (halo)

Approved by rolo 2026-09-24 after `reports/Self improving agent harnesses.md` ("Let's add the first
2 bullet points"): **L0 telemetry from the session logs** and **a human-gated `/improve` command**
(L1 memory/rules + L3 skills). NOT approved, do not build: settings proposals (L2), prompt
optimisation (L4), code self-edits (L5), any automatic promotion.

Repo: `~\Documents\vibes\appDev\halo\` (Windows build host; **Kali Linux is the
primary platform**). Baseline = the H9 commit (`v0.3.0`) on master, all suites green on Windows and
WSL. You are the only worker on the tree. Do not commit (Fable verifies and commits).

## Read first
1. `reports/Self improving agent harnesses.md` → "Rolo-claude's six levels" (L0, L1, L3 rows only),
   the guardrail paragraph, "A four-phase path" (Phase 1 only), and "what this plan avoids".
2. `research_notes/Self improving agent harnesses/rsi_in_shipped_harnesses.md` → §1 (Claude Code's
   auto-memory contract: typed topic files, MEMORY.md 200 lines / 25 KB, `modified` stamps) and the
   Qwen Code section (typed memory + `/learn` + `/curator`).
3. `~/.claude/plans/typed-tickling-squirrel.md` → "Decisions" (**no safety heuristics**: provenance is
   information shown on the card, never a block or a classifier), "Auto mode = uninterrupted +
   steering" (nothing may interrupt a running turn; hints only).
4. Current code: `controller.py` (`compute_session_stats`, `session_stats`), `commands/builtins.py`
   (`_cmd_stats`), `tui/slash.py` (`_handle_stats`), `agent/log.py` (`usage` nodes, node types),
   `agent/derive.py` (non-wire keys stripped), `agent/loop.py` (tool items carry `repaired`;
   tool_result error paths; steer/interrupt/compacted/max_steps nodes), `agent/repair.py`,
   `config/memory.py` (`write`, `update_index`, frontmatter), `config/claude_md.py` (rules
   discovery + post-compaction re-injection), `commands/skills.py`, `config_cli.py`
   (`~/.halo/config.json`), `agent/sessions.py` (index.json), `tui/widgets/cards.py`
   (PermissionCard / PlanCard pattern), `headless.py`, `doctor.py`.

## Part A — Telemetry (L0)
`halo stats [--models] [--tools] [--since 7d|30d|all] [--all-projects] [--session ID]
[--json]` and `/stats --models` in the TUI. Source of truth = the session logs under
`~/.halo/sessions/<slug>/*.jsonl` (+ `subagents/`). **Never a new model-visible field.**
Add non-wire metadata to existing nodes where it is missing, and prove with a test that derived
requests are byte-identical before and after:
- `usage` node: `model`, `route` (or|dbx|ant + host), `provider` (the responding provider:
  OpenRouter's `provider` field on chunks / final object; Databricks endpoint name; `anthropic`),
  `finish_reason`, `latency_ms`, `ttft_ms`, `retries`, `status` (ok|429|5xx|overflow|aborted|
  connect_error).
- assistant tool items: `repaired` (exists) + `repair_kind` (leak_parser|lenient_json|rename|
  args_repair|none) + `promoted_from_leak`.
- `tool_result` nodes: `tool`, `ok`, `error_class` (schema_invalid|not_found|multiple_matches|
  read_before_edit|timeout|denied_by_rule|interrupted|loop_breaker|mcp_error|other), `ms`, `bytes`,
  `spilled`.
- turn-level nodes already logged (steer, interrupt, compacted, max_steps): count them.

`halo_harness/telemetry.py`: `scan(sessions_dir, since, slug|all) -> list[SessionSummary]`;
`aggregate_by_model(...)` → rows per (model, provider): sessions, turns, model calls, tokens
in/out/cached, cost, avg ttft/latency, `finish=length` %, retries/429s, overflows, tool calls,
tool error %, repair-hit % by kind, edit failures (not_found + multiple_matches) %, steers,
interrupts, compactions, loop-breaker trips; `aggregate_by_tool(...)`; top error classes with one
example `session#seq` each. Cache `~/.halo/stats-cache.json` keyed by (path, size, mtime);
a corrupt line is skipped and counted, never a crash. Output: rich table (honour `NO_COLOR`,
`--json` = stable schema). `/stats --models` runs the scan off the UI thread (worker Command, as
U5 did for `list_sessions`); bare `stats` keeps U5's current-session behaviour. `doctor` prints
the sessions count and cache age. POSIX HOME first; no Windows assumptions.

## Part B — `/improve` (L1 memory/rules + L3 skills, human-gated)
Binding principles: the model never edits its own instructions silently; every written artifact
was approved by rolo on a card (or by an explicit headless `--apply`); provenance is shown, never
used to block; nothing drafts or writes automatically in `-p`; nothing interrupts a running turn,
auto mode included — hints only.

B1. **Evidence** (`halo_harness/improve/evidence.py`): from the telemetry scan of the last `--since`
(default 7 d; current project slug unless `--all-projects`) build failure clusters: (a) repeated
tool errors of one `error_class` per tool; (b) repair-layer hits per model; (c) loop-breaker
trips; (d) user corrections = a `user` node in the same turn after a tool error or an assistant
answer whose text starts with a correction cue (`no`, `don't`, `stop`, `wrong`, `instead`,
`actually`, `not that`, `use …`) — one plain constant list, not a classifier; (e) Read ENOENT /
wrong-cwd patterns; (f) the same tool sequence recurring in ≥ 3 sessions (skill candidates). Each
cluster keeps ≤ 6 excerpts of ≤ 600 chars with `session_id#seq` references and a boolean
`from_tool_output` when the excerpt originated in a tool_result (WebFetch/MCP/file) rather than in
the user's own words — shown on the card as "derived from tool output", nothing more.

B2. **Drafting** (`halo_harness/improve/draft.py`): ONE model call (config `improve.model` → else
the small model → else the session model) with a fixed system prompt + the clusters, asking for ≤
`improve.max_candidates` (8) candidates as JSON: `{id, kind: memory|rule|skill, title, target:
{scope: project|user, path}, body, rationale, evidence: ["<sid>#<seq>", …], confidence:
low|med|high}`. Parse with the repair layer's lenient JSON; malformed → one retry quoting the
schema error → else "no candidates". Exact formats:
- `memory` → `<memory_dir>/<name>.md` with Claude Code's frontmatter exactly as `config/memory.py`
  writes it (`name`, `description`, `metadata: {node_type: memory, type: feedback|project|
  reference|user, originSessionId, modified}`) + one MEMORY.md index line; respects
  `autoMemoryEnabled` / `CLAUDE_CODE_DISABLE_AUTO_MEMORY` (disabled → no memory candidates).
- `rule` → `.claude/rules/<name>.md` (project) or `~/.claude/rules/<name>.md` (user), optional
  `paths:` frontmatter. Never edits CLAUDE.md.
- `skill` → `.claude/skills/<name>/SKILL.md` or `~/.claude/skills/<name>/SKILL.md` with `name`,
  `description`, `disable-model-invocation: true` (rolo removes it once he trusts the skill),
  `argument-hint` when it takes arguments; body ≤ 120 lines.
Every artifact ends with a provenance comment:
`<!-- halo improve: created=<iso> sessions=<ids> evidence=<n> model=<ref> from_tool_output=<bool> -->`.
Only NEW files are created. A candidate may target an existing file only if that file carries the
provenance comment (the card then shows a unified diff); user-authored files are never modified —
the candidate gets a new name instead.

B3. **Review UX**: TUI `ImproveCard` per candidate (PermissionCard/PlanCard pattern): kind badge,
target path, rendered body (Markdown) or diff, rationale, evidence list (`session#seq`; `o` opens
the excerpt), provenance line; keys `a` apply, `e` edit in `$VISUAL`/`$EDITOR` via `app.suspend()`
then apply, `s` skip, `d` dismiss forever (sha256 of kind+target+body → `~/.halo/improve/
dismissed.json`), `q` stop reviewing. Apply → atomic write (tmp + `os.replace`), an
`improve_applied` log node `{kind, path, candidate_id, sha256}` in the current session, a toast,
and a fresh instructions/memory-index snapshot appended so the NEXT turn sees it (same mechanism
as post-compaction re-injection). `/improve` runs the scan + draft off the UI thread and shows the
cards between turns; typed while a turn runs → queued until the turn ends.

B4. **Hints (never interrupt)**: when the current session's counters cross `improve.hint_threshold`
(default: ≥ 3 repair hits OR ≥ 2 edit failures OR 1 loop-breaker trip), the status bar shows
"✦ /improve: N candidates" and one `notification{level: info}` event fires per session — counters
only, no model call. Auto mode identical: hint only. `improve.hint: false` disables.

B5. **Headless**: `halo improve [--since 7d] [--all-projects] [--json] [--out FILE]` prints
candidates (text or JSON) and saves them to `~/.halo/improve/<timestamp>.json`;
`halo improve --apply <file>#<id>` (repeatable) writes exactly those candidates. `-p`
sessions never draft or write; `--bare` disables everything.

B6. **Config**: `~/.halo/config.json` → `improve: {enabled: true, hint: true, model: null,
since_days: 7, max_candidates: 8, hint_threshold: {repairs: 3, edit_failures: 2, loop_breaker: 1}}`
settable via `halo config set improve.model or:…`; `doctor` shows it; `/help` lists
`/improve` and `/stats --models`.

## Tests (≥ 45, OS-neutral; fixture logs under `tests/fixtures/telemetry/` with Linux paths)
Telemetry: metadata never changes derived requests (byte-identical); per model/provider/tool
aggregation on fixtures with known counts; corrupt line skipped + counted; cache invalidation on
mtime; `stats --models --json` schema; `/stats --models` off the UI thread (pilot). Improve:
clusters from fixtures (every class); correction cues; excerpt bounds; drafting through the mock
upstream with `ScriptedTurns` (valid JSON; malformed → retry → none); memory frontmatter identical
to fake_home's topic files; rule and skill shapes (`disable-model-invocation: true` present);
provenance comment; user file without marker → new name; marker'd file → diff; dismissed hash
persists; ImproveCard pilot `a`/`e` (editor mocked)/`s`/`d`/`q`; apply appends `improve_applied` and
the next turn's snapshot contains the new rule; hint fires as status only during a running turn
in auto mode (no card, no pause); headless `improve --json` writes nothing and `--apply` writes
exactly one; `-p` never drafts; `~/.claude.json` and settings checksums unchanged;
`.credentials.json` sentinel never opened.

## Acceptance (Fable re-runs)
All suites green on Windows and WSL. `halo stats --models --since 30d` over rolo's real
`~/.halo/sessions` shows the DeepSeek rows with cost, repair and edit-failure columns.
TUI `/improve` on this repo yields ≥ 1 sensible candidate from this week's sessions; `a` writes a
rule with provenance into `.claude/rules/`; `d` on another persists across restarts.
`halo improve --json` writes nothing. `settings.json` / `.claude.json` checksums unchanged.

Report ≤ 60 lines. Rules as in the other briefs: no safety / refusal / "cyber" language, provenance
informs and never blocks, ≤ 250 lines per write, no heredocs with backslashes on Windows, no
commits, never write `~/.claude.json` or `settings.json`.
