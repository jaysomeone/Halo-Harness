"""tests.helpers.fake_home -- builds a temp home directory mirroring rolo's
real ~/.claude layout (plan D-CFG "Fixtures"), for BRIDGE_TEST_HOME.

Both settings.json and settings.local.json are ALWAYS the synthetic
stand-ins below, NEVER a copy of whatever's on the machine running the
suite (finding 14/16: a prior version copied the live settings.json/
settings.local.json when present, so the suite's actual behavior -- model
name, permission mode, the count and shape of settings.local.json's rules
-- silently depended on whichever machine happened to run it, passing only
on rolo's own box and reading a different rule count on any other, incl.
the Kali VM this suite ultimately runs on).
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Optional

# Fallback (used only if the real files aren't present on this machine) --
# the SAME 19-rule shape/escaping as rolo's real settings.local.json, a
# smaller stand-in with the same escaping patterns finding B calls out
# (doubled backslashes, escaped parens inside Tool(...)).
_FALLBACK_SETTINGS_JSON = {
    "permissions": {"defaultMode": "auto"},
    "model": "claude-fable-5-1[1m]",
    "modelSettings": {"claude-fable-5-1": {"effortLevel": "high"}},
    "tui": "fullscreen",
    "theme": "dark",
    "autoMode": {"environment": ["### Org-wide", "**Organization**: None configured"]},
}

# A FIXED 19-rule snapshot exhibiting the same escaping shapes as rolo's
# real settings.local.json (escaped parens `\(`/`\)`, doubled backslashes in
# a Windows path, an admin/elevation-style GetCurrent() one-liner) WITHOUT
# reproducing his actual personal script names/paths -- synthetic, not a
# live copy, so the round-trip test is reproducible on any machine, incl.
# the Kali VM this suite ultimately runs on (finding 14).
_SETTINGS_LOCAL_JSON = {
    "permissions": {
        "allow": [
            "Bash(netstat -ano)",
            "Bash(ipconfig)",
            "PowerShell(Get-NetConnectionProfile)",
            r'PowerShell($id=[Security.Principal.WindowsIdentity]::GetCurrent\(\); $p=New-Object Security.Principal.WindowsPrincipal\($id\); "Elevated \(admin\): " + $p.IsInRole\([Security.Principal.WindowsBuiltinRole]::Administrator\))',
            r"PowerShell(Start-Process powershell.exe -Verb RunAs -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-WindowStyle','Hidden','-File','C:\\Users\\example\\fixture-task.ps1'; \"Elevated process launched \(accept the UAC prompt if shown\).\")",
            r'PowerShell(Start-Sleep -Seconds 2; "Now: " + \(Get-Date -Format \'HH:mm:ss\'\); Get-Process -Id 1111,2222 -ErrorAction SilentlyContinue | Select-Object Id,ProcessName)',
            r"PowerShell($out='C:\\Users\\example\\downloaded-tool.exe'; try { Invoke-WebRequest -Uri 'https://example.invalid/tool.exe' -OutFile $out -UseBasicParsing -TimeoutSec 120; $f=Get-Item $out; \"Downloaded: {0}  Size: {1:N0} bytes\" -f $f.FullName, $f.Length } catch { \"DOWNLOAD FAILED: \" + $_.Exception.Message })",
            r"Bash(powershell.exe -NoProfile -ExecutionPolicy Bypass -File 'C:\\Users\\example\\dupe-scan.ps1')",
            r'PowerShell($f=\'C:\\Users\\example\\.claude\\projects\\fixture-slug\\session.jsonl\'; "{0} bytes" -f \(Get-Item $f\).Length; "{0} lines" -f \(Get-Content $f\).Count)',
            r"Bash(powershell -NoProfile -ExecutionPolicy Bypass -File C:\\Users\\example\\scan-task.ps1)",
            r"PowerShell(Start-Process -FilePath \"powershell.exe\" -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','C:\\Users\\example\\scan-task.ps1' -WindowStyle Hidden -PassThru | Select-Object Id,ProcessName)",
            r"PowerShell(Get-WinEvent -FilterHashtable @{LogName='System'; Id=41} -MaxEvents 10 -ErrorAction SilentlyContinue | Select-Object TimeCreated, Id)",
            r'PowerShell($os=Get-CimInstance Win32_OperatingSystem; "LastBoot: "+$os.LastBootUpTime; "Now:      "+\(Get-Date\); "RAM GB: "+[math]::Round\($os.TotalVisibleMemorySize/1MB,1\))',
            "WebFetch(domain:raw.githubusercontent.com)",
            "Bash(where git *)",
            "Bash(git --version)",
            "Bash(where python *)",
            "Bash(where uv *)",
            "Bash(uv --version)",
        ]
    }
}

MEMORY_TOPIC_1 = '''---
name: project_claude_bridge
description: Claude Code driven by Databricks/OpenRouter models via one-file bridge.py proxy
metadata:
  type: project
  originSessionId: fake-session-001
  modified: 2026-09-23
---
Project notes for claude-bridge.
'''

MEMORY_TOPIC_2 = '''---
name: project_media_server
description: Home media server "mediabox" runs on lan-host.lan; remote access enabled for friend sharing
metadata:
  type: project
  originSessionId: fake-session-002
  modified: 2026-08-12
---
Home media server "mediabox" runs on lan-host.lan; remote access enabled 2026-08-12
for friend sharing, behind double NAT needing a port forwarded on both routers.
Single library on a dedicated video drive (all transcoded video). A streaming pass
is active so shared friends stream free. Owner: the account owner.
'''


def _memory_md_250_lines() -> str:
    lines = ["# MEMORY.md (fake, for tests)", ""]
    lines.append("- [claude-bridge project](project_claude_bridge.md) -- proxy + harness work")
    lines.append('- [Home media server "mediabox"](project_media_server.md) -- runs on lan-host.lan; '
                 "remote access enabled 2026-08-12 for friend sharing")
    for i in range(3, 251):
        lines.append(f"- filler memory line {i} for truncation-cap testing")
    return "\n".join(lines) + "\n"


def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2), encoding="utf-8")


def build_fake_home(root: Optional[Path] = None) -> dict:
    """Build the fixture under `root` (a fresh tempdir if not given) and
    return a dict of useful paths: home, claude_dir, proj, sub, memory_dir."""
    root = Path(root) if root else Path(tempfile.mkdtemp(prefix="halo-fakehome-"))
    home = root / "home"
    claude_dir = home / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)

    # finding 16: settings.json is ALWAYS the synthetic stand-in now, same
    # as settings.local.json below -- a prior version copied rolo's real
    # settings.json when present, so the suite's actual behavior depended
    # on whatever happened to be in it on whichever machine ran the tests
    # (the model name, permission mode, etc.), exactly the "must not depend
    # on the live machine" defect finding 14/16 flags for settings.local.json.
    _write_json(claude_dir / "settings.json", _FALLBACK_SETTINGS_JSON)
    _write_json(claude_dir / "settings.local.json", _SETTINGS_LOCAL_JSON)

    # ~/.claude.json -- projects keyed in BOTH separator forms, plus a
    # stdio mcpServers entry (finding B: 15 real stdio servers on rolo's
    # box; one representative entry is enough for these fixtures).
    proj_dir = root / "proj"
    claude_json = {
        "mcpServers": {
            "expanded-models": {
                "type": "stdio",
                "command": "python",
                "args": ["-m", "expanded_models"],
                "env": {"SOME_KEY": "1"},
            }
        },
        "projects": {
            str(proj_dir).replace("\\", "/"): {
                "hasTrustDialogAccepted": True,
                "allowedTools": ["Bash", "Read"],
            },
            str(proj_dir).replace("/", "\\"): {
                "hasTrustDialogAccepted": False,
                "allowedTools": ["Edit"],
                "mcpServers": {
                    "local-only": {"type": "stdio", "command": "node", "args": ["server.js"]},
                },
                "enabledMcpjsonServers": ["local-only"],
            },
        },
    }
    _write_json(home / ".claude.json", claude_json)

    # ~/.claude/CLAUDE.md
    (claude_dir / "CLAUDE.md").write_text(
        "# User CLAUDE.md (fake)\n\nGlobal user instructions for tests.\n", encoding="utf-8",
    )

    # projects/<slug>/memory/MEMORY.md (250 lines) + two topic files.
    from halo_harness.config.paths import project_slug  # local import: avoid a hard dep for callers that don't need it

    slug = project_slug(proj_dir)
    memory_dir = claude_dir / "projects" / slug / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "MEMORY.md").write_text(_memory_md_250_lines(), encoding="utf-8")
    (memory_dir / "project_claude_bridge.md").write_text(MEMORY_TOPIC_1, encoding="utf-8")
    (memory_dir / "project_media_server.md").write_text(MEMORY_TOPIC_2, encoding="utf-8")

    # proj/ -- CLAUDE.md importing @AGENTS.md and @docs/extra.md, a fenced
    # @not-an-import, an HTML comment, sub/CLAUDE.md.
    (proj_dir / "docs").mkdir(parents=True, exist_ok=True)
    (proj_dir / "sub").mkdir(parents=True, exist_ok=True)
    (proj_dir / ".claude").mkdir(parents=True, exist_ok=True)

    (proj_dir / "AGENTS.md").write_text("Agents.md content (pulled in via explicit @AGENTS.md import).\n", encoding="utf-8")
    (proj_dir / "docs" / "extra.md").write_text("Extra imported doc content.\n", encoding="utf-8")
    (proj_dir / "CLAUDE.md").write_text(
        "# Project CLAUDE.md (fake)\n\n"
        "Root project instructions for tests.\n"
        "@AGENTS.md\n"
        "@docs/extra.md\n"
        "<!-- a block html comment\nthat spans two lines -->\n"
        "Text after the comment.\n"
        "```\n"
        "@not-an-import (inside a fence, must survive untouched)\n"
        "```\n",
        encoding="utf-8",
    )
    (proj_dir / "sub" / "CLAUDE.md").write_text("# Sub CLAUDE.md (fake)\n\nSub-directory instructions.\n", encoding="utf-8")

    return {
        "root": root,
        "home": home,
        "claude_dir": claude_dir,
        "proj": proj_dir,
        "sub": proj_dir / "sub",
        "memory_dir": memory_dir,
        "slug": slug,
    }


def add_fake_skill(skills_root: Path, *, name: str = "deploy", body: Optional[str] = None,
                    frontmatter_extra: Optional[dict] = None) -> Path:
    """OPT-IN skill fixture (H4) -- never called by `build_fake_home`
    itself. Writes `<skills_root>/<name>/SKILL.md` exercising `$0`/`$1`/
    `$ARGUMENTS`/`` !`echo pre` `` substitution (matching D-CFG's own
    fixture description), with `allowed-tools: Bash(echo pre)` so the
    pre-exec span is actually permitted. `skills_root` is normally
    `<proj>/.claude/skills` (project scope) or `<home>/.claude/skills`
    (user scope) -- the caller picks which by the path it passes. Returns
    the written SKILL.md path."""
    fm = {"description": "A fake deploy skill for tests.", "argument-hint": "<env> <version>",
          "allowed-tools": "Bash(echo pre)"}
    if frontmatter_extra:
        fm.update(frontmatter_extra)
    lines = ["---"]
    for k, v in fm.items():
        lines.append(f"{k}: {v}")
    lines.append("---")
    lines.append("")
    lines.append(body or (
        "Deploy environment $0 at version $1.\n"
        "All args: $ARGUMENTS\n"
        "Pre-exec output: !`echo pre`\n"
    ))
    skill_dir = skills_root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    path = skill_dir / "SKILL.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def add_fake_plugin(claude_dir: Path, *, plugin_name: str = "fake-plugin",
                     server_name: str = "fakeserver", enabled: bool = True,
                     via_plugin_json: bool = False, command: Optional[list] = None) -> dict:
    """OPT-IN plugin fixture (config/plugins.py's own layout) -- never
    called by `build_fake_home` itself, so no EXISTING test's MCP server
    count/set changes just by building a fake home; a plugin-server test
    calls this explicitly on the `claude_dir` a `build_fake_home()` call
    already returned. `via_plugin_json=True` declares the server through
    `.claude-plugin/plugin.json` instead of `.mcp.json`, to exercise that
    fallback path too. Returns `{plugins_dir, plugin_root, wire_server_name}`.
    `command` defaults to the real fake MCP stdio server (tests/helpers/
    fake_mcp_server.py) so a live-connect test can use this directly, with
    `${CLAUDE_PLUGIN_ROOT}` threaded into its env to prove expansion works."""
    import sys
    from halo_harness.config.plugins import plugin_server_name

    plugins_root = claude_dir / "plugins"
    plugin_root = plugins_root / "cache" / plugin_name
    plugin_root.mkdir(parents=True, exist_ok=True)

    manifest_path = plugins_root / "installed_plugins.json"
    manifest = {"plugins": {}}
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except ValueError:
            manifest = {"plugins": {}}
        manifest.setdefault("plugins", {})
    manifest["plugins"][plugin_name] = {"enabled": enabled}
    _write_json(manifest_path, manifest)

    server_entry = {
        "type": "stdio",
        "command": command[0] if command else sys.executable,
        "args": (command[1:] if command else ["-m", "tests.helpers.fake_mcp_server"]),
        "env": {"FAKE_PLUGIN_ROOT_SEEN": "${CLAUDE_PLUGIN_ROOT}"},
    }
    if via_plugin_json:
        (plugin_root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
        _write_json(plugin_root / ".claude-plugin" / "plugin.json",
                     {"name": plugin_name, "mcpServers": {server_name: server_entry}})
    else:
        _write_json(plugin_root / ".mcp.json", {"mcpServers": {server_name: server_entry}})

    return {
        "plugins_dir": plugins_root,
        "plugin_root": plugin_root,
        "wire_server_name": plugin_server_name(plugin_name, server_name),
    }
