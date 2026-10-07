# Qwen3-ASR Transcription App

English | [简体中文](README.zh-CN.md)

Speech-to-text on a local NVIDIA GPU. The app loads Qwen3-ASR-1.7B as a GGUF through `llama-server`, transcribes audio or video, and exports the result as SRT, VTT, ASS, TXT or JSON. Word-level timestamps are optional. It runs either in a browser tab or in its own WebView2 window.

## What it does

Input is a single file or a whole folder. Audio is accepted as WAV, MP3, FLAC, OGG or M4A; video as MP4, MKV, MOV, AVI, WebM, FLV, M4V, WMV, MPG, MPEG, TS, 3GP, VOB and OGV. For video, the soundtrack is extracted to MP3 once and cached under `data/audio_cache/`, so re-running the same file skips ffmpeg.

Qwen3-ASR encodes audio as a long run of multimodal tokens, roughly 600 per 10 seconds, so a 40-minute recording does not fit the context window. Long input is cut into 25 s chunks with a 1.5 s overlap, each chunk is transcribed on its own, and the pieces are stitched back together with corrected timestamps. A chunk that fails is retried and then bisected; the overlap at each seam is removed from the merged text rather than duplicated.

Cutting on time is a poor way to make subtitles, so the merged word stream is re-segmented by a scoring model that looks at pause length, punctuation, filler words, and line-breaking rules for both Latin and CJK text. CJK lines get their own tighter limits.

Alignment is optional and gets its own backend. On GPU it runs Qwen3-ForcedAligner-0.6B through the `qwen-asr` stack; on CPU it uses a CrispASR DLL over ctypes. Neither is required. When one is missing, timestamps fall back to the ASR's own segment boundaries instead of failing the job.

Otherwise:

- models in `models/asr/` are hot-swappable from the UI, no restart
- the batch queue persists, so a crash or a closed window resumes where it stopped
- RTF, elapsed time, character and word counts, and chunk count are reported per job
- language is auto-detected unless you force it
- the interface is a glass-panel layout, keyboard-navigable, with reduced-motion support

## Architecture

```
Frontend (React + Vite + Tailwind)
        |
        v
Backend (Python FastAPI)
        |
        +-- llama-server          (Qwen3-ASR-1.7B GGUF, GPU)
        |       ASR inference, spawned at boot, random per-process API key
        |
        +-- Aligner backend       (GPU: qwen-asr / CPU: CrispASR DLL)
        |       Word-level forced alignment, on demand
        |
        +-- Queue worker          (serial asyncio task)
                Drains the persisted batch queue, reusing the
                transcribe pipeline (retry / cancel / align / history)
```

| Component | Stack | Role |
|-----------|-------|------|
| llama-server | C++ binary (CUDA) | ASR inference, `--api-key` protected |
| Aligner (GPU) | Python / qwen-asr | word timestamps from `models/aligner/official` |
| CrispASR | C++ DLL (CPU) | forced alignment fallback via the `align_words` ABI |
| Queue worker | asyncio task | serial batch transcription, state in `data/queue/` |
| Backend | Python 3.13 / FastAPI / httpx | API gateway, chunking, orchestration, security headers |
| Frontend | React 19 / TypeScript / Vite / Tailwind | UI, queue drawer, stats, export |

## Prerequisites

