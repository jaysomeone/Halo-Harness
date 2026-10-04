"""halo_harness.tui.dialogs.mcp_entry_form -- `/mcp` `e` with no $VISUAL/
$EDITOR set (round4 brief item 1): command/args/env for a stdio server, or
url/headers for an http/sse one, writing back to the SAME scope file
`halo mcp add`/`add-json` already use (`mcp_cli._build_entry`/`_store_
entry` -- no second writer for `.mcp.json`/`~/.claude.json`). Same form
family as `roles_editor.py`/`org_editor.py`: plain `Input`/`TextArea`
fields, `ctrl+s` saves and dismisses `True`, `Escape` cancels (`False`),
never touching the file on cancel.
"""

from __future__ import annotations

from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Static, TextArea


class McpEntryForm(ModalScreen):
    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("ctrl+s", "save", "Save", show=True),
    ]
    DEFAULT_CSS = """
    McpEntryForm { align: center middle; }
    McpEntryForm > Vertical { width: 84%; height: auto; max-height: 90%; border: round $primary;
        background: $surface; padding: 1 2; }
    McpEntryForm TextArea { height: 6; }
    McpEntryForm #mcp-field-args { height: 4; }
    """

    def __init__(self, name: str, cfg, *, cwd) -> None:
        super().__init__()
        self.server_name = name
        self.cfg = cfg
        self.cwd = cwd

    def compose(self):
        with Vertical():
            yield Static(f"Editing {self.server_name} ({self.cfg.scope} scope, {self.cfg.type}) -- "
                          f"^S save, Esc cancel", classes="dialog-title")
            if self.cfg.type == "stdio":
                yield Static("Command")
                yield Input(id="mcp-field-command", value=self.cfg.command or "")
                # 2.0.2 review finding 15: a single-line Input split on
                # whitespace can never represent a spaced argument (e.g.
                # a Windows path like "C:/My Projects/app") unambiguously
                # -- one argument per line instead, same shape env/
                # headers below already use.
                yield Static("Args (one per line)")
                yield TextArea(id="mcp-field-args", text="\n".join(str(a) for a in (self.cfg.args or [])))
                yield Static("Env (KEY=VALUE, one per line)")
                yield TextArea(id="mcp-field-env", text="\n".join(f"{k}={v}" for k, v in (self.cfg.env or {}).items()))
            else:
                yield Static("URL")
                yield Input(id="mcp-field-url", value=self.cfg.url or "")
                yield Static("Headers (Key: Value, one per line)")
                yield TextArea(id="mcp-field-headers",
                                text="\n".join(f"{k}: {v}" for k, v in (self.cfg.headers or {}).items()))
            yield Static("", id="mcp-form-hint")

    def action_cancel(self) -> None:
        self.dismiss(False)

    def action_save(self) -> None:
        # 2.0.2 review finding 15 (major): PATCHES the raw on-disk entry
        # (`_raw_entry_for`) instead of rebuilding one from scratch
        # (`_build_entry`, still used by `halo mcp add`/`add-json`,
        # which genuinely want a fresh entry) -- cwd/timeout/
        # headersHelper/alwaysLoad/mcpLazy/any unknown key now survive a
        # save that never touched them.
        from halo_harness.mcp_cli import _parse_kv_list, _patch_entry, _raw_entry_for, _store_entry
        existing = _raw_entry_for(scope=self.cfg.scope, name=self.server_name, cwd=self.cwd)
        try:
            if self.cfg.type == "stdio":
                command = self.query_one("#mcp-field-command", Input).value.strip()
                args = [l.strip() for l in self.query_one("#mcp-field-args", TextArea).text.splitlines()
                        if l.strip()]
                env_lines = [l for l in self.query_one("#mcp-field-env", TextArea).text.splitlines() if l.strip()]
                entry = _patch_entry(existing, transport="stdio", command_or_url=command, extra_args=args,
                                      env=_parse_kv_list(env_lines, "="), headers={}, oauth=self.cfg.oauth)
            else:
                url = self.query_one("#mcp-field-url", Input).value.strip()
                header_lines = [l for l in self.query_one("#mcp-field-headers", TextArea).text.splitlines()
                                 if l.strip()]
                entry = _patch_entry(existing, transport=self.cfg.type, command_or_url=url, extra_args=[], env={},
                                      headers=_parse_kv_list(header_lines, ":"), oauth=self.cfg.oauth)
            _store_entry(scope=self.cfg.scope, name=self.server_name, entry=entry, cwd=self.cwd)
        except (OSError, ValueError, KeyError) as e:
            self.query_one("#mcp-form-hint", Static).update(f"Could not save: {type(e).__name__}: {e}")
            return
        self.dismiss(True)
