@echo off
rem Checks every row and shows what would be sent. Sends nothing and changes no files.
setlocal
cd /d "%~dp0"
call "%~dp0_python.cmd" || (pause & exit /b 1)
%PY% -m mailrows send --dry-run -c config.ini
pause
