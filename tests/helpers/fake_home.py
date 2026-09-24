"""tests.helpers.fake_home -- builds a temp home directory mirroring rolo's
real ~/.claude layout (plan D-CFG "Fixtures"), for BRIDGE_TEST_HOME.

settings.local.json is a byte-for-byte SNAPSHOT of rolo's real 19 rules
(finding 14: the suite must not depend on whatever settings.local.json
happens to exist on the machine running it -- a prior version copied the
live file, which passed only on rolo's own box and read 27+ rules on any
other machine, incl. the Kali VM this suite ultimately runs on). settings.json
still falls back to a synthetic stand-in when rolo's real file isn't present,
since nothing asserts its exact shape/count the way the 19-rules test does.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Optional

_REAL_HOME = Path.home()

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
name: project_plex_server
description: Plex server "roundBoy" runs on lan-host.lan (RTX 4090); remote access enabled for friend sharing
metadata:
  type: project
  originSessionId: fake-session-002
  modified: 2026-08-12
---
Plex server "roundBoy" runs on lan-host.lan (RTX 4090); remote access enabled 2026-08-12
for friend sharing, behind double NAT needing 32400 forwarded on both routers.
Single Movies library on D:\\vids (all 1080p HEVC). Plex Pass active so shared friends
stream free. Owner owner.
'''


def _memory_md_250_lines() -> str:
    lines = ["# MEMORY.md (fake, for tests)", ""]
    lines.append("- [claude-bridge project](project_claude_bridge.md) -- proxy + harness work")
    lines.append('- [Plex server "roundBoy"](project_plex_server.md) -- runs on lan-host.lan (RTX 4090); '
                 "remote access enabled 2026-08-12 for friend sharing")
    for i in range(3, 251):
        lines.append(f"- filler memory line {i} for truncation-cap testing")
    return "\n".join(lines) + "\n"


def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2), encoding="utf-8")


def _copy_or_fallback(real_path: Path, dest_path: Path, fallback_obj) -> None:
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    if real_path.exists():
        shutil.copyfile(real_path, dest_path)
    else:
        _write_json(dest_path, fallback_obj)


def build_fake_home(root: Optional[Path] = None) -> dict:
    """Build the fixture under `root` (a fresh tempdir if not given) and
    return a dict of useful paths: home, claude_dir, proj, sub, memory_dir."""
    root = Path(root) if root else Path(tempfile.mkdtemp(prefix="rolo-claude-fakehome-"))
    home = root / "home"
    claude_dir = home / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)

    # settings.json falls back to a synthetic stand-in when rolo's real file
    # isn't present on this machine; settings.local.json is ALWAYS the fixed
    # 19-rule snapshot above, regardless of what's on the machine running the
    # tests (finding 14 -- this file's own docstring explains why).
    _copy_or_fallback(_REAL_HOME / ".claude" / "settings.json", claude_dir / "settings.json", _FALLBACK_SETTINGS_JSON)
    _write_json(claude_dir / "settings.local.json", _SETTINGS_LOCAL_JSON)

    # ~/.claude.json -- projects keyed in BOTH separator forms, plus a
    # stdio mcpServers entry (finding B: 15 real stdio servers on rolo's
    # box; one representative entry is enough for these fixtures).
    proj_dir = home_proj = root / "proj"
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
    from rolo_claude.config.paths import project_slug  # local import: avoid a hard dep for callers that don't need it

    slug = project_slug(proj_dir)
    memory_dir = claude_dir / "projects" / slug / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "MEMORY.md").write_text(_memory_md_250_lines(), encoding="utf-8")
    (memory_dir / "project_claude_bridge.md").write_text(MEMORY_TOPIC_1, encoding="utf-8")
    (memory_dir / "project_plex_server.md").write_text(MEMORY_TOPIC_2, encoding="utf-8")

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
