@echo off
setlocal
echo.
echo ============================================
echo  ASR Transcription App - Launch
echo ============================================
echo.
set ROOT=%~dp0

where conda >nul 2>&1
if errorlevel 1 (
    echo [ERROR] conda not found. Run setup.bat first.
    pause
    exit /b 1
)

:: Verify the environment exists
conda env list | findstr /b /c:"qwen3-asr " >nul
if errorlevel 1 (
    echo [ERROR] conda environment "qwen3-asr" not found. Run setup.bat first.
    pause
    exit /b 1
)

:: Build frontend (produces frontend/dist, served by backend StaticFiles)
echo [1/2] Building frontend...
where npm >nul 2>&1
if errorlevel 1 (
    echo [ERROR] npm not found. Install Node.js 24+ and run setup.bat.
    pause
    exit /b 1
)
pushd "%ROOT%frontend"
call npm run build
if errorlevel 1 (
    echo [ERROR] Frontend build failed.
    popd
    pause
    exit /b 1
)
popd

:: Start backend (spawns llama-server automatically). Low-VRAM defaults.
echo [2/2] Starting backend on http://127.0.0.1:8000 ...
set CTX_SIZE=32768
set CHUNK_SECONDS=25
set KV_QUANT=q8_0
conda run -n qwen3-asr --no-capture-output python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
pause
