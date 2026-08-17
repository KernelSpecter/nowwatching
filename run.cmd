@echo off
REM Double-click launcher. Keeps the window open on a bad exit so the error is
REM readable instead of flashing past.
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
  py -3 nowwatching.py %*
) else (
  python nowwatching.py %*
)

if errorlevel 1 (
  echo.
  echo nowwatching exited with an error. See run\nowwatching.log
  pause
)
endlocal
