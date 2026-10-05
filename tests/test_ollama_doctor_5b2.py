"""tests.test_ollama_doctor_5b2 -- Halo 2.0.3 round 5b part 2 (brief item
4): `providers.ollama_panel.host_setup_checklist` per OS (behind an
injected `platform_name`, never the real `sys.platform`), `halo ollama
doctor`, and `halo doctor`'s own extended Ollama section.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.mock_ollama import MockUpstream

test, TESTS = new_registry()


# ---- host_setup_checklist, per OS ----------------------------------------

@test
def test_checklist_local_windows(ctx: Ctx):
    from halo_harness.providers.ollama import OllamaHost
    from halo_harness.providers.ollama_panel import host_setup_checklist
    host = OllamaHost(name="local", url="http://127.0.0.1:11434")
    lines = host_setup_checklist(host, platform_name="win32")
    text = " ".join(lines)
    ctx.check("loopback-only line present", "reachable from this machine only" in text)
    ctx.check("names the tray app switch", "tray app" in text and "Expose Ollama to the network" in text)
    ctx.check("recommendations present", "OLLAMA_FLASH_ATTENTION=1" in text and "OLLAMA_KV_CACHE_TYPE=q8_0" in text
              and "OLLAMA_NUM_PARALLEL=1" in text and "OLLAMA_KEEP_ALIVE" in text and "OLLAMA_CONTEXT_LENGTH" in text)
    ctx.check("only the Windows where-sentence, not macOS/Linux", "systemctl" not in text and "launchctl" not in text)


@test
def test_checklist_local_macos_marks_launchctl_unconfirmed(ctx: Ctx):
    from halo_harness.providers.ollama import OllamaHost
    from halo_harness.providers.ollama_panel import host_setup_checklist
    host = OllamaHost(name="local", url="http://127.0.0.1:11434")
    lines = host_setup_checklist(host, platform_name="darwin")
    text = " ".join(lines)
    ctx.check("names the menu-bar app switch", "menu-bar app" in text)
    ctx.check("names launchctl setenv", "launchctl setenv" in text)
    ctx.check("marked as research-doc community knowledge, not confirmed live",
              "not independently confirmed live" in text)
    ctx.check("only the macOS where-sentence", "systemctl" not in text and "tray app" not in text)


@test
def test_checklist_local_linux(ctx: Ctx):
    from halo_harness.providers.ollama import OllamaHost
    from halo_harness.providers.ollama_panel import host_setup_checklist
    host = OllamaHost(name="local", url="http://127.0.0.1:11434")
    lines = host_setup_checklist(host, platform_name="linux")
    text = " ".join(lines)
    ctx.check("names systemctl edit", "systemctl edit ollama" in text)
    ctx.check("names Environment=", 'Environment="VAR=value"' in text)
    ctx.check("only the Linux where-sentence", "launchctl" not in text and "tray app" not in text)


@test
def test_checklist_remote_host_prints_all_three_briefly(ctx: Ctx):
    """A remote (LAN/configured-by-name) host: unknown OS -> all three,
    and NEVER the loopback-only line (this entry is reachable by a
    non-loopback address already, by construction)."""
    from halo_harness.providers.ollama import OllamaHost
    from halo_harness.providers.ollama_panel import host_setup_checklist
    # 203.0.113.x is RFC 5737's reserved documentation/example range
    # (TEST-NET-3) -- non-loopback (so `is_local_host` reads it as remote,
    # the point of this test) without being a real RFC1918 LAN address
    # (`tests/test_privacy_scan.py` fails the build on one of those).
    host = OllamaHost(name="example-remote", url="http://203.0.113.5:11434")
    lines = host_setup_checklist(host)
    text = " ".join(lines)
    ctx.check("no loopback-only line for a non-loopback host", "reachable from this machine only" not in text)
    ctx.check("Windows where-sentence present", "tray app" in text)
    ctx.check("macOS where-sentence present", "menu-bar app" in text)
    ctx.check("Linux where-sentence present", "systemctl edit ollama" in text)


@test
def test_checklist_never_a_block_each_line_is_one_sentence_ish(ctx: Ctx):
    from halo_harness.providers.ollama import OllamaHost
    from halo_harness.providers.ollama_panel import host_setup_checklist
    host = OllamaHost(name="local", url="http://127.0.0.1:11434")
    lines = host_setup_checklist(host, platform_name="linux")
    ctx.check("a small number of lines, never a wall of text", 1 < len(lines) <= 6)
    ctx.check("every line is a plain string", all(isinstance(l, str) for l in lines))


# ---- halo ollama doctor / halo doctor's section --------------------------

@test
def test_ollama_doctor_cli_prints_analysis_and_checklist(ctx: Ctx):
    import io
    from contextlib import redirect_stdout
    from halo_harness.ollama_cli import cmd_ollama
    from halo_harness.theme import set_config_value
    import os
    import tempfile
    d = Path(tempfile.mkdtemp(prefix="ol-doctor-cli-"))
    os.environ["BRIDGE_STATE_DIR"] = str(d)
    mock = MockUpstream().start()
    try:
        set_config_value("ollama.hosts", [{"name": "mockhost", "url": mock.base_url, "default": True}])
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cmd_ollama(["doctor"])
        out = buf.getvalue()
        ctx.check(f"exit 0, got {rc}", rc == 0)
        ctx.check("shows the host analysis (version)", "version" in out)
        ctx.check("shows the setup checklist too", "OLLAMA_FLASH_ATTENTION" in out)
    finally:
        mock.stop()
        os.environ.pop("BRIDGE_STATE_DIR", None)


@test
def test_doctor_py_ollama_section_includes_checklist_lines(ctx: Ctx):
    from halo_harness.doctor import _check_ollama_hosts
    from halo_harness.theme import set_config_value
    import os
    import tempfile
    d = Path(tempfile.mkdtemp(prefix="ol-doctor-section-"))
    os.environ["BRIDGE_STATE_DIR"] = str(d)
    mock = MockUpstream().start()
    try:
        set_config_value("ollama.hosts", [{"name": "mockhost", "url": mock.base_url, "default": True}])
        entries = _check_ollama_hosts()
        ctx.check(f"more than one entry for this one host (reachability + checklist lines), got {len(entries)}",
                  len(entries) > 1)
        joined = " ".join(line for _cid, line in entries)
        ctx.check("the checklist text is present", "OLLAMA_KV_CACHE_TYPE" in joined)
        ctx.check("every entry is [OK]-prefixed (never a WARN just for the checklist)",
                  all(line.startswith("[OK]") for _cid, line in entries))
    finally:
        mock.stop()
        os.environ.pop("BRIDGE_STATE_DIR", None)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
