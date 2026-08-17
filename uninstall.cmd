@echo off
REM Removes the login entry. A copy already running is left alone; close it from
REM Task Manager or just log out.
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
  py -3 nowwatching.py --uninstall
) else (
  python nowwatching.py --uninstall
)
endlocal
