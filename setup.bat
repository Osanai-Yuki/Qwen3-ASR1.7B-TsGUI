@echo off
setlocal
echo.
echo ============================================
echo  ASR Transcription App - Environment Setup
echo ============================================
echo.
set ROOT=%~dp0

:: ── 1. Conda environment ───────────────────────────────────────────
where conda >nul 2>&1
if errorlevel 1 (
    echo [ERROR] conda not found on PATH. Install Miniconda/Anaconda first.
    pause
    exit /b 1
)

echo [1/4] Creating conda environment "qwen3asr"...
conda env list | findstr /b /c:"qwen3asr " >nul
if errorlevel 1 (
    conda env create -f "%ROOT%environment.yml"
    if errorlevel 1 (
        echo [ERROR] Failed to create conda environment.
        pause
        exit /b 1
    )
) else (
    echo       Environment already exists, updating dependencies...
    conda env update -f "%ROOT%environment.yml" --prune
)

:: ── 2. Frontend dependencies ───────────────────────────────────────
echo [2/4] Installing frontend dependencies...
where npm >nul 2>&1
if errorlevel 1 (
    echo [ERROR] npm not found. Install Node.js 24+ first.
    pause
    exit /b 1
)
pushd "%ROOT%frontend"
call npm install
if errorlevel 1 (
    echo [ERROR] npm install failed.
    popd
    pause
    exit /b 1
)
popd

:: ── 3. Required files check ────────────────────────────────────────
echo [3/4] Checking required files...
set MISSING=0
if not exist "%ROOT%bin\llama-server.exe" (
    echo   [MISSING] bin\llama-server.exe  - download llama.cpp CUDA build into bin\
    set MISSING=1
)
if not exist "%ROOT%models\asr\Qwen3-ASR-1.7B-Q8_0.gguf" (
    echo   [MISSING] models\asr\Qwen3-ASR-1.7B-Q8_0.gguf  - main ASR model
    set MISSING=1
)
if not exist "%ROOT%models\asr\mmproj-Qwen3-ASR-1.7B-Q8_0.gguf" (
    echo   [MISSING] models\asr\mmproj-Qwen3-ASR-1.7B-Q8_0.gguf  - ASR projector
    set MISSING=1
)
if not exist "%ROOT%bin\crispasr\crispasr.dll" if not exist "%ROOT%bin\crispasr.dll" (
    echo   [OPTIONAL] crispasr.dll missing - forced alignment will be skipped
)
if not exist "%ROOT%models\aligner\qwen3-forced-aligner-0.6b-q8_0.gguf" (
    echo   [OPTIONAL] aligner model missing - forced alignment will be skipped
)

:: ── 4. ffmpeg check ────────────────────────────────────────────────
echo [4/4] Checking ffmpeg...
where ffmpeg >nul 2>&1
if errorlevel 1 (
    echo   [MISSING] ffmpeg not on PATH - required for audio chunking
    echo           environment.yml installs it via conda; activate the env first.
    set MISSING=1
)

echo.
if "%MISSING%"=="1" (
    echo ============================================
    echo  Setup complete with MISSING items above.
    echo  See README for download links.
    echo ============================================
) else (
    echo ============================================
    echo  Setup complete. Run start.bat to launch.
    echo ============================================
)
echo.
pause
