# H6 brief — sub-agents, plan mode, resume/fork, agents CLI (rolo-claude)

Repo: `~\Documents\vibes\appDev\rolo-claude\` (Windows build host; **Kali Linux primary**
— OS-neutral). Baseline = the H5 commit on master, all suites (+ `test_tui.py`) green on Windows and
WSL. Do not commit.

## Read first
1. `~/.claude/plans/typed-tickling-squirrel.md` → D8 (plan mode, sub-agents, sessions), D-CFG
   "Instructions / memory / skills / commands / agents" (agent discovery order, frontmatter fields,
   model resolution chain `invocation → frontmatter → CLAUDE_CODE_SUBAGENT_MODEL → parent`,
   `permissionMode`, `omitClaudeMd`, `memory:` ignored in v1, built-ins Explore/Plan/general-purpose,
   `Task` alias), finding C "Sub-agents" and "CLI / print", "Decisions" (no safety heuristics; plan
   mode is a permission mode the user chooses).
2. `docs/harness/claude-code-2.1.281-binary-facts.md` §1 (`--agent`, `--agents`, `-c/--continue`,
   `-r/--resume`, `--session-id`, `--fork-session`), `docs/harness/claude-help-2.1.281.txt` (exact
   help wording for those flags and `--worktree`).
3. `reports/DeepSeek and OpenRouter ori harnesses.md` → dsh sub-agent facts (spawn/fork, max 8
   active, depth 1, background completion notices as user-role notes).
4. Current code: `agent/{loop,log,derive,invariants,prompt,catalog}.py`, `config/agents_md.py`
   (exists? else implement per D-CFG), `commands/skills.py` (`context: fork`), `tui/widgets/cards.py`
   (PlanCard, nested cards placeholder), `controller.py`, `hooks.py` (SubagentStart/Stop no-ops),
   `headless.py` (stream-json), `permissions.py`.

## Scope
A. **Agent definitions** (`config/agents_md.py`): managed → `--agents` JSON (or file in `-p`) →
   `.claude/agents/**/*.md` walking up (nearest wins) → `~/.claude/agents/**/*.md` → built-ins
   `general-purpose` (all tools but Agent), `Explore` (Read/Glob/Grep/read-only Bash/WebFetch/
   ToolSearch; skips CLAUDE.md), `Plan` (Explore set + plan-writing prompt); frontmatter `name`,
   `description` (required), `tools` (comma string or list; `Agent(type)` restricts spawning),
   `disallowedTools`, `model` (`sonnet|opus|haiku|inherit|<ref>` via `routes.json` aliases; `haiku` →
   small model), `permissionMode`, `maxTurns`, `skills`, `mcpServers`, `hooks`, `memory`, `background`,
   `omitClaudeMd`, `effort`, `isolation: worktree` (accepted; v1 = same tree), `color`,
   `initialPrompt`; `--agent <name>` runs the whole session as that agent; `@agent-<name>` in a prompt
   forces one; `/agents` lists them.
B. **Agent tool** (`tools/agent.py`, `Task` alias): `Agent(description, prompt, subagent_type?,
   model?, run_in_background?)` → child `Session(parent)` with a fresh log (`<session>/subagents/
   agent-<id>.jsonl` + `.meta.json` like Claude Code), the agent's system prompt (body replaces the
   default; environment + CLAUDE.md unless `omitClaudeMd`/Explore/Plan; no memory index unless
   general-purpose), tool subset (frozen catalog subset — never grows the parent's catalog), model
   override, `permissionMode` override (prompts surfaced to the user with the agent tag; print mode →
   deny), depth limit 1, ≤ 4 concurrent (several Agent calls in one message run in parallel threads),
   `maxTurns`, events tagged `agent_id` + `parent_tool_use_id` (TUI nests cards), SubagentStart/Stop
   hooks, result = final assistant text (≤ 30 000 chars, disk spill) as the tool_result; interrupt
   propagates; background agents complete as a user-role notice in the parent's next step (dsh).
C. **Plan mode**: `EnterPlanMode` (model-initiated → `permission_request` in interactive, allowed in
   `-p`) and `Shift+Tab`/`--permission-mode plan`; plan file `~/.claude/plans/<three-random-words>.md`
   (`plansDirectory` honoured) created per session and the only writable path in plan mode; system
   snapshot "plan mode: research, then ExitPlanMode with the plan"; `ExitPlanMode(plan)` writes the
   file, emits `plan_review`; `plan_reply{approved, feedback, mode_after}` → mode becomes `mode_after`
   (default `acceptEdits`) + tool_result "User approved the plan; implement it", rejected → error
   result with feedback; `-p` auto-approves only with `--permission-mode acceptEdits|bypassPermissions`,
   else the plan text is the result; `useAutoModeDuringPlan` ignored (no classifier).
D. **Sessions**: `--continue` (latest for cwd), `--resume [id|name|transcript.jsonl]` (picker in the
   TUI via `list_sessions`), `--session-id <uuid>` (must be a valid UUID; new or existing),
   `--fork-session` (copy the log under a new id before appending), `replay{messages}` event so the TUI
   re-renders history; `index.json` per slug with first prompt/started/last/turns/cost; `/resume` TUI
   picker; `rolo-claude --resume` with no id opens the picker.
E. **stream-json**: sub-agent events (`subagent_start/stop` with `agent_id`, `parent_tool_use_id`) and
   nested `assistant`/`user` lines carry `parent_tool_use_id` like Claude Code's SDK output.

F. **OpenCode adopt items for H6** (`reports/OpenCode harness deep review.md`): the MAX_STEPS_PROMPT
   text when `maxTurns` is hit (ask the model to summarise state instead of dying silently);
   **resumable sub-agent tasks** — the Agent tool returns `<task_result>` wrapped output plus a
   `task_id`, and a later `Agent(task_id=…, prompt=…)` resumes that child session with its context;
   background sub-agents complete as a user-role notice; `AGENTS.md` fallback when no CLAUDE.md exists
   (already in `config/claude_md.py` — verify); session titles via the small model + `/rename`;
   child sessions navigable in the TUI (parent ↔ child keys); OpenCode's `question` tool shape is
   NOT adopted — use Claude Code's `questions[{question, header, options[{label, description}],
   multiSelect}]` (the U2/H3b review must-do) and support multi-select.

## Tests (≥ 60, OS-neutral)
Agent discovery precedence + frontmatter parsing (all fields, tools string vs list, model chain);
built-ins tool sets; Agent tool e2e via the mock upstream with `ScriptedTurns` (child answers,
parent continues; two parallel agents; depth-1 refusal; maxTurns; background completion notice;
permission prompt surfaced with agent tag; print-mode deny); subagent log files + meta; plan mode
(plan file only writable path; ExitPlanMode → plan_review → approve/reject; `-p` behaviours);
sessions (continue/resume/fork/session-id validation; index.json; replay event); `--agent` and
`@agent-name`; hooks SubagentStart/Stop payloads; TUI pilot: nested cards + PlanCard approve.

## Acceptance
All suites green on Windows and WSL. Live (default model): `python -m rolo_claude -p "use a
sub-agent to count the python files under rolo_claude and report the number" --permission-mode auto
--verbose` → nested agent events and a correct count (compare `find`); `--permission-mode plan -p
"plan how you would add a --version flag to bridge.py"` → a plan file under `~/.claude/plans/` and
the plan text as the result, no edits made; `-r <that session id> -p "now summarise your plan in
one line"` continues the same log; TUI: `Shift+Tab` into plan mode, ask for a plan, PlanCard →
approve → mode switches to acceptEdits; proxy pong; settings.json unchanged.
Report ≤ 60 lines. Rules as in the other briefs.
