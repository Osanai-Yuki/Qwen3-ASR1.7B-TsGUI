@echo off
setlocal
echo.
echo ============================================
echo  Building ASR Transcription App
echo ============================================
echo.

set ROOT=%~dp0
set DIST_DIR=%ROOT%dist
set BUILD_DIR=%ROOT%build
set APP_OUT=%ROOT%Qwen3-ASR
set SHELL_OUT=%ROOT%Qwen3-ASR-Shell

:: Build mode: app (default, browser)  shell (WebView2 desktop)  both
set MODE=%1
if "%MODE%"=="" set MODE=app
set DO_APP=
set DO_SHELL=
if /i "%MODE%"=="app"   (set "DO_APP=1"& set "DO_SHELL=0")
if /i "%MODE%"=="shell" (set "DO_APP=0"& set "DO_SHELL=1")
if /i "%MODE%"=="both"  (set "DO_APP=1"& set "DO_SHELL=1")
if not defined DO_APP (
    echo [ERROR] Unknown mode "%MODE%". Usage: build.bat [app^|shell^|both]
    pause
    exit /b 1
)
echo Build mode: %MODE%

:: Resolve the conda environment's python. Prefer a local override, then
:: conda run (works regardless of install path), then fall back to plain
:: python if conda is absent.
if defined PYTHON_EXE (
    set "PYCMD=%PYTHON_EXE%"
) else (
    where conda >nul 2>&1
    if not errorlevel 1 (
        set "PYCMD=conda run -n qwen3asr --no-capture-output python"
    ) else (
        set "PYCMD=python"
    )
)

echo Using Python: %PYCMD%
echo Verifying environment...
call %PYCMD% -c "import fastapi, uvicorn, numpy, PyInstaller" >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Required packages missing in the target Python environment.
    echo         Activate qwen3asr or run: conda env create -f environment.yml
    pause
    exit /b 1
)
:: The desktop shell additionally needs pywebview (WebView2 backend).
if "%DO_SHELL%"=="1" (
    call %PYCMD% -c "import webview" >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] pywebview missing - required for the desktop shell.
        echo         Install it:  pip install pywebview
        echo         or update:   conda env update -f environment.yml
        pause
        exit /b 1
    )
)

echo Cleaning previous build...
if exist "%BUILD_DIR%" rmdir /s /q "%BUILD_DIR%"
if exist "%DIST_DIR%" rmdir /s /q "%DIST_DIR%"
if "%DO_APP%"=="1" if exist "%APP_OUT%" rmdir /s /q "%APP_OUT%"
if "%DO_SHELL%"=="1" if exist "%SHELL_OUT%" rmdir /s /q "%SHELL_OUT%"

echo Building frontend...
cd /d "%ROOT%frontend"
call npm run build
if %errorlevel% neq 0 (
    echo [ERROR] Frontend build failed.
    cd /d "%ROOT%"
    pause
    exit /b 1
)
cd /d "%ROOT%"

:: Dispatch per-mode builds (goto structure avoids nested-paren pitfalls).
if not "%DO_APP%"=="1" goto :skip_app
call :build_app
if errorlevel 1 exit /b 1
:skip_app

if not "%DO_SHELL%"=="1" goto :skip_shell
call :build_shell
if errorlevel 1 exit /b 1
:skip_shell

echo.
echo Cleaning build artifacts...
rmdir /s /q "%BUILD_DIR%" 2>nul
rmdir /s /q "%DIST_DIR%" 2>nul

echo.
echo ============================================
echo  Build complete
echo ============================================
if "%DO_APP%"=="1"   echo  - Browser app:   %APP_OUT%
if "%DO_SHELL%"=="1" echo  - Desktop shell: %SHELL_OUT%
echo.
pause
exit /b 0


:: ====================================================================
:: Browser app: asr-app.exe (opens http://127.0.0.1:8000 in the system
:: browser). Output: Qwen3-ASR\
:: ====================================================================
:build_app
echo.
echo === Building browser app (asr-app.exe) ===
call %PYCMD% -m PyInstaller asr-app.spec ^
    --distpath "%DIST_DIR%" ^
    --workpath "%BUILD_DIR%" ^
    --noconfirm ^
    --log-level=WARN
if %errorlevel% neq 0 (
    echo [ERROR] PyInstaller failed for asr-app.
    pause
    exit /b 1
)
rename "%DIST_DIR%\asr-app" "Qwen3-ASR"
move "%DIST_DIR%\Qwen3-ASR" "%APP_OUT%" >nul 2>&1
call :assemble "%APP_OUT%"
(
echo @echo off
echo cd /d "%%~dp0"
echo set CTX_SIZE=32768
echo set CHUNK_SECONDS=25
echo set KV_QUANT=q8_0
echo echo.
echo echo ASR Transcription App
echo echo http://127.0.0.1:8000
echo echo.
echo asr-app.exe
echo pause
) > "%APP_OUT%\start.bat"
echo Browser app ready: %APP_OUT%
exit /b 0


:: ====================================================================
:: Desktop shell: asr-shell.exe (self-contained WebView2 window; no
:: external browser needed). Output: Qwen3-ASR-Shell\
:: ====================================================================
:build_shell
echo.
echo === Building desktop shell (asr-shell.exe, WebView2) ===
call %PYCMD% -m PyInstaller shell\asr-shell.spec ^
    --distpath "%DIST_DIR%" ^
    --workpath "%BUILD_DIR%" ^
    --noconfirm ^
    --log-level=WARN
if %errorlevel% neq 0 (
    echo [ERROR] PyInstaller failed for asr-shell.
    pause
    exit /b 1
)
rename "%DIST_DIR%\asr-shell" "Qwen3-ASR-Shell"
move "%DIST_DIR%\Qwen3-ASR-Shell" "%SHELL_OUT%" >nul 2>&1
call :assemble "%SHELL_OUT%"
echo Desktop shell ready: %SHELL_OUT%
echo   (double-click asr-shell.exe; requires the WebView2 Runtime)
exit /b 0


:: ====================================================================
:: Shared assembly: copy the runtime assets the frozen backend resolves
:: relative to the exe dir (see backend.main._resolve_app_root). Arg %1
:: is the target distribution folder.
:: ====================================================================
:assemble
set "TARGET=%~1"
if exist "%ROOT%frontend\dist" (
    echo       Copying frontend\dist\ ...
    xcopy "%ROOT%frontend\dist" "%TARGET%\frontend\dist\" /e /i /q /y >nul
)
if exist "%ROOT%bin" (
    echo       Copying bin\ ...
    xcopy "%ROOT%bin" "%TARGET%\bin\" /e /i /q /y >nul
)
if exist "%ROOT%models" (
    echo       Copying models\ ...
    xcopy "%ROOT%models" "%TARGET%\models\" /e /i /q /y >nul
)
mkdir "%TARGET%\data\history" 2>nul
exit /b 0
