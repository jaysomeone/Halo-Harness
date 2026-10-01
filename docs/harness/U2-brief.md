# U2 brief — the Textual TUI (halo launches like `claude`)

Repo: `~\Documents\vibes\appDev\halo\` (Windows build host; **Kali Linux primary**
— the TUI must run in a Linux terminal first; Windows Terminal second). Baseline = the H3 commit on
master, both suites green on Windows and WSL. Do not commit.

## Read first
1. `~/.claude/plans/typed-tickling-squirrel.md` → "D-TUI" (framework, modules, layout, widgets,
   keys, print mode, packaging, testing, build order U2–U5) and "D-Contract reconciliation"
   (Controller API + event kinds), "Decisions" (rich TUI from day one; rule writes to
   `.claude/settings.local.json`; no safety heuristics), "Primary use case".
2. `halo_harness/events.py` (the implemented Event/Command contract — the UI consumes exactly this),
   `halo_harness/agent/loop.py` (`Session.run()` command pump, `abort`, permission wait points),
   `halo_harness/headless.py` + `output.py` (the sinks; the TUI is another consumer of the same queue),
   `halo_harness/commands/` + `history.py` + `theme.py` + `testing/fake_controller.py` (U0),
   `halo_harness/permissions.py` (`add_allow_rule`, `suggested_rules`), `cli.py` (bare `halo`
   currently prints "TUI arrives in U2" — replace with the app).
3. `docs/harness/claude-help-2.1.281.txt` (flags the TUI must honour on launch: `--model`,
   `--permission-mode`, `--continue/--resume`, `--add-dir`, `--agent`, `--theme` ours).

## Scope (U2 + U3 + U4 of the plan, in one worker; U5 polish later)
A. **Packaging**: add `textual==8.2.8` + `rich>=14,<16` to `pyproject.toml` dependencies (mcp is
   already there from H3), `requirements.lock` via `uv pip compile`, `uv tool install --editable .`
   on the Windows host producing `%USERPROFILE%\.local\bin\halo.exe`; update
   `~\bin\halo.cmd` to prefer that exe and fall back to `python -m halo_harness`;
   `bin/halo` (POSIX) mirrors it (`~/.local/bin/halo` else `python3 -m halo_harness`);
   for the Kali box document `pip install --user -e .` / `uv tool install`; textual imported only
   inside the TUI entry point.
B. **App** (`halo_harness/tui/app.py`, `tui/styles.tcss`, `tui/keys.py`, `tui/theme.py` using U0's
   theme data): `BridgeApp(App)` with `Transcript(VerticalScroll)`, `CompletionPopup`,
   `PromptInput(TextArea)` auto-growing 1–8 lines, `StatusBar` (model, context bar %, cost, mode
   glyph, cwd + git branch, MCP n/m, spinner + elapsed, "↓ N new"); 30 Hz `_drain` timer pulling
   events with an 8 ms budget and coalescing deltas per block (`tui/events.py::drain_queue`, pure);
   no cross-thread widget access; `ENABLE_COMMAND_PALETTE = False`; no Header/Footer.
C. **Widgets** (`tui/widgets/{transcript,cards,diffview,input,statusbar}.py`): `UserMessage`,
   `AssistantText(Markdown)` streamed via `get_stream()`, `ThinkingBlock` (dim, collapsed "✻
   Thinking… <last line>", Ctrl+O expands), `ToolCard` keyed by `tool_use_id` ("⏺ Bash(git status
   -sb)" via per-tool `summarize_input`, spinner while running, ✓/✗/⊘ glyph, collapsed 3 lines +
   "… +N lines (ctrl+o)", `o` opens a pager), `DiffView` for Edit/Write (unified diff, context 3),
   sub-agent nesting placeholder (H6), `SystemNote`, `FoldedHistory` after 300 widgets.
D. **Inline prompt cards**: `PermissionCard` (1/y once, 2/a session, 3 always → `add_allow_rule`
   into `.claude/settings.local.json` with `suggested_rules[0]`, 4/n/Esc deny + "tell Claude what to
   do differently" input → `permission_reply`), `QuestionCard` for AskUserQuestion (OptionList /
   SelectionList per question, "Other…", tab strip), `PlanCard` (H6 wires plan mode; build the card
   now against the `plan_review` event). Input disabled while a card is pending. Loop side: make the
   interactive `ask` path REALLY wait on the `permission_reply` command (H2b resolves it as a
   non-blocking denial today because no UI existed) — implement the `threading.Event` wait keyed by
   `request_id` in `Session` with the abort Event as the escape.
E. **Dialogs** (`tui/dialogs/*`): ModelPicker (from `halo models` data: ref/context/output/
   price, near-miss correction), SessionPicker (`--resume` list), McpStatus (`/mcp`, `r` reconnect),
   PermissionsDialog (rules by source, add rule), Help, HistorySearch (Ctrl+R).
F. **Keys** (per D-TUI): Enter submit; `\`+Enter / Ctrl+J / Alt+Enter newline; Esc interrupt →
   `controller.interrupt()` (sets the Session abort Event; running Bash killed) / dismiss / deny;
   Ctrl+C interrupt → clear → double-press quit; Ctrl+D quit on empty; Shift+Tab cycles
   default → acceptEdits → plan → auto; Ctrl+L clear view; Ctrl+O verbose; Ctrl+R history; PgUp/PgDn
   scroll; Tab completion; F1 help. Paste ≥ 4 lines → `[Pasted text #n +N lines]` placeholder.
G. **Slash commands in the TUI** through U0's registry (`/model`, `/mcp`, `/cost`, `/compact` (H5),
   `/permissions`, `/clear`, `/help`, `/theme`, custom commands, skills), `/` and `@` completion
   (`os.scandir` walk with prunes, 20k cap), history Up/Down with prefix filter.
H. **Controller** (`halo_harness/controller.py`): the non-blocking UI-thread facade over the
   command queue per D-Contract (`submit`, `interrupt`, `set_permission_mode`, `set_model`,
   `add_permission_rule`, `answer_permission`, `answer_question`, `run_slash`, `list_models`,
   `list_sessions`, `resume`, `mcp_status`, `reconnect_mcp`, `memory_path`, `quit` with a 5 s
   deadline); `Session.run()` on a worker thread; `status` events emitted at start, after each
   `message_end`, on mode/model change. Quit prints the transcript to normal scrollback when
   settings `tui != "fullscreen"`.

## Tests
`test_tui.py` at the repo root (same Ctx/@test runner; `asyncio.run` per case; `FakeController`
scripted events; `app.run_test(size=(100, 40))` pilots): submit/stream, permission card keys incl.
the rule write into a temp `.claude/settings.local.json` with Claude Code's escaping, deny with
message, `/model` picker → `fake.model`, Shift+Tab cycles the status text through the four modes,
Esc during a running turn → `fake.interrupts == 1`, Ctrl+C twice → `return_code == 0` and
`quit_called`, tool card start→result same widget + Ctrl+O expansion, paste placeholder, history Up
with both key forms, AskUser multi-select + Other, plan approval card, folding after 300 widgets,
`drain_queue` coalescing; SVG snapshots (`export_screenshot` normalised, `UPDATE_SNAPSHOTS=1`).
Also a real end-to-end pilot against the mock upstream (not the fake controller): type a prompt,
see streamed text and a Read tool card. Everything OS-neutral; snapshots generated on Linux (WSL).

## Acceptance
Both suites + `test_tui.py` green on Windows and WSL; `halo` (bare, from PATH on Windows;
`python3 -m halo_harness` in WSL) opens the full-screen TUI; a live prompt streams text with a tool
card and a permission card in `default` mode; `/model` picker switches to `or:moonshotai/kimi-k3`
and the next reply comes from it; Esc interrupts a long answer; Ctrl+C twice exits cleanly and
restores the terminal; `--demo --stress 500` stays responsive; proxy still works.
Report ≤ 60 lines with screenshots (SVG paths) for the main screen and each card. Rules as in the
other briefs (≤ 250 lines per write, no heredocs with backslashes, no commits, no safety language).
