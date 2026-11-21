@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

rem Ensure virtual environment exists
if not exist ".venv\Scripts\pythonw.exe" (
    echo [SIMPLE_3D_STEP] Setting up Python environment via uv...
    where uv >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] 'uv' is not installed or not on PATH.
        pause
        exit /b 1
    )
    uv sync
    if errorlevel 1 (
        echo [ERROR] Failed to sync environment.
        pause
        exit /b 1
    )
)

rem Detect explicit CLI flags safely (immune to filenames containing -i, etc.)
set "RUN_CLI=0"
for %%A in (%*) do (
    if /i "%%~A"=="--cli" set "RUN_CLI=1"
    if /i "%%~A"=="-i" set "RUN_CLI=1"
    if /i "%%~A"=="--input" set "RUN_CLI=1"
    if /i "%%~A"=="-h" set "RUN_CLI=1"
    if /i "%%~A"=="--help" set "RUN_CLI=1"
)

if "%RUN_CLI%"=="1" (
    uv run simple-3d-step %*
    exit /b %errorlevel%
)

rem Launch Tkinter GUI without leaving a console window open
rem (Supports double-click or dragging a .step/.stp file onto this .bat file)
start "" "%~dp0.venv\Scripts\pythonw.exe" -m simple_3d_step.main %*
endlocal
