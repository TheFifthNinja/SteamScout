@echo off
title Steam Compatibility Checker
color 0A

echo.
echo  ==========================================
echo   Steam Compatibility Checker
echo  ==========================================
echo.

:: ── Check Python ──────────────────────────────────────────────────────────────
python --version >nul 2>&1
if errorlevel 1 (
    echo  [ERROR] Python not found.
    echo  Please install Python 3.9+ from https://python.org
    echo  Make sure to tick "Add Python to PATH" during install.
    echo.
    pause
    exit /b 1
)

:: ── Install dependencies ───────────────────────────────────────────────────────
echo  [1/4] Checking dependencies...
pip install -r requirements.txt -q --disable-pip-version-check
if errorlevel 1 (
    echo  [ERROR] Dependency install failed. Check your internet connection.
    pause
    exit /b 1
)
echo        OK.

:: ── Find Steam install path ────────────────────────────────────────────────────
echo  [2/4] Locating Steam...

set STEAM_PATH=
for /f "tokens=2*" %%a in ('reg query "HKCU\Software\Valve\Steam" /v "SteamExe" 2^>nul') do (
    set "STEAM_PATH=%%b"
)

if "%STEAM_PATH%"=="" (
    :: Fallback: common default paths
    if exist "C:\Program Files (x86)\Steam\steam.exe" set "STEAM_PATH=C:\Program Files (x86)\Steam\steam.exe"
    if exist "C:\Program Files\Steam\steam.exe"       set "STEAM_PATH=C:\Program Files\Steam\steam.exe"
)

if "%STEAM_PATH%"=="" (
    echo  [ERROR] Could not find Steam. Is it installed?
    pause
    exit /b 1
)
echo        Found: %STEAM_PATH%

:: ── Ensure Steam debug endpoint is available ──────────────────────────────────
echo  [3/4] Checking Steam debug session...

set DEBUG_OK=0
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -UseBasicParsing -Uri 'http://localhost:8080/json' -TimeoutSec 3; if ($r.StatusCode -eq 200) { exit 0 } else { exit 1 } } catch { exit 1 }"
if not errorlevel 1 set DEBUG_OK=1

if "%DEBUG_OK%"=="1" (
    echo        Existing Steam debug session found. No restart needed.
) else (
    echo        Debug endpoint not active. Restarting Steam once with debug flags...
    echo.
    echo  NOTE: Steam may restart briefly this time only.

    taskkill /im steam.exe /f >nul 2>&1
    timeout /t 3 /nobreak >nul

    start "" "%STEAM_PATH%" -remote-debugging-port=8080 -cef-enable-debugging -cef-enable-remote-debugging -cef-remote-debugging-port=8080
    timeout /t 6 /nobreak >nul

    powershell -NoProfile -Command "try { $r = Invoke-WebRequest -UseBasicParsing -Uri 'http://localhost:8080/json' -TimeoutSec 4; if ($r.StatusCode -eq 200) { exit 0 } else { exit 1 } } catch { exit 1 }"
    if errorlevel 1 (
        echo  [ERROR] Steam debug endpoint is still not reachable at http://localhost:8080/json
        echo  Steam may be blocking debug flags in this build/session.
        echo  Fully close Steam, then run this script again.
        pause
        exit /b 1
    )
)

:: ── Start backend ─────────────────────────────────────────────────────────────
echo  [4/4] Starting compatibility checker...
start "SteamCompat-Backend" /min python backend.py

:: Give backend time to start WebSocket server
timeout /t 2 /nobreak >nul

:: ── Start overlay (foreground — closing this exits everything) ─────────────────
python overlay.py

:: ── Cleanup ───────────────────────────────────────────────────────────────────
echo.
echo  Overlay closed. Stopping backend...
taskkill /f /fi "WindowTitle eq SteamCompat-Backend" >nul 2>&1
echo  Done.