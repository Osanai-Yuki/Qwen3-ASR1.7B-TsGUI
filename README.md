# Qwen3-ASR Transcription App

CUDA-accelerated automatic speech recognition using the Qwen3-ASR-1.7B GGUF model, with optional word-level forced alignment via Qwen3-ForcedAligner-0.6B, multi-format subtitle export, real-time performance statistics, and a minimalist black-and-white UI.

## Features

- **Audio file transcription** — upload WAV, MP3, FLAC, OGG, or M4A files
- **Audio chunking** — long audio is split into 25 s chunks to fit 32K context (4 GB VRAM friendly)
- **Forced alignment (optional)** — word-level timestamps via Qwen3-ForcedAligner-0.6B (GGUF, CPU)
- **Multi-format export** — SRT, VTT, ASS, TXT, JSON
- **Performance statistics** — RTF, processing time, character/word counts, chunk count
- **GPU acceleration** — full CUDA offload via llama-server
- **Multi-language** — automatic language detection
- **Minimalist UI** — pure black, white text, bold typography, sharp corners

## Architecture

```
Frontend (React + Vite + Tailwind)
        |
        v
Backend (Python FastAPI)
        |
        +── llama-server :8080  (Qwen3-ASR-1.7B, GPU)
        |       ASR inference, always running
        |
        +── CrispASR DLL        (Qwen3-ForcedAligner-0.6B, CPU)
                Forced alignment, on-demand via ctypes
```

| Component | Stack | Role |
|-----------|-------|------|
| **llama-server** | C++ binary (CUDA) | ASR inference, port 8080 |
| **CrispASR** | C++ DLL (CPU) | Forced alignment via `align_words` ABI |
| **Backend** | Python 3.13 / FastAPI / httpx | API gateway, chunking, orchestration |
| **Frontend** | React 19 / TypeScript / Vite / Tailwind | UI, stats, multi-format export |

## Prerequisites

