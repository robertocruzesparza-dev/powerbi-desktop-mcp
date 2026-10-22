@echo off
rem Guided setup / runner for the Microsoft 365 Copilot build. Usage: setup-copilot.cmd [setup|run|status|package|reset] [--yes]
rem Uses the first suitable Python it finds (3.12, 3.13, 3.11, 3.10, then whatever "python" is).
setlocal
for %%V in (3.12 3.13 3.11 3.10) do (
  py -%%V -c "import sys" >nul 2>nul
  if not errorlevel 1 (
    py -%%V "%~dp0setup_copilot.py" %*
    exit /b %errorlevel%
  )
)
where python >nul 2>nul
if not errorlevel 1 (
  python "%~dp0setup_copilot.py" %*
  exit /b %errorlevel%
)
echo Python 3.10-3.13 was not found. Install it from https://www.python.org/downloads/ and run this again.
exit /b 1
