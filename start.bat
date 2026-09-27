@echo off
setlocal
echo.
echo ============================================
echo  ASR Transcription App - Launch
echo ============================================
echo.
set ROOT=%~dp0
set PYCACHE=%ROOT%.qwen3asr_python.cache

:: ==== Pre-flight file checks (mirrors setup.bat) ====
:: Fail fast in milliseconds before any conda/python spawn, so missing
:: downloads don't surface minutes later as a llama-server boot error.
if not exist "%ROOT%bin\llama-server.exe" (
    echo [ERROR] bin\llama-server.exe missing - download the llama.cpp CUDA
    echo         build into bin\ ^(see README^) or run setup.bat.
    pause
    exit /b 1
)
if not exist "%ROOT%models\asr\Qwen3-ASR-1.7B-Q8_0.gguf" (
    echo [ERROR] models\asr\Qwen3-ASR-1.7B-Q8_0.gguf missing - see README for download links.
    pause
    exit /b 1
)
if not exist "%ROOT%models\asr\mmproj-Qwen3-ASR-1.7B-Q8_0.gguf" (
    echo [ERROR] models\asr\mmproj-Qwen3-ASR-1.7B-Q8_0.gguf missing - see README for download links.
    pause
    exit /b 1
)
if not exist "%ROOT%bin\crispasr\crispasr.dll" if not exist "%ROOT%bin\crispasr.dll" (
    echo [OPTIONAL] crispasr.dll missing - forced alignment will be skipped.
)
if not exist "%ROOT%models\aligner\qwen3-forced-aligner-0.6b-q8_0.gguf" (
    echo [OPTIONAL] aligner model missing - forced alignment will be skipped.
)

:: ==== Resolve the environment's python ====
:: Fast path: a cached absolute python.exe path skips conda entirely
:: (conda env list + conda run cost several seconds per launch). The
:: cache is rebuilt automatically when missing or stale; delete
:: .qwen3asr_python.cache to force re-resolution.
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

:: Verify the environment exists
conda env list | findstr /b /c:"qwen3asr " >nul
if errorlevel 1 (
    echo [ERROR] conda environment "qwen3asr" not found. Run setup.bat first.
    pause
    exit /b 1
)

:: Resolve and cache the env's python.exe for subsequent fast launches.
for /f "usebackq delims=" %%i in (`conda run -n qwen3asr --no-capture-output python -c "import sys;print(sys.executable)"`) do set "PYEXE=%%i"
if not defined PYEXE (
    echo [ERROR] Failed to resolve python from the qwen3asr environment.
    pause
    exit /b 1
)
(echo %PYEXE%)>"%PYCACHE%"

:python_ready
echo Using Python: %PYEXE%

:: Minimal env activation: put the env's own dirs first on PATH so tools
:: installed by conda (ffmpeg in Library\bin) resolve exactly as they do
:: under "conda run", without paying conda's startup cost.
for %%i in ("%PYEXE%") do set "ENVDIR=%%~dpi"
set "PATH=%ENVDIR%;%ENVDIR%Library\bin;%ENVDIR%Scripts;%PATH%"

:: Build frontend (produces frontend/dist, served by backend StaticFiles).
:: Skip the rebuild when a build already exists so repeat launches are fast and
:: don't require npm/Node at run time. Delete frontend\dist to force a rebuild.
if exist "%ROOT%frontend\dist\index.html" (
    echo [1/2] Frontend already built - skipping rebuild.
) else (
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
)

:: Start backend (spawns llama-server automatically). Low-VRAM defaults.
:: run.py also auto-opens the browser once the boot sequence is ready
:: (set ASR_NO_BROWSER=1 to opt out).
echo [2/2] Starting backend on http://127.0.0.1:8000 ...
echo       Loading Python modules - the FIRST start can take a while
echo       (antivirus scan). Please wait; do NOT press Ctrl+C or click
echo       inside this window while it is loading.
set CTX_SIZE=32768
set CHUNK_SECONDS=25
set KV_QUANT=q8_0
"%PYEXE%" "%ROOT%run.py"
set EXITCODE=%errorlevel%
echo.
if %EXITCODE% neq 0 (
    echo [WARN] Backend exited with code %EXITCODE%.
    echo        - "KeyboardInterrupt" in the traceback means the process
    echo          received Ctrl+C ^(accidental keypress or a click inside a
    echo          QuickEdit console selects text and can interrupt it^).
    echo          Just run start.bat again.
    echo        - For import/module errors, re-run setup.bat to repair the
    echo          conda environment, or delete .qwen3asr_python.cache if
    echo          the environment was recreated at a different path.
)
pause
