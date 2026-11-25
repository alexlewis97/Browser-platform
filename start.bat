@echo off
setlocal EnableExtensions
cd /d "%~dp0"

rem Always prefer the per-user Node we installed (PATH may not have refreshed yet).
if exist "%LOCALAPPDATA%\BrowserPlatform\runtime\node\node.exe" (
  set "PATH=%LOCALAPPDATA%\BrowserPlatform\runtime\node;%PATH%"
)
if exist "%LOCALAPPDATA%\BrowserPlatform\runtime\node-home.txt" (
  for /f "usebackq delims=" %%I in ("%LOCALAPPDATA%\BrowserPlatform\runtime\node-home.txt") do (
    if exist "%%I\node.exe" set "PATH=%%I;%PATH%"
  )
)

set "NEED_INSTALL=0"
if not exist ".venv\Scripts\python.exe" set "NEED_INSTALL=1"
if not exist "node_host\node_modules\pomerado" set "NEED_INSTALL=1"
if exist "release-id.txt" (
  if not exist ".install-stamp" (
    set "NEED_INSTALL=1"
  ) else (
    fc /b "release-id.txt" ".install-stamp" >nul 2>&1
    if errorlevel 1 set "NEED_INSTALL=1"
  )
)

if "%NEED_INSTALL%"=="1" (
  echo Installing or upgrading Browser Platform. This can take a few minutes...
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
  if errorlevel 1 (
    echo Install failed. You can re-run install.ps1 from PowerShell with:
    echo   powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
    pause
    exit /b 1
  )
)

rem Local Windows mode does not start a tunnel or the tunnel watchdog.
echo Starting Browser Platform at http://127.0.0.1:8745
echo Leave this window open. Close it to stop the backend.
echo Chrome will open with a dedicated profile if nothing is already on port 9222.
start "" cmd /c "timeout /t 2 /nobreak >nul && start http://127.0.0.1:8745/"
".venv\Scripts\python.exe" -m browser_platform --host 127.0.0.1 --port 8745
if errorlevel 1 (
  echo.
  echo The backend stopped with an error.
  pause
)
