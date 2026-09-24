# U5 brief — TUI polish + OpenCode UX adoptions (rolo-claude)

Repo: `C:\Users\user\Documents\vibes\appDev\rolo-claude\` (Windows build host; **Kali Linux primary**
— the TUI must be excellent in xterm/kitty/tmux over SSH). Baseline = the H6 commit on master, all
suites green on Windows and WSL. Do not commit.

## Read first
1. `~/.claude/plans/typed-tickling-squirrel.md` → D-TUI (U5 items: folding, snapshots, sub-agent
   nesting, transcript-on-exit, `statusLine` command), "Auto mode = uninterrupted + steering",
   "Closing milestone H9".
2. `reports/OpenCode harness deep review.md` → the U5 rows of the adopt table + Appendix (keymap
   defaults, palette, `@file#L1-20` mentions, `!` shell prefix, git-shadow `/undo` `/redo`, child-session
   navigation, `system`/ansi theme, attention notifications, card expand key, session rename/fork UX).
3. `docs/harness/review-findings-u2-h3b.md` → the UX items in "H5/H6 must-do" (auto-grow counts
   logical lines not wrapped ones; `list_sessions`/`_git_branch` block the UI thread; OpenAI-dialect
   thinking mounts below the answer) and finding 16 (Ctrl+C copy — verify done).
4. `docs/harness/claude-code-2.1.281-binary-facts.md` §1 (`--theme` does not exist in Claude Code —
   ours is extra), Claude Code's `~/.claude/keybindings.json` format (the `keybindings-help` skill
   describes it: chords, contexts) — adopt THAT file format for remaps, not OpenCode's `tui.json`.
5. Current `rolo_claude/tui/**`, `controller.py`, `test_tui.py`, `docs/harness/tui-snapshots/`.

## Scope
A. **Keymap**: read `~/.claude/keybindings.json` (Claude Code's format: chords like `ctrl+x ctrl+s`,
   contexts) with our defaults as a data table (`tui/keys.py`), a which-key overlay after a chord
   prefix (`ctrl+x`), `/keybindings` to show them; `ctrl+p` command palette (fuzzy over slash commands,
   skills, recent files, sessions); `@file#L10-20` line-range mentions expanded via the Read path;
   `!cmd` prefix runs a shell command inline (through the Bash tool + permissions) and shows the output
   as a tool card; card expand/collapse key (`o`), `Ctrl+O` verbose, `Ctrl+E` open the current file in
   `$VISUAL`/`$EDITOR` via `app.suspend()`.
B. **Git-shadow rewind**: every Write/Edit/Bash-that-changed-files step records a snapshot in a
   shadow git repo under `~/.rolo-claude/sessions/<slug>/<id>/shadow/` (like OpenCode's snapshots and
   Claude Code's `file-history`); `/rewind` (alias `/undo`, `/redo`) lists steps and restores the
   working tree to a step (with a confirmation card); the log gets a `rewind` node.
C. **Sessions UX**: session titles via the small model after the first turn (`/rename` to change),
   `/resume` picker showing title/age/cost/turns, `/fork`, child-session navigation for sub-agents
   (parent ↔ child keys per OpenCode), `/export [--sanitize] [file]` (transcript as markdown/JSONL
   with secrets scrubbed), `/stats` (tokens/cost per model, tool counts).
D. **Rendering**: folding after 300 widgets; SVG snapshot suite kept current; sub-agent nested cards;
   thinking block rendered ABOVE the answer text for OpenAI-dialect reasoning; auto-grow counts wrapped
   rows; `list_sessions`/git branch off the UI thread; transcript-on-exit when `tui != "fullscreen"`;
   `statusLine` command from settings executed like Claude Code (its JSON stdin contract per the docs)
   and shown in the status bar; `system`/`ansi` theme auto-select for terminals without truecolor
   (`COLORTERM`, `TERM` checks) plus `claude-dark/light` and daltonized variants; terminal bell +
   `notify-send`/toast when input is needed and `inputNeededNotifEnabled`.
E. **Linux terminal acceptance**: xterm, kitty, gnome-terminal, tmux (mouse on/off), over SSH;
   clipboard via OSC 52 with `xclip`/`wl-copy` fallback and a doctor line; Shift+drag selection note in
   F1; resize handling; no hangs on `Ctrl+Z`/`fg`.

## Tests
`test_tui.py` additions: keymap parsing from a temp `keybindings.json` incl. chords and which-key;
palette filtering; `@file#L` expansion; `!cmd` card; rewind restores a file; titles/rename/fork/
export/stats through the FakeController and one real-Controller pilot; thinking-above-answer order;
auto-grow with wrapped lines; theme auto-select from env; statusLine command output; snapshots
regenerated on WSL.

## Acceptance
All suites green on Windows and WSL; a live TUI session in WSL under `script` (and, when reachable,
the Kali VM over SSH inside tmux): chord + which-key, palette, `@README.md#L1-5`, `!ls`, an Edit then
`/rewind` restoring the file, `/rename`, `/fork`, `/export --sanitize`, `/stats`; SVGs attached.
Report ≤ 50 lines. Rules as in the other briefs.
