@echo off
rem Builds dist\PalHostFix.exe (single file, no console).
cd /d "%~dp0"
if not exist .venv python -m venv .venv
.venv\Scripts\python.exe -m pip install -q -r requirements.txt pyinstaller
.venv\Scripts\python.exe -m PyInstaller --noconfirm --onefile --windowed --name PalHostFix --collect-binaries ooz PalHostFix.pyw
echo.
echo Built: %~dp0dist\PalHostFix.exe
