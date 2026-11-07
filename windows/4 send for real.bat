@echo off
rem Sends the real emails after showing a preview and asking you to type yes.
setlocal
cd /d "%~dp0"
call "%~dp0_python.cmd" || (pause & exit /b 1)
%PY% -m mailrows send -c config.ini
pause
