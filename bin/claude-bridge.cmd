@echo off
where uv >nul 2>nul
if %errorlevel% == 0 (
    uv run --script "%~dp0..\bridge.py" launch %*
) else (
    python "%~dp0..\bridge.py" launch %*
)
