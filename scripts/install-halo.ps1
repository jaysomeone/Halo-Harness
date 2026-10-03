#!/usr/bin/env pwsh
# install-halo.ps1 -- install Halo Harness (the successor of rolo-claude) on
# Windows and make sure an old rolo-claude install can never shadow it.
# Mirrors scripts/install-halo.sh (same steps, same flags, same spirit).
#
#   powershell -ExecutionPolicy ByPass -File install-halo.ps1
#   .\install-halo.ps1 -Yes          # no questions (uninstalls old rolo-claude)
#   .\install-halo.ps1 -NoClone      # install straight from GitHub, no clone
#   .\install-halo.ps1 -DryRun       # print every command this script would
#                                     # run (installs, uninstalls, clone/pull),
#                                     # never actually runs them -- no network,
#                                     # no filesystem change, never prompts
#   .\install-halo.ps1 -CloneDir C:\src\Halo-Harness
#
# Needs: git, Python 3.10+. Installs uv if it is missing.
[CmdletBinding()]
param(
    [switch]$Yes,
    [switch]$NoClone,
    [switch]$DryRun,
    [string]$CloneDir = $(if ($env:HALO_CLONE_DIR) { $env:HALO_CLONE_DIR } else { "$HOME\Halo-Harness" })
)

$RepoUrl = "https://github.com/roloVibes/Halo-Harness.git"
$RepoUrlNoGit = "https://github.com/roloVibes/Halo-Harness"
# Deliberately NOT "Stop": several steps below call a native .exe (pip,
# git, uv) whose normal, non-fatal stderr chatter (progress output, a
# "not found" warning on a routine existence check) would otherwise be
# promoted into a script-ending exception. Every step that must actually
# stop the script on failure checks $LASTEXITCODE itself instead.
$ErrorActionPreference = "Continue"

function Say($msg) { Write-Host "`n==> $msg" }
function Have($cmd) { [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }

# Every REAL action (never a read-only detection) goes through Run -- prints
# the exact command instead of executing it under -DryRun. Only for plain
# argv-shaped external commands; a step with its own control flow (clone-or-
# pull, the uv self-installer) gets its own explicit dry-run branch instead.
function Run {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$CommandParts)
    if ($DryRun) { Write-Host "DRY RUN: $($CommandParts -join ' ')" }
    else { & $CommandParts[0] @($CommandParts[1..($CommandParts.Length - 1)]) }
}

function Confirm($prompt) {
    if ($Yes) { return $true }
    if ($DryRun) { Write-Host "DRY RUN: would ask: $prompt [y/N]"; return $false }
    $r = Read-Host "$prompt [y/N]"
    return $r -match '^[Yy]'
}

# 1. Python 3.10+ (detection only -- real either way, -DryRun still needs the
#    real answer to decide whether it would even proceed).
$PyCmd = $null
foreach ($c in @("py", "python", "python3")) {
    if (Have $c) {
        $ok = & $c -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) { $PyCmd = $c; break }
    }
}
if (-not $PyCmd) {
    Write-Host "Python 3.10 or newer is required (check: python --version)."
    exit 1
}
Say "Python: $(& $PyCmd --version)"

# 2. uv (isolated tool installs)
if (-not (Have "uv")) {
    if ($DryRun) {
        Say "DRY RUN: uv not found -- would install it: irm https://astral.sh/uv/install.ps1 | iex"
    } else {
        Say "uv not found: installing it from https://astral.sh/uv"
        powershell -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex"
        $env:PATH = "$HOME\.local\bin;$env:PATH"
        if (-not (Have "uv")) { Write-Host "uv still not on PATH; open a new terminal and re-run."; exit 1 }
    }
} else {
    Say "uv: $(uv --version)"
}

# 3. Remove the old rolo-claude tool so it can never run by mistake.
#    (halo migrates ~/.rolo-claude to ~/.halo by itself on first run.)
if ((uv tool list 2>$null) -match '^rolo-claude ') {
    Say "Old rolo-claude uv tool found"
    if (Confirm "Uninstall it (recommended)?") { Run uv tool uninstall rolo-claude }
}
if ((Have "pipx") -and ((pipx list 2>$null) -match 'package rolo-claude')) {
    Say "Old rolo-claude pipx install found"
    if (Confirm "Uninstall it (recommended)?") { Run pipx uninstall rolo-claude }
}
& $PyCmd -m pip show rolo-claude 2>$null | Out-Null
if ($LASTEXITCODE -eq 0) {
    Say "Old rolo-claude pip install found"
    if (Confirm "Uninstall it (recommended)?") { Run $PyCmd -m pip uninstall -y rolo-claude }
}

# 4. Install halo (2.0.1+ ships exactly one executable: halo)
if ($NoClone) {
    Say "Installing halo straight from GitHub"
    Run uv tool install --reinstall "git+$RepoUrlNoGit"
} else {
    if (Test-Path (Join-Path $CloneDir ".git")) {
        Say "Updating the clone at $CloneDir"
        if ($DryRun) { Write-Host "DRY RUN: git -C $CloneDir pull --ff-only" }
        else { git -C $CloneDir pull --ff-only }
    } else {
        Say "Cloning to $CloneDir"
        if ($DryRun) { Write-Host "DRY RUN: git clone $RepoUrl $CloneDir" }
        else { git clone $RepoUrl $CloneDir }
    }
    Say "Installing halo from the clone"
    if ($DryRun) { Write-Host "DRY RUN: (cd $CloneDir && uv tool install --reinstall .)" }
    else { Push-Location $CloneDir; try { uv tool install --reinstall . } finally { Pop-Location } }
}

# 5. PATH for the uv tools directory
try { uv tool update-shell *> $null } catch {}
$env:PATH = "$HOME\.local\bin;$env:PATH"
if (($env:PATH -split ';') -notcontains "$HOME\.local\bin") {
    Write-Host 'Add %USERPROFILE%\.local\bin to PATH (Settings > System > About > Advanced > Environment Variables), then open a new terminal.'
}

# 6. Verify: halo first on PATH, no rolo-claude left anywhere on PATH
Say "Verify"
if ($DryRun) {
    Write-Host "DRY RUN: would check halo is on PATH, warn about a lingering rolo-claude, then run: halo doctor"
} else {
    if (-not (Have "halo")) {
        Write-Host "halo is not on PATH yet: open a new terminal (uv tools live in %USERPROFILE%\.local\bin) and run: halo doctor"
        exit 1
    }
    $haloPath = (Get-Command halo).Source
    Write-Host "halo: $haloPath  ($(halo --version))"
    if (Have "rolo-claude") {
        $old = (Get-Command rolo-claude).Source
        Write-Host "WARNING: an old rolo-claude is still on PATH at $old."
        Write-Host "         Remove it so it cannot run by mistake (halo doctor names the exact command)."
    }
    try { halo doctor } catch {}
}

Say "Done. Next:"
Write-Host "  cd ~ ; halo init      # pick a provider (Databricks at work, OpenRouter at home), then a live pong"
Write-Host "  halo                  # the TUI, from any directory"
if (-not $NoClone) {
    Write-Host "Updates later:  cd $CloneDir ; git pull ; uv tool install --reinstall ."
} else {
    Write-Host "Updates later:  uv tool install --reinstall git+$RepoUrlNoGit"
}
Write-Host "Or just:        halo update    (or /update inside halo)"
