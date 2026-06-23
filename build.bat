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
set OUT_DIR=%ROOT%Qwen3-ASR

:: Resolve the conda environment's python. Prefer a local override, then
:: conda run (works regardless of install path), then fall back to plain
:: python if conda is absent.
if defined PYTHON_EXE (
    set "PYCMD=%PYTHON_EXE%"
) else (
    where conda >nul 2>&1
    if not errorlevel 1 (
        set "PYCMD=conda run -n qwen3-asr --no-capture-output python"
    ) else (
        set "PYCMD=python"
    )
)

echo Using Python: %PYCMD%
echo Verifying environment...
%PYCMD% -c "import fastapi, uvicorn, numpy, PyInstaller" >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Required packages missing in the target Python environment.
    echo         Activate qwen3-asr or run: conda env create -f environment.yml
    pause
    exit /b 1
)

echo [1/6] Cleaning previous build...
if exist "%BUILD_DIR%" rmdir /s /q "%BUILD_DIR%"
if exist "%DIST_DIR%" rmdir /s /q "%DIST_DIR%"
if exist "%OUT_DIR%" rmdir /s /q "%OUT_DIR%"

echo [2/6] Building frontend...
cd /d "%ROOT%frontend"
call npm run build
if %errorlevel% neq 0 (
    echo [ERROR] Frontend build failed.
    pause
    exit /b 1
)
cd /d "%ROOT%"

echo [3/6] Running PyInstaller...
call %PYCMD% -m PyInstaller asr-app.spec ^
    --distpath "%DIST_DIR%" ^
    --workpath "%BUILD_DIR%" ^
    --noconfirm ^
    --log-level=WARN
if %errorlevel% neq 0 (
    echo [ERROR] PyInstaller failed.
    pause
    exit /b 1
)

echo [4/6] Assembling distribution folder...
rename "%DIST_DIR%\asr-app" "Qwen3-ASR"
move "%DIST_DIR%\Qwen3-ASR" "%OUT_DIR%" >nul 2>&1

:: Frontend static files
if exist "%ROOT%frontend\dist" (
    echo       Copying frontend\dist\...
    xcopy "%ROOT%frontend\dist" "%OUT_DIR%\frontend\dist\" /e /i /q /y >nul
)

:: External runtime folders
if exist "%ROOT%bin" (
    echo       Copying bin\...
    xcopy "%ROOT%bin" "%OUT_DIR%\bin\" /e /i /q /y >nul
)

if exist "%ROOT%models" (
    echo       Copying models\...
    xcopy "%ROOT%models" "%OUT_DIR%\models\" /e /i /q /y >nul
)

:: Data directory
mkdir "%OUT_DIR%\data\history" 2>nul

echo [5/6] Creating start.bat...
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
) > "%OUT_DIR%\start.bat"

echo [6/6] Cleaning build artifacts...
rmdir /s /q "%BUILD_DIR%" 2>nul
rmdir /s /q "%DIST_DIR%" 2>nul

echo.
echo ============================================
echo  Build complete: %OUT_DIR%
echo ============================================
echo.
dir /b "%OUT_DIR%"
echo.
for /f "tokens=3" %%a in ('dir /s /-c "%OUT_DIR%" 2^>nul ^| findstr /c:"File(s)"') do (
    set /a "mb=%%a/1048576"
    echo Total size: %%a bytes
)
echo.
pause
