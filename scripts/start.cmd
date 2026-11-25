@echo off
setlocal
pushd "%~dp0.."
py -3.12 -X utf8 -B -m app.launch --open-browser
set "EXIT_CODE=%ERRORLEVEL%"
popd
if not "%EXIT_CODE%"=="0" (
  echo.
  echo Shixu could not start. See the message above.
  pause
)
exit /b %EXIT_CODE%
