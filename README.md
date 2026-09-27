# Qwen3-ASR Transcription App

CUDA-accelerated automatic speech recognition using the Qwen3-ASR-1.7B GGUF model, with optional word-level forced alignment, batch transcription queue, multi-format subtitle export, real-time performance statistics, and a minimalist glass UI. Runs as a browser app or a WebView2 desktop shell.

## Features

- **Audio & video transcription** — upload audio (WAV, MP3, FLAC, OGG, M4A, …) or video (MP4, MKV, MOV, AVI, WebM, …); video soundtracks are extracted to MP3 once and cached for replay
- **Single & batch mode** — one file at a time, or drop a whole folder into the persistent transcription queue (pause / resume / reorder / retry / skip)
- **Audio chunking** — long audio is split into 25 s chunks with 1.5 s overlap to fit the context window; failed chunks are retried and re-split automatically, and overlapping chunk seams are de-duplicated in the merged text
- **Subtitle-grade segmentation** — chunk words are merged into subtitle lines by a scoring model (pauses, punctuation, filler words, line-boundary rules), with tighter CJK line limits
- **Forced alignment (optional)** — word-level timestamps via a GPU backend (qwen-asr / Qwen3-ForcedAligner-0.6B) or the CPU CrispASR DLL, with automatic fallback to ASR segment timestamps
- **Hot model switching** — swap between GGUF models in `models/asr/` from the UI without restarting
- **Multi-format export** — SRT, VTT, ASS, TXT, JSON
- **Performance statistics** — RTF, processing time, character/word counts, chunk count
- **GPU acceleration** — full CUDA offload via llama-server; VRAM-adaptive context/KV presets, per-process API key
- **Multi-language** — automatic language detection
- **Minimalist UI** — glass panels, semantic design tokens, full keyboard navigation and reduced-motion support

## Architecture

```
Frontend (React + Vite + Tailwind)
        |
        v
Backend (Python FastAPI)
        |
        +── llama-server          (Qwen3-ASR-1.7B GGUF, GPU)
        |       ASR inference, spawned at boot, random per-process API key
        |
        +── Aligner backend       (GPU: qwen-asr / CPU: CrispASR DLL)
        |       Word-level forced alignment, on demand
        |
        +── Queue worker          (serial asyncio task)
                Drains the persisted batch queue, reusing the
                transcribe pipeline (retry / cancel / align / history)
```

| Component | Stack | Role |
|-----------|-------|------|
| **llama-server** | C++ binary (CUDA) | ASR inference, `--api-key` protected |
| **Aligner (GPU)** | Python / qwen-asr | Word timestamps from `models/aligner/official` |
| **CrispASR** | C++ DLL (CPU) | Forced alignment fallback via `align_words` ABI |
| **Queue worker** | asyncio task | Serial batch transcription, persisted in `data/queue/` |
| **Backend** | Python 3.13 / FastAPI / httpx | API gateway, chunking, orchestration, security headers |
| **Frontend** | React 19 / TypeScript / Vite / Tailwind | UI, queue drawer, stats, multi-format export |

## Prerequisites