- Windows 10/11
- NVIDIA GPU with CUDA 12.4+
- [Miniconda](https://docs.conda.io/en/latest/miniconda.html) or Anaconda
- Node.js 24+
- **ffmpeg** on `PATH` (used to chunk audio before inference)

## Low-VRAM tuning

Qwen3-ASR encodes audio as a long sequence of multimodal tokens (~600 tokens per
10 s of audio). On a 4 GB GPU, a single several-minute clip will overflow the
context window or the KV cache. The backend therefore **splits incoming audio
into short chunks**, transcribes each chunk independently, and stitches the
segments back together with corrected timestamps.

Tunable environment variables (set before launching `start.bat` or uvicorn):

| Variable | Default | Description |
|----------|---------|-------------|
| `CTX_SIZE` | `32768` | llama-server context window. Keep at 32 K on 4 GB GPUs. |
| `CHUNK_SECONDS` | `25` | Audio chunk length in seconds. Lower this if you still hit context overflow. |
| `KV_QUANT` | `q8_0` | KV-cache quantization (`q8_0`, `q4_0`, `f16`). Quantized KV roughly halves VRAM. |
| `SKIP_LLAMA` | `0` | Set to `1` to run the API without spawning llama-server (UI dev mode). |

## Forced Alignment (optional)

Qwen3-ForcedAligner-0.6B produces word-level timestamps by aligning the ASR
transcript back to the audio. It is **optional** — if the model file or DLL is
absent, the system falls back to ASR segment timestamps automatically.

### How it works

The ForcedAligner GGUF uses architecture `qwen3asr`, which is **not supported
by standard llama-server**. Instead, alignment runs through the
[CrispASR](https://github.com/CrispStrobe/CrispASR) C++ runtime via
`crispasr.dll`. This runs entirely on **CPU** — no GPU VRAM needed, no model
swapping, no impact on the ASR model.

### Setup

1. **Download the GGUF** from
   [cstr/qwen3-forced-aligner-0.6b-GGUF](https://huggingface.co/cstr/qwen3-forced-aligner-0.6b-GGUF)
   and place it in the project root:
   ```
   qwen3-forced-aligner-0.6b-q8_0.gguf   # ~940 MB
   ```

2. **Build CrispASR** (one-time):
   ```powershell
   git clone https://github.com/CrispStrobe/CrispASR D:\Temp\CrispASR
   cd D:\Temp\CrispASR
   cmake -B build -DCMAKE_BUILD_TYPE=Release -G "Visual Studio 18 2026" -A x64
   cmake --build build --config Release --target crispasr-lib
   copy build\bin\Release\crispasr.dll <project>\bin\
   ```

3. **Install numpy** (for PCM conversion):
   ```powershell
   conda activate qwen3-asr
   pip install numpy
   ```

The backend auto-detects both the model file and `crispasr.dll` at startup.
If either is missing, alignment is gracefully skipped.

## Project Structure

```
Qwen3-ASR17-Ts/
├── bin/                                  # llama-server binary + CUDA DLLs + CrispASR
│   ├── llama-server.exe
│   ├── ggml-cuda.dll
│   └── crispasr.dll                     # CrispASR runtime (for ForcedAligner)
├── backend/
│   ├── main.py                           # FastAPI app (transcribe + align + stats)
│   ├── llamarunner.py                    # llama-server subprocess manager
│   ├── forced_aligner.py                 # ForcedAligner via CrispASR C++ (CPU)
│   ├── audio_chunk.py                    # ffmpeg-based audio chunking
│   ├── test_main.py                      # Backend tests
│   └── requirements.txt
├── frontend/
│   ├── src/
│   │   ├── App.tsx                       # Main layout + transcription state machine
│   │   ├── main.tsx                      # React entrypoint
│   │   ├── index.css                     # Tailwind v4 theme + minimalist black/white base
│   │   ├── types.ts                      # API response interfaces
│   │   ├── api.ts                        # fetch wrappers for all endpoints
│   │   ├── hooks/
│   │   │   ├── useReadiness.ts           # Poll /api/readiness until boot complete
│   │   │   └── useJobStatus.ts           # Poll /api/status during transcription
│   │   ├── components/
│   │   │   ├── BootOverlay.tsx           # Full-screen mask while models load
│   │   │   ├── UploadZone.tsx            # Drag-and-drop upload + align toggle
│   │   │   ├── ProgressBar.tsx           # Live job status / progress
│   │   │   ├── TranscriptPanel.tsx       # Timestamped transcript display
│   │   │   ├── ExportBar.tsx             # Multi-format export (SRT/VTT/ASS/TXT/JSON)
│   │   │   ├── StatsPanel.tsx            # Performance statistics display
│   │   │   └── HistoryPanel.tsx          # History list: restore / delete / clear
│   │   └── utils/
│   │       ├── subtitle.ts               # Subtitle generation (5 formats)
│   │       └── format.ts                 # Time/size/duration formatting
│   ├── public/
│   │   └── favicon.svg
│   ├── vite.config.ts
│   ├── tsconfig.json
│   └── package.json
├── environment.yml                       # Conda environment definition
├── Qwen3-ASR-1.7B-Q8_0.gguf             # Main ASR model (Q8, ~2.2 GB)
├── mmproj-Qwen3-ASR-1.7B-Q8_0.gguf      # ASR multimodal projector (Q8, ~356 MB)
├── Qwen3-ForcedAligner-0.6B-Q8_0.gguf   # ForcedAligner model (Q8, ~600 MB, optional)
├── mmproj-Qwen3-ForcedAligner-0.6B-Q8_0.gguf  # Aligner projector (optional)
├── setup.bat                             # One-click environment setup
├── start.bat                             # One-click launch script
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
conda activate qwen3-asr
```

#### 2. Frontend dependencies

```powershell
cd frontend
npm install
```

#### 3. llama-server binary

Download `llama-b9637-bin-win-cuda-12.4-x64.zip` from the [llama.cpp releases](https://github.com/ggml-org/llama.cpp/releases/tag/b9637) and extract to `bin/`.

## Run

### Quick start

```powershell
start.bat
```

This activates the conda environment, builds the frontend, and starts the server. Open **http://localhost:8000** in your browser.

### Manual start

```powershell
# Activate conda environment
conda activate qwen3-asr

# Build frontend
cd frontend
npm run build
cd ..

# Start backend (spawns llama-server automatically)
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

### Development mode

```powershell
# Terminal 1: Backend (skip llama-server for UI dev)
conda activate qwen3-asr
$env:SKIP_LLAMA = "1"
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000

# Terminal 2: Frontend dev server (with hot reload)
cd frontend
npm run dev
```

The Vite dev server proxies `/api` requests to the backend at port 8000.

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/health` | `{ backend, llama_server, aligner_available, aligner_running }` |
| `GET` | `/api/status` | Current job status `{ status, progress, message }` |
| `POST` | `/api/transcribe` | Upload audio, returns `{ text, segments[], stats }` |
| `POST` | `/api/align` | Standalone forced alignment (audio + text → word timestamps) |

### Transcribe parameters

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `file` | file | required | Audio file (WAV/MP3/FLAC/OGG/M4A) |
| `align` | bool | `false` | Enable forced alignment for word-level timestamps |

### Example: transcribe via curl

```powershell
# Basic transcription
curl -X POST http://localhost:8000/api/transcribe -F "file=@audio.wav"

# With forced alignment
curl -X POST http://localhost:8000/api/transcribe -F "file=@audio.wav" -F "align=true"
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
    "aligner_model": "Qwen3-ForcedAligner-0.6B-Q8_0"
  }
}
```

## Testing

```powershell
# Backend tests
conda activate qwen3-asr
$env:SKIP_LLAMA = "1"
python -m pytest backend\test_main.py -v

# Frontend tests
cd frontend
npx vitest run
```

## Model Files

| File | Size | Required | Description |
|------|------|----------|-------------|
| `Qwen3-ASR-1.7B-Q8_0.gguf` | ~2.2 GB | Yes | Main ASR model (Q8 quantized) |
| `mmproj-Qwen3-ASR-1.7B-Q8_0.gguf` | ~356 MB | Yes | ASR multimodal projector (Q8) |
| `mmproj-Qwen3-ASR-1.7B-bf16.gguf` | ~640 MB | No | ASR projector (bf16, unused) |
| `Qwen3-ForcedAligner-0.6B-Q8_0.gguf` | ~600 MB | No | ForcedAligner model (Q8) |
| `mmproj-Qwen3-ForcedAligner-0.6B-Q8_0.gguf` | — | No | ForcedAligner projector (if available) |

- ASR models: [ggml-org/Qwen3-ASR-1.7B-GGUF](https://huggingface.co/ggml-org/Qwen3-ASR-1.7B-GGUF)
- Aligner GGUF: [cstr/qwen3-forced-aligner-0.6b-GGUF](https://huggingface.co/cstr/qwen3-forced-aligner-0.6b-GGUF)

## License

Model files are subject to their original license from Qwen. Application code is provided as-is.
