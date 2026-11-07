@echo off
rem Sends every email to one address of your choice, marked [TEST]. Changes no files or logs.
setlocal
cd /d "%~dp0"
call "%~dp0_python.cmd" || (pause & exit /b 1)
set /p "ME=Your email address: "
if "%ME%"=="" (echo No address given. & pause & exit /b 1)
%PY% -m mailrows send --test-to "%ME%" -c config.ini
pause
