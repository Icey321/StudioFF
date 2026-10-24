@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
	echo The UI environment is missing. Run install.ps1 first.
	pause
	exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" -m ffstudio
