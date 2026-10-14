@echo off
rem Builds dist\PalHostFix.exe (single file, no console).
cd /d "%~dp0"
if not exist .venv python -m venv .venv
.venv\Scripts\python.exe -m pip install -q -r requirements.txt pyinstaller
.venv\Scripts\python.exe -m PyInstaller --noconfirm --onefile --windowed --name PalHostFix --collect-binaries ooz PalHostFix.pyw
rem Checksum to paste into the release notes, so people can check their download.
certutil -hashfile dist\PalHostFix.exe SHA256 | findstr /v ":" > dist\PalHostFix.exe.sha256
echo.
echo Built: %~dp0dist\PalHostFix.exe
echo SHA-256:
type dist\PalHostFix.exe.sha256
