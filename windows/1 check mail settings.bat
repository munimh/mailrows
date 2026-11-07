@echo off
rem Tries the usual ports and security settings for your mail server. Sends nothing.
setlocal
cd /d "%~dp0"
call "%~dp0_python.cmd" || (pause & exit /b 1)
%PY% -m mailrows smtp-check -c config.ini
pause
