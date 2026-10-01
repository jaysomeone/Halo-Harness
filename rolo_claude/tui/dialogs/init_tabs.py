"""rolo_claude.tui.dialogs.init_tabs -- H15 Part A: `rolo-claude init`'s
tabbed provider setup, one tab per `init_providers.TAB_PROVIDERS` entry
(Databricks, OpenRouter, Anthropic API (key), Claude Code subscription,
TypeSafe). A standalone Textual `App` (same reason `init_picker.py`'s own
pickers are standalone: a plain CLI command has no host `BridgeApp` screen
stack to push onto) -- `init_cli.py` runs this synchronously via
`run_init_tabs`, on a real terminal only; a piped/non-tty run never reaches
this module (see `init_cli.py::cmd_init`'s own TTY gate).

Navigation: Shift+Tab always switches tabs (app-level, priority); Left/
Right switch tabs too whenever the tab bar itself has focus (Textual's own
`Tabs` widget already binds them there) -- while a credential field has
focus, Left/Right move the cursor instead (`Input`'s own binding correctly
wins, matching how every real wizard behaves: you don't lose cursor
movement just because tabs exist). Up/Down move between the active tab's
own fields/button (app-level, non-priority -- `Input` binds neither key,
so these never fight it). Enter activates a focused field (saves) or
button. Esc leaves the whole dialog (`.run()` returns) -- `init_cli.py`
proceeds to the default-model/permission-mode steps exactly as it does
after the old one-at-a-time picker loop.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Button, Input, Static, TabbedContent, TabPane

from rolo_claude.init_providers import TAB_LABEL, TAB_PROVIDERS, tab_credential_state


def _pane_id(provider: str) -> str:
    return f"pane-{provider}"


def _status_text(provider: str, *, state: Optional[dict] = None) -> str:
    """A.2: "picked up from <source>" + masked value once configured; a
    discovered Databricks HOST with no token yet gets its own in-between
    wording (A.1's Databricks-specific "host and token" fields)."""
    state = state if state is not None else tab_credential_state(provider)
    if state["configured"]:
        return f"picked up from {state['source']}: {state['masked']}"
    if state.get("known_host"):
        return f"host picked up from {state['source']}: {state['known_host']} -- token still needed"
    return "not set up"


def _reach_text(provider: str) -> str:
    from rolo_claude.providers.reachability import reachability_tag
    return f"reachability: {reachability_tag(provider)}"


class InitTabsApp(App):
    TITLE = "rolo-claude init"
    BINDINGS = [
        Binding("shift+tab", "prev_tab", "Previous provider", priority=True, show=False),
        Binding("up", "focus_prev_field", "Up", show=False),
        Binding("down", "focus_next_field", "Down", show=False),
        Binding("escape", "finish", "Done", priority=True, show=False),
    ]
    CSS = """
    Screen { align: center middle; }
    #tabs-body { width: 92%; height: 88%; border: round $primary; padding: 1 2; }
    .tab-status { margin-top: 1; color: $text-muted; }
    .tab-reach { margin-top: 1; }
    .tab-help { margin-top: 1; color: $text-muted; }
    Input { margin-top: 1; }
    Button { margin-top: 1; }
    """

    def __init__(self, *, team: "Optional[str]" = None, no_live: bool = False) -> None:
        super().__init__()
        # Read by init_cli.py AFTER .run() returns.
        self.configured_this_run: "list[str]" = []
        self.catalog_notes: "dict[str, str]" = {}
        self.written: "list[str]" = []
        # 1.0.1 part 2 fixpass finding 6: --team/--no-live, silently ignored
        # by this app before this fix.
        self._no_live = no_live
        self._team_flag = team
        self.team_warnings: "list[str]" = []
        # 1.0.1 part 2 fixpass finding 4: each tab's credential state is
        # computed ONCE (synchronously here for the four purely-local
        # providers, which never spawn/touch the network; in a worker for
        # "claude", which shells out to `claude auth status`) and CACHED --
        # a keystroke (`on_input_changed`) only ever reads this cache, it
        # never re-derives the state itself. `team_cfg` is loaded eagerly
        # too, but ONLY from a local file (`team_config.team_config_path`
        # is pure/fast); a `--team https://...` URL is resolved in a worker
        # instead (see `_team_cfg_worker`) -- a real network fetch must
        # never run inline in `__init__`/`compose`.
        self._team_cfg, self._team_cfg_pending = self._load_team_cfg_fast()
        self._state_cache: "dict[str, dict]" = {
            p: tab_credential_state(p, team_cfg=self._team_cfg) for p in TAB_PROVIDERS if p != "claude"
        }
        # A real placeholder (never an absent key) -- `_claude_state_worker`
        # overwrites it once resolved; keeping the key present from the
        # start means `_collect_values`/`on_input_changed` never have a
        # reason to fall back to a fresh (spawning, UI-thread-blocking)
        # `tab_credential_state("claude")` call of their own if "Check
        # login" is somehow clicked before that worker finishes.
        self._state_cache["claude"] = {"configured": False, "source": None, "masked": None,
                                        "known_host": None, "fields": []}

    def _load_team_cfg_fast(self) -> "tuple[Optional[dict], bool]":
        from rolo_claude.team_config import load_team_config, team_config_path
        try:
            source = team_config_path(Path.cwd(), team_flag=self._team_flag)
        except Exception:
            return None, False
        if source and (source.startswith("http://") or source.startswith("https://")):
            return None, True  # resolved in _team_cfg_worker instead
        cfg, warnings = load_team_config(Path.cwd(), team_flag=self._team_flag)
        self.team_warnings.extend(warnings)
        return cfg, False

    def compose(self) -> ComposeResult:
        with Vertical(id="tabs-body"):
            yield Static("Set up providers below -- Esc when done (default model/permission mode come next)",
                         classes="dialog-title")
            with TabbedContent(initial=_pane_id(TAB_PROVIDERS[0])):
                for provider in TAB_PROVIDERS:
                    with TabPane(TAB_LABEL[provider], id=_pane_id(provider)):
                        yield from self._tab_widgets(provider)

    def _tab_widgets(self, provider: str) -> ComposeResult:
        # finding 4: "claude" is the one provider whose state requires a
        # `claude auth status` SPAWN (`claude_login_available()`) -- never
        # computed here (compose() must return instantly); a placeholder
        # until the on_mount worker below resolves it for real. The other
        # four are purely local (env vars/settings files, no subprocess/
        # network) and were already computed once in `__init__`.
        state = self._state_cache[provider]
        status_text = "checking…" if provider == "claude" else _status_text(provider, state=state)
        yield Static(status_text, id=f"{provider}-status", classes="tab-status")
        for f in state["fields"]:
            prefill = state.get("known_host") or "" if f["name"] == "host" else ""
            yield Input(value=prefill, placeholder=f["label"], password=f["secret"],
                        id=f"{provider}-field-{f['name']}")
        if provider == "claude":
            yield Static("Uses your existing `claude` login as-is -- nothing is stored here.",
                         classes="tab-help")
        if provider == "typesafe":
            yield Static("Stores TYPESAFE_API_KEY only -- for a later feature, no routed models yet.",
                         classes="tab-help")
        label = "Check login" if provider == "claude" else "Save"
        yield Button(label, id=f"{provider}-save", variant="primary")
        # finding 4: NEVER a real probe here (compose() must return
        # instantly) -- always "checking…" to start; on_mount's own workers
        # below resolve the real tag, or --no-live skips it outright.
        reach = "reachability: skipped (--no-live)" if self._no_live else "reachability: checking…"
        yield Static(reach, id=f"{provider}-reach", classes="tab-reach")

    def on_mount(self) -> None:
        if self._team_cfg_pending:
            self.run_worker(self._team_cfg_worker, thread=True, name="init-team-config")
        # finding 4: the "claude" tab's own (spawning) state, resolved once
        # off the UI thread regardless of --no-live (a local subprocess
        # check, not a network "live" probe/catalog fetch).
        self.run_worker(self._claude_state_worker, thread=True, name="init-tab-claude-state")
        if self._no_live:
            return  # finding 6: --no-live skips every reachability probe/catalog fetch below
        # A.3: the bounded background probe runs right away for whatever's
        # ALREADY configured (re-running init on a partially-set-up box) --
        # never for a tab that still needs credentials, which starts "not
        # set up" until Save. Read from the cache computed in __init__ --
        # never re-derives it (finding 4).
        for provider in TAB_PROVIDERS:
            if provider != "claude" and self._state_cache[provider]["configured"]:
                self.run_worker(lambda p=provider: self._probe_worker(p), thread=True, name=f"init-tab-probe-{provider}")

    def _team_cfg_worker(self) -> None:
        from rolo_claude.team_config import load_team_config
        cfg, warnings = load_team_config(Path.cwd(), team_flag=self._team_flag)
        self.call_from_thread(self._apply_team_cfg, cfg, warnings)

    def _apply_team_cfg(self, cfg: "Optional[dict]", warnings: list) -> None:
        self._team_cfg = cfg
        self.team_warnings.extend(warnings)
        if not cfg or "databricks" in self.configured_this_run:
            return
        state = tab_credential_state("databricks", team_cfg=self._team_cfg)
        self._state_cache["databricks"] = state
        if state["configured"] or not state.get("known_host"):
            return
        try:
            self.query_one("#databricks-status", Static).update(_status_text("databricks", state=state))
            host_input = self.query_one("#databricks-field-host", Input)
            if not host_input.value:
                host_input.value = state["known_host"]
        except Exception:
            pass

    def _claude_state_worker(self) -> None:
        state = tab_credential_state("claude", team_cfg=self._team_cfg)
        # `detected=state["configured"]` -- reuses the ALREADY-known answer
        # (this same worker's own `claude_login_available()` call just
        # above) instead of `reachability_tag` independently re-deriving it
        # via a SECOND `claude auth status` spawn.
        from rolo_claude.providers.reachability import reachability_tag
        reach_text = f"reachability: {reachability_tag('claude', detected=state['configured'])}"
        self.call_from_thread(self._apply_claude_state, state, reach_text)

    def _apply_claude_state(self, state: dict, reach_text: str) -> None:
        self._state_cache["claude"] = state
        try:
            self.query_one("#claude-status", Static).update(_status_text("claude", state=state))
        except Exception:
            pass
        if not self._no_live:
            try:
                self.query_one("#claude-reach", Static).update(reach_text)
            except Exception:
                pass

    # ---- navigation --------------------------------------------------

    def action_prev_tab(self) -> None:
        tabs = self.query_one(TabbedContent)
        tab_ids = [_pane_id(p) for p in TAB_PROVIDERS]
        try:
            idx = tab_ids.index(tabs.active)
        except ValueError:
            idx = 0
        tabs.active = tab_ids[(idx - 1) % len(tab_ids)]

    def action_focus_prev_field(self) -> None:
        self.screen.focus_previous()

    def action_focus_next_field(self) -> None:
        self.screen.focus_next()

    def action_finish(self) -> None:
        self.exit()

    # ---- live status as a value is entered (A.2) ----------------------

    def on_input_changed(self, event: Input.Changed) -> None:
        field_id = event.input.id or ""
        if "-field-" not in field_id:
            return
        provider = field_id.split("-field-", 1)[0]
        # finding 4: reads the CACHE -- a field only exists on a tab that
        # was already computed synchronously in __init__ (never "claude",
        # which has no fields at all), so this never re-derives/re-spawns.
        state = self._state_cache[provider]
        if state["configured"]:
            return  # already resolved elsewhere -- editing here only matters once Saved
        try:
            status = self.query_one(f"#{provider}-status", Static)
        except Exception:
            return
        any_value = any((self._field_value(provider, f["name"]) or "").strip() for f in state["fields"])
        status.update("value entered -- Enter (or Save) stores it" if any_value
                      else _status_text(provider, state=state))

    def _field_value(self, provider: str, name: str) -> str:
        try:
            return self.query_one(f"#{provider}-field-{name}", Input).value
        except Exception:
            return ""

    # ---- save ----------------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id.endswith("-save"):
            self._save(button_id[: -len("-save")])

    def on_input_submitted(self, event: Input.Submitted) -> None:
        field_id = event.input.id or ""
        if "-field-" in field_id:
            self._save(field_id.split("-field-", 1)[0])

    def _collect_values(self, provider: str) -> dict:
        state = self._state_cache[provider]
        return {f["name"]: self._field_value(provider, f["name"]) for f in state["fields"]}

    def _save(self, provider: str) -> None:
        if provider == "claude":
            # M3 (1.0.1 final pass): "Check login" used to call
            # save_tab_credentials()+tab_credential_state() right here, on
            # the UI thread -- each spawns `claude auth status`, so a slow
            # subprocess froze the whole app for its duration (twice over).
            # Moved to a thread=True worker + call_from_thread, same pattern
            # _claude_state_worker/_apply_claude_state already use for this
            # tab's own on_mount check.
            self.run_worker(self._save_claude_worker, thread=True, name="init-tab-save-claude")
            return
        from rolo_claude.init_providers import save_tab_credentials
        values = self._collect_values(provider)
        ok, message = save_tab_credentials(provider, values, team_cfg=self._team_cfg)
        try:
            status = self.query_one(f"#{provider}-status", Static)
        except Exception:
            return
        if not ok:
            status.update(f"not set up ({message})")
            return
        # 1.0.1 part 2 fixpass finding 15: `enable_if_was_explicitly_
        # disabled`, never a bare `enable()` -- see its own docstring; item
        # 21.1 ("completing a tab is what enables it") is already covered
        # live by auto-detection, so this only ever flips an EXISTING
        # explicit `enabled: false` back to `true`, never writes a fresh
        # permanent override that would outlive a later revocation.
        from rolo_claude.providers.enablement import enable_if_was_explicitly_disabled
        enable_if_was_explicitly_disabled(provider)
        if provider not in self.configured_this_run:
            self.configured_this_run.append(provider)
        # finding 4: refreshes the cache -- the save just changed the
        # underlying credential/login state this provider's cached entry
        # was computed from.
        state = tab_credential_state(provider, team_cfg=self._team_cfg)
        self._state_cache[provider] = state
        status.update(_status_text(provider, state=state))
        if self._no_live:
            # finding 6: --no-live skips the reachability probe AND the
            # catalog fetch below -- "Check login"/"Save" still works and
            # enables the tab, it just never touches the network.
            try:
                self.query_one(f"#{provider}-reach", Static).update("reachability: skipped (--no-live)")
            except Exception:
                pass
            return
        try:
            self.query_one(f"#{provider}-reach", Static).update("reachability: checking…")
        except Exception:
            pass
        self.run_worker(lambda: self._finish_tab_worker(provider), thread=True, name=f"init-tab-save-{provider}")

    def _save_claude_worker(self) -> None:
        """M3 (1.0.1 final pass): the "claude" tab's own "Check login"
        button, off the UI thread -- `_collect_values("claude")` always
        returns `{}` (this tab has no input fields at all), so there is
        nothing to read off a widget here; both calls below spawn `claude
        auth status`, exactly like the synchronous code this replaces did."""
        from rolo_claude.init_providers import save_tab_credentials, tab_credential_state
        ok, message = save_tab_credentials("claude", {}, team_cfg=self._team_cfg)
        state = tab_credential_state("claude", team_cfg=self._team_cfg) if ok else None
        self.call_from_thread(self._apply_claude_save, ok, message, state)

    def _apply_claude_save(self, ok: bool, message: str, state: "Optional[dict]") -> None:
        try:
            status = self.query_one("#claude-status", Static)
        except Exception:
            return
        if not ok:
            status.update(f"not set up ({message})")
            return
        from rolo_claude.providers.enablement import enable_if_was_explicitly_disabled
        enable_if_was_explicitly_disabled("claude")
        if "claude" not in self.configured_this_run:
            self.configured_this_run.append("claude")
        self._state_cache["claude"] = state
        status.update(_status_text("claude", state=state))
        if self._no_live:
            try:
                self.query_one("#claude-reach", Static).update("reachability: skipped (--no-live)")
            except Exception:
                pass
            return
        try:
            self.query_one("#claude-reach", Static).update("reachability: checking…")
        except Exception:
            pass
        self.run_worker(lambda: self._finish_tab_worker("claude"), thread=True, name="init-tab-save-claude-finish")

    def _probe_worker(self, provider: str) -> None:
        from rolo_claude.providers.reachability import reachability_tag
        tag = reachability_tag(provider)
        self.call_from_thread(self._apply_reach, provider, tag)

    def _finish_tab_worker(self, provider: str) -> None:
        """A.4: a tab that now has credentials gets its catalog fetched
        and cached right away, off the UI thread -- the reachability tag
        updates from the SAME probe pass."""
        from rolo_claude.init_providers import refresh_tab_catalog
        from rolo_claude.providers.reachability import reachability_tag
        tag = reachability_tag(provider)
        _ok, note = refresh_tab_catalog(provider)
        if note:
            self.catalog_notes[provider] = note
        self.call_from_thread(self._apply_reach, provider, tag)

    def _apply_reach(self, provider: str, tag: str) -> None:
        try:
            self.query_one(f"#{provider}-reach", Static).update(f"reachability: {tag}")
        except Exception:
            pass


def run_init_tabs(*, team: Optional[str] = None, no_live: bool = False) -> InitTabsApp:
    """Runs the tabbed app as a standalone full-screen app; the caller
    reads `.configured_this_run`/`.catalog_notes`/`.written`/`.team_warnings`
    back off the returned instance once `.run()` has returned (Esc, or any
    other exit). `team`/`no_live` (1.0.1 part 2 fixpass finding 6): the
    same `--team`/`--no-live` flags `init_cli.py`'s own sequential path
    already honors."""
    app = InitTabsApp(team=team, no_live=no_live)
    app.run()
    return app
