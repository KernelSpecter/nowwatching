@echo off
REM Double-click this once. It registers nowwatching to start at login and
REM starts it immediately, so there is nothing else to do afterwards.
REM
REM To undo, double-click uninstall.cmd.
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
  py -3 nowwatching.py --install
) else (
  python nowwatching.py --install
)

if errorlevel 1 (
  echo.
  echo Install did not complete. See the message above.
  pause
)
endlocal
