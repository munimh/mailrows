@echo off
rem Finds a Python 3.10+ that can import mailrows and sets PY to it.
rem Used by the other .bat files in this folder.

set "PY="
rem running from a downloaded copy of the source code: use it directly
if exist "%~dp0..\src\mailrows\__init__.py" set "PYTHONPATH=%~dp0..\src;%PYTHONPATH%"

set "CHECK=import sys, mailrows, openpyxl; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
for %%C in ("python" "py -3" "python3") do (
    if not defined PY (
        %%~C -c "%CHECK%" >nul 2>&1 && set "PY=%%~C"
    )
)
if defined PY exit /b 0

set "FOUND="
for %%C in ("python" "py -3" "python3") do (
    if not defined FOUND (
        %%~C -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1 && set "FOUND=1"
    )
)
if defined FOUND (
    echo mailrows is not installed yet. Double-click install.bat first.
) else (
    echo Python 3.10 or newer was not found.
    echo Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
)
exit /b 1