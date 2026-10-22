@echo off
rem Exports a plain-text snapshot of your open Power BI dashboard(s) for a Microsoft 365 Copilot agent.
rem Usage: export-snapshot.cmd [--instance NAME] [--pbix FILE] [--out FOLDER] [--no-quality] [--traces]
setlocal
for %%V in (3.12 3.13 3.11 3.10) do (
  py -%%V -c "import mcp, clr, psutil" >nul 2>nul
  if not errorlevel 1 (
    py -%%V "%~dp0export_snapshot.py" %*
    exit /b %errorlevel%
  )
)
python -c "import mcp, clr, psutil" >nul 2>nul
if not errorlevel 1 (
  python "%~dp0export_snapshot.py" %*
  exit /b %errorlevel%
)
echo Could not find a Python (3.10-3.13) with the plugin's packages installed.
echo Run once:  python -m pip install -r "%~dp0..\requirements.txt"
exit /b 1
