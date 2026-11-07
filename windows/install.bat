@echo off
rem Installs mailrows for the current Windows user.
setlocal
set "PY="
for %%C in ("py -3" "python" "python3") do (
    if not defined PY (
        %%~C -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1 && set "PY=%%~C"
    )
)
if not defined PY (
    echo Python 3.10 or newer was not found.
    echo Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH", then run this again.
    pause
    exit /b 1
)
if exist "%~dp0..\pyproject.toml" (
    echo Installing mailrows from this folder...
    %PY% -m pip install --user "%~dp0.."
) else (
    echo Installing mailrows...
    %PY% -m pip install --user mailrows
)
if errorlevel 1 (
    echo.
    echo The install failed. The messages above say why.
) else (
    echo.
    echo Done. Next: put config.ini next to these .bat files ^(copy config.example.ini^) and fill it in.
)
pause