- Windows 10/11
- NVIDIA GPU with CUDA 12.4+
- [Miniconda](https://docs.conda.io/en/latest/miniconda.html) or Anaconda
- Node.js 22+ (frontend build / CI)
- **ffmpeg** on `PATH` (chunking + video audio extraction)
- `pywebview` (optional, only for the desktop shell — declared in `requirements.txt`)

## Configuration

### GPU / performance

Tuning defaults are **picked automatically from detected VRAM** (override any of them via environment variables before launching):

| VRAM | ctx_size | KV quant |
|------|----------|----------|
| ≤ 4.5 GB | 16384 | `q4_0` |
| ≤ 8.5 GB | 32768 | `q8_0` |
| > 8.5 GB | 32768 | `f16` |

Qwen3-ASR encodes audio as a long sequence of multimodal tokens (~600 tokens per
10 s of audio), so long input is **split into short chunks**, transcribed
independently, and stitched back together with corrected timestamps.

| Variable | Default | Description |
|----------|---------|-------------|
| `VRAM_GB` | auto-detected | Override VRAM detection that drives the presets above. |
| `CTX_SIZE` | per table | llama-server context window. |
| `KV_QUANT` | per table | KV-cache quantization (`q8_0`, `q4_0`, `f16`). |
| `NGL` | `99` | GPU layers offloaded to llama-server. |
| `THREADS` | auto | llama-server CPU threads. |
| `CHUNK_SECONDS` | `25` | Audio chunk length in seconds. |
| `CHUNK_OVERLAP` | `1.5` | Overlap (s) between adjacent chunks so boundary words keep context. |

### Transcription

| Variable | Default | Description |
|----------|---------|-------------|
| `ASR_TEMPERATURE` | `0` | Sampling temperature passed to llama-server. |
| `ASR_LANGUAGE` | _(empty)_ | Force a language hint instead of auto-detection. |
| `ASR_MAX_RETRIES` | `2` | Retries per chunk before re-splitting it into smaller pieces. |
| `ASR_RESPLIT` | `1` | Re-split a failing chunk into halves until it passes (set `0` to disable). |
| `ASR_PROMPT_MAX_CHARS` | `200` | Max carry-over prompt between chunks. |

### Limits & cache

| Variable | Default | Description |
|----------|---------|-------------|
| `ASR_MAX_UPLOAD_BYTES` | 2 GiB | Per-file upload limit (mirrored client-side). |
| `ASR_MAX_AUDIO_DURATION` | 12 h | Maximum accepted media duration. |
| `ASR_MAX_ALIGN_TEXT_CHARS` | `50000` | Per-request aligner text cap. |
| `ASR_AUDIO_CACHE_MAX_BYTES` | 4 GiB | LRU cap of the extracted-audio cache (`data/audio_cache/`). |
| `ASR_AUDIO_CACHE_MAX_ENTRIES` | `200` | Entry cap of the same cache. |

### Batch queue

| Variable | Default | Description |
|----------|---------|-------------|
| `ASR_QUEUE_MAX_ENTRIES` | `500` | Maximum queued items. |
| `ASR_QUEUE_MAX_BYTES` | 16 GiB | Maximum total staged upload size. |

### Server & shell

| Variable | Default | Description |
|----------|---------|-------------|
| `HOST` | `127.0.0.1` | Bind address. Set to `0.0.0.0` to share on a trusted LAN (no auth — only expose on trusted networks). |
| `ASR_ALLOWED_HOSTS` | _(empty)_ | Comma-separated Host names to allow when sharing on a LAN. Empty = allow any Host on a non-loopback bind; drive-by browsers are still blocked by Origin/Sec-Fetch-Site checks. |
| `SKIP_LLAMA` | `0` | Set to `1` to run the API without spawning llama-server (UI dev / unit tests). |
| `ASR_NO_BROWSER` | `0` | Set to `1` to suppress the automatic browser open on launch. |
| `ALIGNER_BACKEND` | `gpu` | `gpu` (qwen-asr, needs torch + `models/aligner/official`) or `cpu` (CrispASR DLL). |
| `SHELL_MODE` / `ASR_SHELL_TOKEN` | — | Set internally by the packaged desktop shell to enable its HttpOnly-cookie session handshake. Do not set manually. |
| `PORT` | picked by shell | Loopback port for the desktop shell (dev). |
| `ASR_SHELL_SMOKE` / `ASR_SHELL_DEBUG` | — | Desktop shell dev knobs: auto-close after N seconds / verbose logging. |

All responses are sent with CSP (`script-src 'self'`, `media-src blob:`), `nosniff` and `no-referrer` headers; non-loopback binds additionally run origin/host checks.

## Forced Alignment

Word-level timestamps align the ASR transcript back to the audio. Alignment is
**optional** — if no backend is available, the system falls back to ASR
segment timestamps automatically.

Two backends are available (`ALIGNER_BACKEND`):

1. **GPU (default)** — runs Qwen3-ForcedAligner through the `qwen-asr`
   transformers stack. Requires `torch`, `transformers` and `qwen-asr` (not
   installed by default; install into the env or the embedded runtime with
   `pip install qwen-asr torch transformers`) and the model files under:
   ```
   models/aligner/official/          # Qwen3-ForcedAligner-0.6B (safetensors)
   ```

2. **CPU** — the [CrispASR](https://github.com/CrispStrobe/CrispASR) C++
   runtime via `bin/crispasr/crispasr.dll` with the GGUF
   `models/aligner/qwen3-forced-aligner-0.6b-q8_0.gguf` (~940 MB, from
   [cstr/qwen3-forced-aligner-0.6b-GGUF](https://huggingface.co/cstr/qwen3-forced-aligner-0.6b-GGUF)).
   No VRAM needed, no impact on the ASR model. The GGUF architecture
   `qwen3asr` is not supported by standard llama-server, hence the custom DLL.

The backend probes the selected backend at startup and degrades gracefully
(GPU → CPU → ASR segments) when dependencies or files are missing.

## Batch Transcription Queue

Multiple files can be enqueued and transcribed one at a time by a serial
worker, so the GPU processes a single job while the rest wait.

- **Enqueue** — the upload modal's *Batch* tab (folder picker / multi-select)
  or a window-wide drag-and-drop of several files while busy; each file is
  staged under `data/queue/uploads/` with a random ID.
- **Manage** — reorder by drag or keyboard, sort by name/size/added time,
  pause/resume the worker, retry failed items, clear finished items. Deleting
  the running item cooperatively cancels it at the next chunk boundary.
- **Persist** — state lives in `data/queue/queue.json` (atomic writes, version
  counter for efficient polling); an interrupted run resumes as *queued* on
  the next start, and completed jobs land in the regular history.

## Project Structure

```
Qwen3-ASR17B-TsGUI/
├── bin/                                  # llama-server binary + CUDA DLLs + CrispASR (gitignored)
│   └── crispasr/crispasr.dll             # CPU aligner runtime
├── models/                               # GGUF + aligner model files (gitignored)
│   ├── asr/                              # Qwen3-ASR GGUFs (hot-swappable via /api/models)
│   └── aligner/                          # Aligner GGUF (CPU) + official/ (GPU)
├── backend/
│   ├── main.py                           # FastAPI app (transcribe + align + models + history + cache)
│   ├── llamarunner.py                    # llama-server subprocess manager (API key, health)
│   ├── queue_api.py                      # Batch queue router + serial worker
│   ├── queue_store.py                    # Persisted queue state (atomic JSON, crash recovery)
│   ├── forced_aligner.py                 # CPU aligner via CrispASR DLL
│   ├── aligner_gpu.py                    # GPU aligner via qwen-asr
│   ├── audio_chunk.py                    # ffmpeg chunking (fixed + VAD planning helpers)
│   ├── resegment.py                      # Word-level → subtitle segment scoring/merging
│   ├── text_clean.py                     # ASR artifact stripping + chunk-seam de-duplication
│   ├── history.py                        # Per-job JSON history (data/history/)
│   ├── test_main.py                      # Core API tests (CI: mocked, SKIP_LLAMA=1)
│   ├── test_queue.py / test_vad.py       # Queue, chunking tests
│   ├── test_subtitles.py / test_shell.py # Segmentation, shell/security tests
│   └── requirements.txt
├── frontend/
│   └── src/
│       ├── App.tsx                       # Layout, single/batch state machine, drag-drop
│       ├── api.ts                        # Fetch wrappers (transcribe, queue, models, history)
│       ├── types.ts                      # API response interfaces
│       ├── hooks/                        # useReadiness / useJobStatus / useQueue / useElapsed
│       ├── utils/                        # subtitle.ts, format.ts, fileTypes.ts, audioClock.ts
│       └── components/                   # UploadZone, BatchUploadZone, QueuePanel, BootOverlay,
│                                         # TranscriptPanel, AudioPlayer, ProgressBar, StatsPanel,
│                                         # ExportBar, HistoryPanel, ModelInfo, icons
├── shell/
│   ├── shell_main.py                     # pywebview/WebView2 desktop shell (in-process backend)
│   ├── asr-shell.spec                    # PyInstaller spec (windowed asr-shell.exe)
│   └── start-shell.bat                   # Dev launcher for the shell
├── tools/
│   ├── build_embed.py                    # Embeddable-Python distribution builder
│   └── subcompare.py                     # Subtitle WER/CER comparison (SRT/VTT)
├── .github/workflows/ci.yml              # CI: frontend typecheck+build, backend mocked tests
├── environment.yml                       # Conda environment definition
├── requirements-runtime.txt              # Deps for the embedded runtime (no dev/test extras)
├── run.py                                # Entrypoint: uvicorn + readiness-polled browser open
├── asr-app.spec                          # PyInstaller spec (browser-mode asr-app.exe)
├── setup.bat                             # One-click environment setup
├── start.bat                             # One-click launch (dev, browser mode)
├── build.bat                             # build.bat [app|shell|both]
├── build-embed.bat                       # One-click embeddable build
└── README.md
```

## Setup

### One-click setup

```powershell
setup.bat
```

This creates the conda environment, installs all dependencies, and checks for required files.

### Manual setup

#### 1. Conda environment

```powershell
conda env create -f environment.yml
conda activate qwen3asr
```

#### 2. Frontend dependencies

```powershell
cd frontend
npm install
```

#### 3. llama-server binary

Download `llama-b9637-bin-win-cuda-12.4-x64.zip` from the [llama.cpp releases](https://github.com/ggml-org/llama.cpp/releases/tag/b9637) and extract to `bin/`.

## Run

### Browser mode (dev)

```powershell
start.bat
```

This activates the conda environment, builds the frontend, and starts the server. Open **http://localhost:8000** in your browser.

### Desktop shell (dev)

```powershell
shell\start-shell.bat
```

Runs the backend in-process inside a 1280×820 WebView2 window (pywebview): random loopback port, one-time token handshake exchanged for an HttpOnly cookie, navigation guard that forwards external URLs to the system browser, single-instance mutex, logs in `data/logs/asr.log`. Falls back to the system browser if WebView2 is unavailable.

### Manual start

```powershell
conda activate qwen3asr
cd frontend && npm run build && cd ..

python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

### Development mode

```powershell
# Terminal 1: Backend (skip llama-server for UI dev)
conda activate qwen3asr
$env:SKIP_LLAMA = "1"
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000

# Terminal 2: Frontend dev server (with hot reload)
cd frontend
npm run dev
```

The Vite dev server proxies `/api` requests to the backend at port 8000.

### Packaged builds

```powershell
build.bat app      # PyInstaller → Qwen3-ASR\        (asr-app.exe, system browser UI)
build.bat shell    # PyInstaller → Qwen3-ASR-Shell\  (asr-shell.exe, WebView2 window)
build.bat both     # both of the above
build-embed.bat    # tools/build_embed.py → Qwen3-ASR-Embed\
                   # embeddable Python 3.12 runtime, no PyInstaller; source is editable
                   # and pip works, so GPU alignment can be restored in place via
                   # runtime\python.exe -m pip install qwen-asr torch transformers
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/health` | `{ backend, llama_server, aligner_available, aligner_running, aligner_backend }` |
| `GET` | `/api/readiness` | Boot sequence phases (model load / warmup / aligner check) |
| `POST` | `/api/readiness/retry` | Re-run the boot sequence after a terminal boot error |
| `GET` | `/api/status` | Current job status `{ status, progress, message }` (`cancelled` included) |
| `POST` | `/api/transcribe` | Upload audio/video, returns `{ text, segments[], stats, history_id }` |
| `POST` | `/api/abort` | Cancel the in-flight transcription at the next chunk boundary |
| `POST` | `/api/align` | Standalone forced alignment (audio + text → word timestamps) |
| `GET` | `/api/models` | List GGUFs in `models/asr/` + current model + tuning |
| `POST` | `/api/models/switch` | Hot-swap the loaded ASR model (refuses while busy) |
| `GET` | `/api/history` / `GET·DELETE /api/history/{hid}` / `DELETE /api/history` | History list / record / delete / clear |
| `GET` | `/api/audio-cache` / `DELETE /api/audio-cache` | Extracted-audio cache list / clear |
| `GET` | `/api/audio-cache/{name}` | Stream cached audio (drives the built-in player) |
| `POST` | `/api/queue/items` | Enqueue files (multipart, multi-file) |
| `GET` | `/api/queue` | Queue snapshot `{ items[], paused, active_id, version }` |
| `POST` | `/api/queue/reorder` / `/api/queue/sort` | Manual reorder / rule-based sort |
| `DELETE` | `/api/queue/items/{qid}` | Remove item; if running, cooperatively cancel (skip) it |
| `POST` | `/api/queue/items/{qid}/retry` | Re-queue a failed/cancelled item |
| `POST` | `/api/queue/pause` / `/api/queue/resume` | Pause / resume the worker |
| `DELETE` | `/api/queue/finished` | Clear done/cancelled items |

### Transcribe parameters

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `file` | file | required | Audio or video file |
| `align` | bool | `false` | Enable forced alignment for word-level timestamps |

### Example: transcribe via curl

```powershell
# Basic transcription
curl -X POST http://localhost:8000/api/transcribe -F "file=@audio.wav"

# With forced alignment
curl -X POST http://localhost:8000/api/transcribe -F "file=@audio.wav" -F "align=true"

# Enqueue a batch
curl -X POST http://localhost:8000/api/queue/items -F "files=@a.mp3" -F "files=@b.mp4"
```

### Response format

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

## Testing

```powershell
# Backend tests (skip llama-server spawn for unit testing)
conda activate qwen3asr
$env:SKIP_LLAMA = "1"
python -m pytest backend\test_main.py -v                              # core API (CI runs this mocked)
python -m pytest backend\test_queue.py backend\test_vad.py -v         # queue + chunking
python -m pytest backend\test_subtitles.py backend\test_shell.py -v   # segmentation + security

# Frontend type-check + production build
cd frontend
npm run typecheck
npm run build
```

CI (`.github/workflows/ci.yml`) runs the frontend typecheck/build on Node 22 and backend `compileall` + the mocked `test_main.py` on Python 3.13 for every push/PR.

## Tools

```powershell
python tools\subcompare.py ref.srt hyp.srt            # WER / CER / timestamp drift
python tools\subcompare.py ref.srt hyp.srt --html report.html
```

`subcompare.py` parses SRT/VTT (encoding auto-detected), pairs segments by
time range and reports WER, CER and start/end drift as a TUI table, HTML
report, or JSON — useful for A/B-ing ASR or segmentation changes.

## Model Files

Place model files under `models/` (gitignored — download from the links below
or from the GitHub Release). The backend auto-detects each file at startup
and gracefully disables features that are missing. Any additional `.gguf`
dropped into `models/asr/` becomes hot-swappable via `/api/models`.

| File | Location | Size | Required | Description |
|------|----------|------|----------|-------------|
| `Qwen3-ASR-1.7B-Q8_0.gguf` | `models/asr/` | ~2.2 GB | Yes | Main ASR model (Q8 quantized) |
| `mmproj-Qwen3-ASR-1.7B-Q8_0.gguf` | `models/asr/` | ~356 MB | Yes | ASR multimodal projector (Q8) |
| `qwen3-forced-aligner-0.6b-q8_0.gguf` | `models/aligner/` | ~940 MB | No | ForcedAligner model (Q8, CPU backend) |
| Qwen3-ForcedAligner-0.6B (safetensors) | `models/aligner/official/` | ~1.8 GB | No | ForcedAligner (GPU backend via qwen-asr) |

- ASR models: [ggml-org/Qwen3-ASR-1.7B-GGUF](https://huggingface.co/ggml-org/Qwen3-ASR-1.7B-GGUF)
- Aligner GGUF: [cstr/qwen3-forced-aligner-0.6b-GGUF](https://huggingface.co/cstr/qwen3-forced-aligner-0.6b-GGUF)

## License

Model files are subject to their original license from Qwen. Application code is provided as-is.
