@echo off
if exist "%~dp0.venv\Scripts\python.exe" (
  "%~dp0.venv\Scripts\python.exe" "%~dp0ocswarm.py" %*
) else (
  python "%~dp0ocswarm.py" %*
)