- Windows 10 or 11
- an NVIDIA GPU with CUDA 12.4 or newer
- [Miniconda](https://docs.conda.io/en/latest/miniconda.html) or Anaconda
- Node.js 22+, only needed to build the frontend or run CI
- **ffmpeg** reachable on `PATH` (chunking and video track extraction; it comes with the conda env)
- `pywebview`, only for the desktop shell, already in `requirements.txt`

## Configuration

### GPU and performance

Context size, KV quantization and the layer count are chosen from detected VRAM. `nvidia-smi` is queried at startup, and 8 GB is assumed when it is unavailable. Any of them can be overridden with an environment variable before launch.

| VRAM | ctx_size | KV quant |
|------|----------|----------|
| up to 4.5 GB | 16384 | `q4_0` |
| up to 8.5 GB | 32768 | `q8_0` |
| above 8.5 GB | 32768 | `f16` |

| Variable | Default | Description |
|----------|---------|-------------|
| `VRAM_GB` | auto-detected | Skip detection and feed this number to the table above. |
| `CTX_SIZE` | per table | llama-server context window. |
| `KV_QUANT` | per table | KV-cache quantization: `q8_0`, `q4_0`, `f16`. |
| `NGL` | `99` | Layers offloaded to the GPU. |
| `THREADS` | auto | llama-server CPU threads. |
| `CHUNK_SECONDS` | `25` | Chunk length in seconds. |
| `CHUNK_OVERLAP` | `1.5` | Overlap between neighbouring chunks, so a word spanning a cut keeps context. `0` makes chunks contiguous. |

### Transcription

| Variable | Default | Description |
|----------|---------|-------------|
| `ASR_TEMPERATURE` | `0` | Sampling temperature for llama-server. |
| `ASR_LANGUAGE` | _(empty)_ | Force a language instead of auto-detection. |
| `ASR_MAX_RETRIES` | `2` | Attempts per chunk before it is bisected. |
| `ASR_RESPLIT` | `1` | Bisect a failing chunk into halves until it passes. Set `0` to give up on it instead. |
| `ASR_PROMPT_MAX_CHARS` | `200` | Carry-over prompt from one chunk to the next. |

### CPU aligner

The CrispASR DLL loads the aligner model per call, so several chunks are aligned in parallel through a thread pool (ctypes releases the GIL during the C call). Each concurrent call holds a roughly 262 MB model buffer, which is why workers are capped.

| Variable | Default | Description |
|----------|---------|-------------|
| `ALIGN_N_THREADS` | `4` | Threads per alignment call. |
| `ALIGN_MAX_WORKERS` | `3` | Concurrent alignment calls, clamped to 1–4. |
| `ALIGN_MAX_CHARS` | `400` | Text longer than this is truncated before CPU alignment. The aligner's attention matrix grows with the square of the sequence length, and a hallucinating model can produce enough text to exhaust RAM. |

### Limits and cache

| Variable | Default | Description |
|----------|---------|-------------|
| `ASR_MAX_UPLOAD_BYTES` | 2 GiB | Per-file upload limit, mirrored client-side. |
| `ASR_MAX_AUDIO_DURATION` | 12 h | Longest accepted media. |
| `ASR_MAX_ALIGN_TEXT_CHARS` | `50000` | Per-request text cap for `/api/align`. |
| `ASR_AUDIO_CACHE_MAX_BYTES` | 4 GiB | LRU ceiling for `data/audio_cache/`. |
| `ASR_AUDIO_CACHE_MAX_ENTRIES` | `200` | Entry ceiling for the same cache. |

### Batch queue

| Variable | Default | Description |
|----------|---------|-------------|
| `ASR_QUEUE_MAX_ENTRIES` | `500` | Maximum queued items. |
| `ASR_QUEUE_MAX_BYTES` | 16 GiB | Maximum total staged upload size. |

### Server and shell

| Variable | Default | Description |
|----------|---------|-------------|
| `HOST` | `127.0.0.1` | Bind address. `0.0.0.0` shares the app on the LAN, with no authentication. Only do that on a network you trust. |
| `PORT` | `8000` | Listen port. The desktop shell picks a free loopback port instead. |
| `ASR_ALLOWED_HOSTS` | _(empty)_ | Comma-separated `Host` names to accept when bound to a non-loopback address. Empty means any Host is accepted, and drive-by requests are still stopped by the Origin and `Sec-Fetch-Site` checks. Set it to pin a LAN share to one hostname. |
| `SKIP_LLAMA` | `0` | `1` starts the API without spawning llama-server, for UI work and tests. |
| `ASR_NO_BROWSER` | `0` | `1` suppresses the automatic browser open. |
| `ALIGNER_BACKEND` | `gpu` | `gpu` needs torch and `models/aligner/official`; `cpu` uses the CrispASR DLL. An unrecognized value falls back to `cpu`. |
| `SHELL_MODE`, `ASR_SHELL_TOKEN` | unset | Set by the packaged shell to turn on its HttpOnly-cookie handshake. Not meant to be set by hand. |
| `ASR_SHELL_SMOKE`, `ASR_SHELL_DEBUG` | unset | Shell development switches: auto-close after N seconds, and verbose logging. |

Every response carries a CSP with `script-src 'self'` and `media-src blob:`, plus `nosniff` and `no-referrer`. Origin and Host checks only engage on a non-loopback bind, since a same-origin browser app sends no `Sec-Fetch-Site` worth gating on localhost.

## Forced alignment

Word-level timestamps come from aligning the transcript back against the audio. Pick a backend with `ALIGNER_BACKEND`.

**GPU** (default) runs Qwen3-ForcedAligner through the `qwen-asr` transformers stack. It needs `torch`, `transformers` and `qwen-asr`, none of which are installed by default:

```
pip install qwen-asr torch transformers
```

and the model files under `models/aligner/official/` (safetensors, about 1.8 GB). The GPU backend unloads the ASR model from VRAM before loading the aligner, so a 4 GB card can run both in turn but not at once.

**CPU** calls the [CrispASR](https://github.com/CrispStrobe/CrispASR) C++ runtime through `bin/crispasr/crispasr.dll`, with the GGUF at `models/aligner/qwen3-forced-aligner-0.6b-q8_0.gguf` (about 940 MB, from [cstr/qwen3-forced-aligner-0.6b-GGUF](https://huggingface.co/cstr/qwen3-forced-aligner-0.6b-GGUF)). It needs no VRAM and leaves the ASR model alone. A custom DLL is necessary because llama-server does not support the `qwen3asr` GGUF architecture.

Whichever you pick, it is probed at startup and the app steps down GPU, then CPU, then plain ASR segment timestamps when a dependency or file is missing.

## Batch transcription queue

Files are transcribed one at a time by a serial worker, because one GPU holds one model.

You enqueue from the *Batch* tab of the upload dialog (folder picker or multi-select), or by dropping several files anywhere in the window while a job is running. Each file is staged under `data/queue/uploads/` under a random id.

The queue drawer supports reordering by drag or keyboard, sorting by name, size or time added, pausing and resuming the worker, retrying failures, and clearing finished items. Deleting the item that is currently running cancels it cooperatively at the next chunk boundary.

State lives in `data/queue/queue.json`, written atomically with a version counter so polling can stay cheap. A run interrupted by a crash or a closed window comes back as *queued* on the next start. Finished jobs land in the normal history.

## Project Structure

```
Qwen3-ASR1.7B-TsGUI/
├── bin/                                  # llama-server binary + CUDA DLLs + CrispASR (gitignored)
│   └── crispasr/crispasr.dll             # CPU aligner runtime
├── models/                               # GGUF + aligner model files (gitignored)
│   ├── asr/                              # Qwen3-ASR GGUFs, hot-swappable via /api/models
│   └── aligner/                          # aligner GGUF (CPU) + official/ (GPU)
├── backend/
│   ├── main.py                           # FastAPI app: transcribe, align, models, history, cache
│   ├── llamarunner.py                    # llama-server subprocess (API key, health)
│   ├── queue_api.py                      # batch queue router + serial worker
│   ├── queue_store.py                    # persisted queue state, atomic JSON, crash recovery
│   ├── forced_aligner.py                 # CPU aligner over the CrispASR DLL
│   ├── aligner_gpu.py                    # GPU aligner over qwen-asr
│   ├── audio_chunk.py                    # ffmpeg chunking (the VAD planner here is not wired in yet)
│   ├── resegment.py                      # word stream to subtitle lines, scoring and merging
│   ├── text_clean.py                     # ASR artifact stripping, chunk-seam de-duplication
│   ├── history.py                        # per-job JSON history under data/history/
│   ├── requirements.txt
│   └── test_*.py                         # main, queue, vad, subtitles, shell
├── frontend/
│   ├── index.html
│   ├── vite.config.ts
│   └── src/
│       ├── App.tsx                       # layout, single/batch state machine, drag-drop
│       ├── api.ts                        # fetch wrappers for transcribe, queue, models, history
│       ├── types.ts                      # API response interfaces
│       ├── index.css                     # Tailwind entry, design tokens
│       ├── hooks/                        # useReadiness, useJobStatus, useQueue, useElapsed
│       ├── utils/                        # subtitle, format, fileTypes, audioClock
│       └── components/                   # UploadZone, BatchUploadZone, QueuePanel, BootOverlay,
│                                         # TranscriptPanel, AudioPlayer, ProgressBar, StatsPanel,
│                                         # ExportBar, HistoryPanel, ModelInfo, icons
├── shell/
│   ├── shell_main.py                     # pywebview/WebView2 desktop shell, backend in-process
│   ├── asr-shell.spec                    # PyInstaller spec (windowed asr-shell.exe)
│   └── start-shell.bat                   # development launcher
├── tools/
│   ├── build_embed.py                    # embeddable-Python distribution builder
│   └── subcompare.py                     # subtitle WER/CER comparison
├── data/                                 # runtime state (gitignored): history, queue,
│                                         # audio_cache, chunk_cache, jobs, logs, webview_profile
├── .github/workflows/ci.yml              # frontend typecheck+build, backend mocked tests
├── environment.yml                       # conda environment
├── requirements-runtime.txt              # deps for the embedded runtime, no dev or test extras
├── run.py                                # uvicorn entrypoint, opens the browser once ready
├── asr-app.spec                          # PyInstaller spec (browser-mode asr-app.exe)
├── setup.bat                             # create the conda env and install dependencies
├── start.bat                             # launch in dev, browser mode
├── build.bat                             # build.bat [app|shell|both]
├── build-embed.bat                       # one-click embeddable build
├── README.md
└── README.zh-CN.md
```

## Setup

```powershell
setup.bat
```

creates the conda environment, installs dependencies and checks for the files the app expects. To do it by hand:

```powershell
conda env create -f environment.yml
conda activate qwen3asr
cd frontend
npm install
```

Then download `llama-b9637-bin-win-cuda-12.4-x64.zip` from the [llama.cpp releases](https://github.com/ggml-org/llama.cpp/releases/tag/b9637) and extract it into `bin/`.

## Running

Browser mode, which is what you want while developing:

```powershell
start.bat
```

This activates the conda env, builds the frontend and serves on http://localhost:8000.

The desktop shell runs the backend in-process inside a 1280×820 WebView2 window:

```powershell
shell\start-shell.bat
```

It binds a random loopback port, passes the page a one-time token that is exchanged for an HttpOnly cookie, forwards external links to the system browser instead of navigating the window, and holds a named mutex so a second launch cannot race the port, the queue or the browser profile. Logs go to `data/logs/asr.log`. If WebView2 is not available it falls back to the system browser.

Without the batch files:

```powershell
conda activate qwen3asr
cd frontend && npm run build && cd ..
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

For frontend work with hot reload, start the API with `SKIP_LLAMA=1` in one terminal and `npm run dev` in another. Vite proxies `/api` to port 8000.

```powershell
# Terminal 1
conda activate qwen3asr
$env:SKIP_LLAMA = "1"
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000

# Terminal 2
cd frontend
npm run dev
```

### Packaged builds

```powershell
build.bat app      # PyInstaller, Qwen3-ASR\asr-app.exe (system browser)
build.bat shell    # PyInstaller, Qwen3-ASR-Shell\asr-shell.exe (WebView2 window)
build.bat both
build-embed.bat    # tools/build_embed.py into Qwen3-ASR-Embed\
```

The embeddable build is not a PyInstaller bundle. It ships a standalone Python 3.12 runtime with the source left editable and `pip` working, which is how you add the GPU alignment dependencies after the fact:

```powershell
runtime\python.exe -m pip install qwen-asr torch transformers
```

## API

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/health` | `backend`, `llama_server`, `aligner_available`, `aligner_running`, `aligner_backend` |
| `GET` | `/api/readiness` | boot sequence phases: model load, warmup, aligner check |
| `POST` | `/api/readiness/retry` | re-run the boot sequence after a terminal failure |
| `GET` | `/api/status` | current job: `status`, `progress`, `message` |
| `POST` | `/api/transcribe` | upload media, returns `text`, `segments[]`, `stats`, `history_id` |
| `POST` | `/api/abort` | cancel the running transcription at the next chunk boundary |
| `POST` | `/api/align` | standalone alignment, audio plus text in, word timestamps out |
| `GET` | `/api/models` | GGUFs in `models/asr/`, the current model, and tuning |
| `POST` | `/api/models/switch` | hot-swap the ASR model, refused while busy |
| `GET` | `/api/history` | history list |
| `GET`, `DELETE` | `/api/history/{hid}` | one record |
| `DELETE` | `/api/history` | clear history |
| `GET`, `DELETE` | `/api/audio-cache` | extracted-audio cache, list or clear |
| `GET` | `/api/audio-cache/{name}` | stream one cached file, used by the built-in player |
| `POST` | `/api/queue/items` | enqueue files (multipart, multiple per request) |
| `GET` | `/api/queue` | snapshot: `items[]`, `paused`, `active_id`, `version` |
| `POST` | `/api/queue/reorder`, `/api/queue/sort` | manual order, rule-based order |
| `DELETE` | `/api/queue/items/{qid}` | remove; cancels the item if it is running |
| `POST` | `/api/queue/items/{qid}/retry` | re-queue a failed or cancelled item |
| `POST` | `/api/queue/pause`, `/api/queue/resume` | worker control |
| `DELETE` | `/api/queue/finished` | drop done and cancelled items |

`/api/transcribe` takes a required `file` and an optional `align` boolean, default `false`.

```powershell
curl -X POST http://localhost:8000/api/transcribe -F "file=@audio.wav"
curl -X POST http://localhost:8000/api/transcribe -F "file=@audio.wav" -F "align=true"
curl -X POST http://localhost:8000/api/queue/items -F "files=@a.mp3" -F "files=@b.mp4"
```

The response looks like this:

```json
{
  "text": "full transcription text",
  "segments": [
    {"start": 0.0, "end": 3.5, "text": "segment text"}
  ],
  "stats": {
    "audio_duration": 120.5,
    "total_time": 15.3,
    "rtf": 0.127,
    "asr_time": 12.1,
    "align_time": 3.2,
    "char_count": 350,
    "word_count": 72,
    "segment_count": 8,
    "chunk_count": 5,
    "aligner_used": true,
    "model": "Qwen3-ASR-1.7B-Q8_0",
    "aligner_model": "Qwen3-ForcedAligner-0.6B"
  }
}
```

## Tests

```powershell
conda activate qwen3asr
$env:SKIP_LLAMA = "1"
python -m pytest backend\test_main.py -v
python -m pytest backend\test_queue.py backend\test_vad.py -v
python -m pytest backend\test_subtitles.py backend\test_shell.py -v

cd frontend
npm run typecheck
npm run build
```

`test_main.py` mocks llama-server, ffmpeg and the aligners, so it runs on a machine with no GPU. CI (`.github/workflows/ci.yml`) runs the frontend typecheck and build on Node 22, plus `compileall` and that mocked test file on Python 3.13, for every push and PR.

## Comparing subtitle output

```powershell
python tools\subcompare.py ref.srt hyp.srt
python tools\subcompare.py ref.srt hyp.vtt --html report.html
python tools\subcompare.py ref.srt hyp.srt --min-iou 0.3
```

`subcompare.py` parses SRT, VTT and ASS, guesses the encoding, pairs segments by
time range and prints WER, CER and start/end drift in the terminal; `--html`
writes the same thing as a report, and `--min-iou` drops pairs whose time
overlap is too small to count as the same line. It is how you check whether a
change to chunking or resegmentation actually helped.

## Model files

Models are not in the repo. Put them under `models/` and the backend finds them at startup, disabling whatever is absent rather than refusing to boot. Any extra `.gguf` dropped in `models/asr/` shows up in `/api/models` and can be hot-swapped.

| File | Location | Size | Required | |
|------|----------|------|----------|-|
| `Qwen3-ASR-1.7B-Q8_0.gguf` | `models/asr/` | ~2.2 GB | yes | the ASR model, Q8 |
| `mmproj-Qwen3-ASR-1.7B-Q8_0.gguf` | `models/asr/` | ~356 MB | yes | its multimodal projector |
| `qwen3-forced-aligner-0.6b-q8_0.gguf` | `models/aligner/` | ~940 MB | no | aligner for the CPU backend |
| Qwen3-ForcedAligner-0.6B (safetensors) | `models/aligner/official/` | ~1.8 GB | no | aligner for the GPU backend |

ASR: [ggml-org/Qwen3-ASR-1.7B-GGUF](https://huggingface.co/ggml-org/Qwen3-ASR-1.7B-GGUF)
Aligner GGUF: [cstr/qwen3-forced-aligner-0.6b-GGUF](https://huggingface.co/cstr/qwen3-forced-aligner-0.6b-GGUF)

## Limits worth knowing

- One transcription at a time. The queue exists so you do not have to babysit the GPU, not to parallelize it.
- `HOST=0.0.0.0` is an unauthenticated share. The Origin and Host checks stop drive-by browser requests; they do not stop someone on your network who wants to read your history or queue up files.
- Windows only. The launchers are batch files, the shell is WebView2, and the mutex and the aligner DLL are `kernel32` and Windows-specific paths.
- The 1.5 s chunk overlap reduces boundary errors but does not remove them. A word that straddles a cut can still come out wrong.
- On the CPU backend, text past `ALIGN_MAX_CHARS` is truncated before alignment, so those words keep segment-level timestamps instead of word-level ones.
- Uploads stop at 2 GiB and 12 h by design.

## License

Model files remain under their original Qwen license. The application code is provided as-is.
