@echo off
setlocal
echo.
echo ============================================
echo  ASR Transcription App - Desktop Shell
echo ============================================
echo.
for %%i in ("%~dp0..") do set "ROOT=%%~fi\"
set PYCACHE=%ROOT%.qwen3asr_python.cache

:: Resolve the env python (same cache fast-path as start.bat).
set "PYEXE="
if exist "%PYCACHE%" set /p PYEXE=<"%PYCACHE%"
if defined PYEXE if exist "%PYEXE%" goto :python_ready
set "PYEXE="

where conda >nul 2>&1
if errorlevel 1 (
    echo [ERROR] conda not found. Run setup.bat first.
    pause
    exit /b 1
)
for /f "usebackq delims=" %%i in (`conda run -n qwen3asr --no-capture-output python -c "import sys;print(sys.executable)"`) do set "PYEXE=%%i"
if not defined PYEXE (
    echo [ERROR] Failed to resolve python from the qwen3asr environment.
    pause
    exit /b 1
)
(echo %PYEXE%)>"%PYCACHE%"

:python_ready
echo Using Python: %PYEXE%

:: Minimal env activation (ffmpeg lives in the env's Library\bin).
for %%i in ("%PYEXE%") do set "ENVDIR=%%~dpi"
set "PATH=%ENVDIR%;%ENVDIR%Library\bin;%ENVDIR%Scripts;%PATH%"

:: Frontend build check (shell serves frontend/dist same as start.bat).
if not exist "%ROOT%frontend\dist\index.html" (
    echo [ERROR] frontend\dist missing - run start.bat once or "npm run build".
    pause
    exit /b 1
)

set CTX_SIZE=32768
set CHUNK_SECONDS=25
set KV_QUANT=q8_0
"%PYEXE%" "%ROOT%shell\shell_main.py"
if errorlevel 1 (
    echo.
    echo [WARN] Shell exited with an error - see data\logs\asr.log
    pause
)
