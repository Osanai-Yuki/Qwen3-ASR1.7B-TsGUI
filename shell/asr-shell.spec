# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the WebView2 desktop shell (windowed, no console).
# The plain browser build (asr-app.spec / run.py) remains untouched so both
# distribution modes coexist. Build from the project root:
#   python -m PyInstaller shell/asr-shell.spec --distpath dist --workpath build
from pathlib import Path

block_cipher = None

# SPECPATH points at shell/; the project root is one level up.
ROOT = Path(SPECPATH).parent

datas = []

a = Analysis(
    [str(ROOT / "shell" / "shell_main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "backend",
        "backend.main",
        "backend.llamarunner",
        "backend.forced_aligner",
        "backend.aligner_gpu",
        "backend.audio_chunk",
        "backend.resegment",
        "backend.text_clean",
        "backend.history",
        "backend.queue_store",
        "backend.queue_api",
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
        "fastapi",
        "fastapi.staticfiles",
        "httpx",
        "python_multipart",
        "numpy",
        # pywebview EdgeChromium (WebView2) backend + pythonnet loader chain.
        "webview",
        "webview.platforms.edgechromium",
        "webview.platforms.winforms",
        "clr_loader",
        "pythonnet",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "pytest",
        "matplotlib",
        "tkinter",
        "scipy",
        "pandas",
        "PIL",
        "IPython",
        "jupyter",
        # The GPU aligner stack (torch/transformers/qwen_asr) is loaded
        # lazily at runtime via importlib.find_spec + in-function imports
        # (backend/aligner_gpu.py). Bundling it balloons the build to ~5 GB
        # and drags in a large CVE surface. Excluding it makes the frozen
        # shell degrade GPU alignment to CPU CrispASR (or skip) gracefully;
        # users who want GPU alignment run from the conda env instead.
        # NOTE: keep this list to the top-level ML packages only — excluding
        # shared transitive deps (sympy/tokenizers/safetensors/…) breaks the
        # frozen uvicorn/asyncio runtime and hangs backend startup.
        "torch",
        "torchaudio",
        "torchvision",
        "transformers",
        "qwen_asr",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="asr-shell",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,  # windowed app; logs go to data/logs/asr.log
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="asr-shell",
)
