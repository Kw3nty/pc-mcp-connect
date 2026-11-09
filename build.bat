@echo off
rem Build dist\PcMcpConnect.exe. Requires Python 3.11+ (https://www.python.org/downloads/).
rem Before building, put the shared relay address into defaults.json (see README.md).
setlocal
cd /d "%~dp0"
if exist .venv\Scripts\python.exe goto :deps
py -3 -m venv .venv 2>nul
if not exist .venv\Scripts\python.exe python -m venv .venv
if not exist .venv\Scripts\python.exe goto :err
:deps
.venv\Scripts\python -m pip install -q --disable-pip-version-check -r requirements.txt pyinstaller
if errorlevel 1 goto :err
.venv\Scripts\python app\make_icon.py
if errorlevel 1 goto :err
.venv\Scripts\python -m PyInstaller --noconfirm --clean --onefile --noconsole --name PcMcpConnect --icon "%~dp0build\icon.ico" --distpath dist --workpath build\pyi --specpath build --paths app --add-data "%~dp0defaults.json;." --add-data "%~dp0README.md;." --hidden-import pc_mcp_server --hidden-import pystray._win32 --hidden-import aiohttp_socks --collect-submodules mcp.server --collect-submodules mcp.shared --collect-submodules uvicorn app\pcmcp_connect.py
if errorlevel 1 goto :err
echo.
echo OK: %~dp0dist\PcMcpConnect.exe
exit /b 0
:err
echo.
echo BUILD FAILED - see messages above.
exit /b 1
