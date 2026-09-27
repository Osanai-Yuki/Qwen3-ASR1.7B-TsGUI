"""Build the embedded-Python distribution of Qwen3-ASR (no PyInstaller).

Layout produced (Qwen3-ASR-Embed/):
    Qwen3-ASR.bat            desktop shell (runtime\\pythonw.exe, no console)
    Qwen3-ASR-Browser.bat    browser mode  (runtime\\python.exe run.py)
    Qwen3-ASR-Debug.bat      shell with a console for troubleshooting
    runtime/                 python-embed + Lib/site-packages (real deps)
    backend/ shell/ run.py   app sources, verbatim (dev/prod parity)
    frontend/dist/ bin/ models/ data/

Why no freezing: sources stay editable, pip stays available inside the
distribution (users can restore GPU alignment in-place with
``runtime\\python.exe -m pip install qwen-asr torch transformers``), and
none of the PyInstaller windowed/hiddenimport pitfalls apply.

Run from the qwen3asr conda env (its pip resolves the wheels):
    python tools/build_embed.py [--skip-assets] [--skip-frontend]
"""
import argparse
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Last 3.12.x with official binaries (security-only releases ship none).
# ABI-compatible (cp312) with the qwen3asr env used for pip --target.
EMBED_VERSION = "3.12.10"
EMBED_ZIP = f"python-{EMBED_VERSION}-embed-amd64.zip"
EMBED_URL = f"https://www.python.org/ftp/python/{EMBED_VERSION}/{EMBED_ZIP}"

OUT = ROOT / "Qwen3-ASR-Embed"
RUNTIME = OUT / "runtime"
SITE = RUNTIME / "Lib" / "site-packages"
CACHE = ROOT / "build" / "embed-cache"

SOURCES = ["backend", "shell", "run.py"]
ASSETS = ["bin", "models"]

ENV_PRESET = "set CTX_SIZE=32768\r\nset CHUNK_SECONDS=25\r\nset KV_QUANT=q8_0\r\n"

LAUNCHERS = {
    # start "" ... lets the .bat window close immediately; pythonw has no
    # console of its own (shell_main handles the None-stdio case).
    "Qwen3-ASR.bat": (
        "@echo off\r\ncd /d \"%~dp0\"\r\n" + ENV_PRESET +
        "start \"\" \"%~dp0runtime\\pythonw.exe\" \"%~dp0shell\\shell_main.py\"\r\n"
    ),
    "Qwen3-ASR-Browser.bat": (
        "@echo off\r\ncd /d \"%~dp0\"\r\n" + ENV_PRESET +
        "echo ASR Transcription App - http://127.0.0.1:8000\r\n"
        "\"%~dp0runtime\\python.exe\" \"%~dp0run.py\"\r\npause\r\n"
    ),
    "Qwen3-ASR-Debug.bat": (
        "@echo off\r\ncd /d \"%~dp0\"\r\n" + ENV_PRESET +
        "echo [debug] shell with console; logs also in data\\logs\\asr.log\r\n"
        "\"%~dp0runtime\\python.exe\" \"%~dp0shell\\shell_main.py\"\r\npause\r\n"
    ),
}


def log(msg: str) -> None:
    print(f"[build-embed] {msg}", flush=True)


def step_frontend(skip: bool) -> None:
    dist = ROOT / "frontend" / "dist" / "index.html"
    if skip and dist.exists():
        log("frontend: skipped (dist exists)")
        return
    log("frontend: npm run build ...")
    npm = shutil.which("npm")
    if not npm:
        raise RuntimeError("npm not found on PATH (install Node.js 24+)")
    subprocess.run([npm, "run", "build"], cwd=ROOT / "frontend", check=True)
    if not dist.exists():
        raise RuntimeError("frontend build produced no dist/index.html")


def step_runtime() -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    zip_path = CACHE / EMBED_ZIP
    if not zip_path.exists():
        log(f"runtime: downloading {EMBED_URL} ...")
        tmp = zip_path.with_suffix(".part")
        urllib.request.urlretrieve(EMBED_URL, tmp)
        tmp.rename(zip_path)
    else:
        log(f"runtime: using cached {zip_path.name}")

    if RUNTIME.exists():
        shutil.rmtree(RUNTIME)
    RUNTIME.mkdir(parents=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(RUNTIME)

    # ._pth fully controls sys.path for the embeddable interpreter (site and
    # script-dir insertion are disabled). ".." puts the dist root on the path
    # so `import backend` works for run.py / shell_main.py alike.
    pth = next(RUNTIME.glob("python3*._pth"))
    pth.write_text(
        f"{pth.stem.replace('._pth', '')}.zip\n.\nLib\\site-packages\n..\n",
        encoding="ascii",
    )
    log(f"runtime: extracted + patched {pth.name}")


def step_deps() -> None:
    log("deps: pip install --target runtime\\Lib\\site-packages ...")
    SITE.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [sys.executable, "-m", "pip", "install",
         "--target", str(SITE),
         "-r", str(ROOT / "requirements-runtime.txt"),
         "--quiet", "--disable-pip-version-check"],
        check=True,
    )


def step_sources() -> None:
    log("sources: copying backend/ shell/ run.py ...")
    for name in SOURCES:
        src = ROOT / name
        dst = OUT / name
        if dst.exists():
            shutil.rmtree(dst) if dst.is_dir() else dst.unlink()
        if src.is_dir():
            shutil.copytree(
                src, dst,
                ignore=shutil.ignore_patterns("__pycache__", "test_*", "*.bat", "*.spec"),
            )
        else:
            shutil.copy2(src, dst)


def step_assets(skip: bool) -> None:
    fe_src = ROOT / "frontend" / "dist"
    fe_dst = OUT / "frontend" / "dist"
    if fe_dst.exists():
        shutil.rmtree(fe_dst)
    shutil.copytree(fe_src, fe_dst)
    log("assets: frontend/dist copied")

    if skip:
        log("assets: bin/ models/ SKIPPED (--skip-assets)")
    else:
        for name in ASSETS:
            src = ROOT / name
            if not src.exists():
                log(f"assets: {name}/ missing in repo - skipped")
                continue
            dst = OUT / name
            if dst.exists():
                shutil.rmtree(dst)
            log(f"assets: copying {name}/ (may take a while) ...")
            shutil.copytree(src, dst)

    (OUT / "data" / "history").mkdir(parents=True, exist_ok=True)


def step_launchers() -> None:
    for name, content in LAUNCHERS.items():
        (OUT / name).write_text(content, encoding="ascii")
    log(f"launchers: {', '.join(LAUNCHERS)}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--skip-assets", action="store_true",
                    help="skip copying bin/ and models/ (fast dev iteration)")
    ap.add_argument("--skip-frontend", action="store_true",
                    help="reuse existing frontend/dist instead of npm build")
    args = ap.parse_args()

    OUT.mkdir(exist_ok=True)
    step_frontend(args.skip_frontend)
    step_runtime()
    step_deps()
    step_sources()
    step_assets(args.skip_assets)
    step_launchers()

    total = sum(f.stat().st_size for f in OUT.rglob("*") if f.is_file())
    log(f"DONE -> {OUT}  ({total / 1048576:,.0f} MB)")
    log("desktop:  Qwen3-ASR.bat   browser: Qwen3-ASR-Browser.bat")
    log("GPU alignment (optional):")
    log("  runtime\\python.exe -m pip install qwen-asr torch transformers")


if __name__ == "__main__":
    main()
