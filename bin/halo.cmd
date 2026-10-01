@echo off
rem halo (Windows) -- mirrors bin/halo (POSIX): prefers the installed
rem `uv tool install --editable .` / `pip install --user -e .` console
rem script when one exists on this machine and isn't THIS file (a PATH
rem entry pointing back at this same bin\ directory would otherwise
rem recurse into itself forever -- bin/halo's own comment explains the
rem equivalent POSIX symlink case), falling back to `python -m
rem halo_harness` from this checkout (PYTHONPATH set to the repo root, one
rem level up from this bin\ directory) otherwise.
rem finding 9 (2.0.0 fixpass): the installed-target branch used to call
rem "%%G" %* and `exit /b %ERRORLEVEL%` INSIDE the parenthesised `for ...
rem do ( )` block -- cmd.exe expands %ERRORLEVEL% once when the whole
rem parenthesised block is PARSED, before "%%G" %* actually runs, so the
rem real exit code was always lost (silently replaced by whatever
rem %ERRORLEVEL% happened to be before this script started). The target is
rem stashed in a plain variable instead and the call happens after a
rem `goto`, outside any parenthesised block, so %ERRORLEVEL% is read fresh.
setlocal
set "HALO_CMD_HERE=%~dp0"
set "HALO_CMD_SELF=%~f0"
set "HALO_CMD_TARGET="
for /f "delims=" %%G in ('where halo 2^>nul') do (
    if /i not "%%G"=="%HALO_CMD_SELF%" if not defined HALO_CMD_TARGET set "HALO_CMD_TARGET=%%G"
)
if defined HALO_CMD_TARGET goto :run

set "PYTHONPATH=%HALO_CMD_HERE%..;%PYTHONPATH%"
python -m halo_harness %*
exit /b %ERRORLEVEL%

:run
"%HALO_CMD_TARGET%" %*
exit /b %ERRORLEVEL%
