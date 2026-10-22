@echo off
rem Portable launcher for the powerbi-desktop MCP server (see launch.ps1 for details).
rem Claude runs this file; it must never print anything to stdout except the MCP protocol.
setlocal EnableExtensions

set "PY="
rem launch.ps1 makes sure a private Python environment exists (first run downloads/installs it,
rem later runs return immediately) and prints the interpreter path. stdin is closed for it so it
rem can never read Claude's messages; its progress messages go to stderr and launcher.log.
for /f "usebackq delims=" %%P in (`powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0launch.ps1" -Prepare ^<nul`) do set "PY=%%P"

if not defined PY (
    >&2 echo [powerbi-desktop] Setup failed - see %LOCALAPPDATA%\powerbi-desktop-mcp\launcher.log
    exit /b 1
)

"%PY%" "%~dp0run_server.py"
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
    rem The server stopped with an error: let the launcher rebuild a damaged environment next time.
    powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0launch.ps1" -Repair <nul >nul 2>&1
)
exit /b %RC%
