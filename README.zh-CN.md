# Qwen3-ASR 语音转写应用

[English](README.md) | 简体中文

在本地 NVIDIA 显卡上跑的语音转文字。模型是 llama-server 加载的 Qwen3-ASR-1.7B GGUF，输入音频或视频，输出 SRT、VTT、ASS、TXT 或 JSON。词级时间戳是可选项。界面既能在浏览器里用，也能装进一个 WebView2 窗口单独运行。

## 功能说明

输入可以是一个文件，也可以是一整个文件夹。音频支持 WAV、MP3、FLAC、OGG、M4A；视频支持 MP4、MKV、MOV、AVI、WebM、FLV、M4V、WMV、MPG、MPEG、TS、3GP、VOB、OGV。视频先把音轨抽成 MP3，抽出来的文件存进 `data/audio_cache/`，同一个视频再跑第二次就不用再过 ffmpeg。

Qwen3-ASR 把音频编码成一长串多模态 token，大约每 10 秒 600 个，40 分钟的录音塞不进上下文窗口。所以长音频先切成 25 秒的块，相邻块留 1.5 秒重叠，每块单独转写，再按修正过的时间戳拼回去。某一块失败就先重试，重试仍失败就把它对半切开。拼接时接缝上重叠的那部分内容会被裁掉，不会重复出现。

按时间硬切出来的块不适合直接当字幕，所以词流会再过一遍打分段，按停顿长度、标点、语气词、行首尾禁则重新断句，中文行的宽度另有一套更紧的限制。

对齐是可选的，有自己的后端。GPU 走 `qwen-asr` 跑 Qwen3-ForcedAligner-0.6B，CPU 走 ctypes 调 CrispASR 的 DLL。两个都不是必需的，缺了就用 ASR 自带的段级时间戳，任务照样完成。

另外几件事：

- `models/asr/` 下的模型可以在界面里直接换，不用重启
- 批量队列的状态会落盘，崩溃或关窗后下次启动接着跑
- 每次任务报告 RTF、耗时、字符数、词数、分块数
- 语种默认自动检测，也可以手动指定
- 界面是玻璃质感面板，支持完整键盘导航和减弱动效

## 架构

```
前端 (React + Vite + Tailwind)
        |
        v
后端 (Python FastAPI)
        |
        +-- llama-server          (Qwen3-ASR-1.7B GGUF, GPU)
        |       ASR 推理，启动时拉起，进程级随机 API key
        |
        +-- 对齐器后端            (GPU: qwen-asr / CPU: CrispASR DLL)
        |       词级强制对齐，按需运行
        |
        +-- 队列 worker           (串行 asyncio 任务)
                消费持久化的批量队列，复用 transcribe 的
                重试 / 取消 / 对齐 / 历史链路
```

| 组件 | 技术栈 | 职责 |
|-----------|-------|------|
| llama-server | C++ 二进制（CUDA） | ASR 推理，用 `--api-key` 保护 |
| 对齐器（GPU） | Python / qwen-asr | 从 `models/aligner/official` 出词级时间戳 |
| CrispASR | C++ DLL（CPU） | 经 `align_words` ABI 的对齐回退 |
| 队列 worker | asyncio 任务 | 串行批量转写，状态在 `data/queue/` |
| 后端 | Python 3.13 / FastAPI / httpx | API 网关、分块、编排、安全响应头 |
| 前端 | React 19 / TypeScript / Vite / Tailwind | 界面、队列抽屉、统计、导出 |

## 环境要求

- Windows 10 或 11
- NVIDIA 显卡，CUDA 12.4 及以上
- [Miniconda](https://docs.conda.io/en/latest/miniconda.html) 或 Anaconda
- Node.js 22+，只有构建前端和跑 CI 时需要
- `PATH` 上要有 **ffmpeg**，分块和抽音轨都靠它（conda 环境里自带）
- `pywebview`，只有桌面壳需要，已在 `requirements.txt` 里

## 配置

### GPU 与性能

上下文大小、KV 量化、卸载层数按显存自动选。启动时用 `nvidia-smi` 探测，探不到就按 8 GB 算。下面任何一项都能在启动前用环境变量覆盖。

| 显存 | ctx_size | KV 量化 |
|------|----------|---------|
| 4.5 GB 以下 | 16384 | `q4_0` |
| 8.5 GB 以下 | 32768 | `q8_0` |
| 8.5 GB 以上 | 32768 | `f16` |

| 变量 | 默认值 | 说明 |
|----------|---------|-------------|
| `VRAM_GB` | 自动检测 | 跳过探测，直接告诉上表用这个值。 |
| `CTX_SIZE` | 见上表 | llama-server 上下文窗口。 |
| `KV_QUANT` | 见上表 | KV 缓存量化：`q8_0`、`q4_0`、`f16`。 |
| `NGL` | `99` | 卸载到 GPU 的层数。 |
| `THREADS` | 自动 | llama-server 的 CPU 线程数。 |
| `CHUNK_SECONDS` | `25` | 分块时长，秒。 |
| `CHUNK_OVERLAP` | `1.5` | 相邻块的重叠秒数，让跨界的词保住上下文。设 `0` 就是紧挨着切。 |

### 转写

| 变量 | 默认值 | 说明 |
|----------|---------|-------------|
| `ASR_TEMPERATURE` | `0` | 传给 llama-server 的采样温度。 |
| `ASR_LANGUAGE` | _(空)_ | 指定语种，不走自动检测。 |
| `ASR_MAX_RETRIES` | `2` | 每块的重试次数，用完就触发再切分。 |
| `ASR_RESPLIT` | `1` | 失败块二分到通过为止。设 `0` 则直接放弃这块。 |
| `ASR_PROMPT_MAX_CHARS` | `200` | 从上一块带到下一块的提示字符数。 |

### CPU 对齐器

CrispASR 每次调用都要加载一遍对齐模型，所以多个块用线程池并行对齐（ctypes 在 C 调用期间释放 GIL，线程是真并行）。每次并发调用约占 262 MB 模型缓冲，这就是 worker 数要设上限的原因。

| 变量 | 默认值 | 说明 |
|----------|---------|-------------|
| `ALIGN_N_THREADS` | `4` | 单次对齐调用用的线程数。 |
| `ALIGN_MAX_WORKERS` | `3` | 并发对齐数，会被夹到 1–4。 |
| `ALIGN_MAX_CHARS` | `400` | 超过这个长度的文本在对齐前截断。对齐器的注意力矩阵随序列长度平方增长，模型一旦幻觉，吐出的文本足够把内存吃光。 |

### 限额与缓存

| 变量 | 默认值 | 说明 |
|----------|---------|-------------|
| `ASR_MAX_UPLOAD_BYTES` | 2 GiB | 单文件上传上限，前端同步校验。 |
| `ASR_MAX_AUDIO_DURATION` | 12 小时 | 接受的最长媒体。 |
| `ASR_MAX_ALIGN_TEXT_CHARS` | `50000` | `/api/align` 单请求的文本上限。 |
| `ASR_AUDIO_CACHE_MAX_BYTES` | 4 GiB | `data/audio_cache/` 的 LRU 容量上限。 |
| `ASR_AUDIO_CACHE_MAX_ENTRIES` | `200` | 同一缓存的条目数上限。 |

### 批量队列

| 变量 | 默认值 | 说明 |
|----------|---------|-------------|
| `ASR_QUEUE_MAX_ENTRIES` | `500` | 队列条目上限。 |
| `ASR_QUEUE_MAX_BYTES` | 16 GiB | 暂存上传的总量上限。 |

### 服务器与桌面壳

| 变量 | 默认值 | 说明 |
|----------|---------|-------------|
| `HOST` | `127.0.0.1` | 绑定地址。设成 `0.0.0.0` 就把应用开放到局域网，且没有任何鉴权，只在可信网络上这么干。 |
| `PORT` | `8000` | 监听端口。桌面壳会自己挑一个空闲的回环端口。 |
| `ASR_ALLOWED_HOSTS` | _(空)_ | 绑到非回环地址时允许的 `Host`，逗号分隔。留空表示任意 Host 都收，路人浏览器仍然会被 Origin 和 `Sec-Fetch-Site` 检查拦下。想把这个局域网共享钉在一台机器名上就填它。 |
| `SKIP_LLAMA` | `0` | 设 `1` 则不拉起 llama-server，做界面开发和跑测试时用。 |
| `ASR_NO_BROWSER` | `0` | 设 `1` 则启动后不自动开浏览器。 |
| `ALIGNER_BACKEND` | `gpu` | `gpu` 需要 torch 和 `models/aligner/official`；`cpu` 用 CrispASR DLL。填了不认识的值会退回 `cpu`。 |
| `SHELL_MODE`、`ASR_SHELL_TOKEN` | 未设置 | 由打包后的桌面壳内部设置，启用它的 HttpOnly Cookie 握手。不要手动填。 |
| `ASR_SHELL_SMOKE`、`ASR_SHELL_DEBUG` | 未设置 | 桌面壳的开发开关：N 秒后自动关窗、详细日志。 |

所有响应都带 CSP，`script-src 'self'` 加 `media-src blob:`，另有 `nosniff` 和 `no-referrer`。Origin 与 Host 检查只在非回环绑定上生效，因为同源跑的浏览器应用本来也不会有值得据此判断的 `Sec-Fetch-Site`。

## 强制对齐

词级时间戳是把转写文本对齐回音频得到的。用 `ALIGNER_BACKEND` 选后端。

**GPU**（默认）经 `qwen-asr` transformers 栈跑 Qwen3-ForcedAligner。需要 `torch`、`transformers`、`qwen-asr`，这三个默认都不装：

```
pip install qwen-asr torch transformers
```

模型文件放在 `models/aligner/official/`，safetensors 格式，约 1.8 GB。GPU 后端在加载对齐器前会把 ASR 模型从显存里卸掉，所以 4 GB 的卡能先后跑两者，不能同时跑。

**CPU** 经 `bin/crispasr/crispasr.dll` 调 [CrispASR](https://github.com/CrispStrobe/CrispASR) 的 C++ 运行时，配 `models/aligner/qwen3-forced-aligner-0.6b-q8_0.gguf`（约 940 MB，来自 [cstr/qwen3-forced-aligner-0.6b-GGUF](https://huggingface.co/cstr/qwen3-forced-aligner-0.6b-GGUF)）。不占显存，也不影响 ASR 模型。之所以需要这个定制 DLL，是因为标准 llama-server 不支持 `qwen3asr` 这个 GGUF 架构。

不管选哪个，启动时都会探测一次；依赖或文件缺失时按 GPU、CPU、ASR 段级时间戳的顺序往下退。

## 批量转写队列

文件由一个串行 worker 逐个转写，因为一块显卡同时只能装一个模型。

入队从上传弹窗的 *Batch* 标签页操作（选文件夹或多选），或者在任务跑着的时候把多个文件拖到窗口任意位置。每个文件以一个随机 id 暂存到 `data/queue/uploads/`。

队列抽屉支持拖拽和键盘重排、按名称/大小/入队时间排序、暂停恢复 worker、重试失败项、清理已完成项。删掉正在跑的那一项，它会在下一个分块边界被协作式取消。

状态存在 `data/queue/queue.json`，原子写，带一个自增的版本号好让轮询便宜一些。崩溃或关窗打断的运行，下次启动时回到 *queued*。完成的进常规历史。

## 项目结构

```
Qwen3-ASR1.7B-TsGUI/
├── bin/                                  # llama-server 二进制 + CUDA DLL + CrispASR（gitignore）
│   └── crispasr/crispasr.dll             # CPU 对齐器运行时
├── models/                               # GGUF 与对齐器模型（gitignore）
│   ├── asr/                              # Qwen3-ASR GGUF，可经 /api/models 热切换
│   └── aligner/                          # 对齐器 GGUF（CPU）+ official/（GPU）
├── backend/
│   ├── main.py                           # FastAPI 应用：转写、对齐、模型、历史、缓存
│   ├── llamarunner.py                    # llama-server 子进程管理（API key、健康检查）
│   ├── queue_api.py                      # 队列路由 + 串行 worker
│   ├── queue_store.py                    # 队列状态落盘，原子 JSON，崩溃恢复
│   ├── forced_aligner.py                 # 经 CrispASR DLL 的 CPU 对齐器
│   ├── aligner_gpu.py                    # 经 qwen-asr 的 GPU 对齐器
│   ├── audio_chunk.py                    # ffmpeg 分块（其中的 VAD 规划器尚未接入流水线）
│   ├── resegment.py                      # 词流转字幕段的打分与合并
│   ├── text_clean.py                     # ASR 伪影清理、分块接缝去重
│   ├── history.py                        # 逐任务 JSON 历史，data/history/
│   ├── requirements.txt
│   └── test_*.py                         # main、queue、vad、subtitles、shell
├── frontend/
│   ├── index.html
│   ├── vite.config.ts
│   └── src/
│       ├── App.tsx                       # 布局、单文件/批量状态机、拖放
│       ├── api.ts                        # transcribe、queue、models、history 的 fetch 封装
│       ├── types.ts                      # API 响应接口
│       ├── index.css                     # Tailwind 入口、设计令牌
│       ├── hooks/                        # useReadiness、useJobStatus、useQueue、useElapsed
│       ├── utils/                        # subtitle、format、fileTypes、audioClock
│       └── components/                   # UploadZone、BatchUploadZone、QueuePanel、BootOverlay、
│                                         # TranscriptPanel、AudioPlayer、ProgressBar、StatsPanel、
│                                         # ExportBar、HistoryPanel、ModelInfo、icons
├── shell/
│   ├── shell_main.py                     # pywebview/WebView2 桌面壳，后端跑在进程内
│   ├── asr-shell.spec                    # PyInstaller 配置（窗口模式 asr-shell.exe）
│   └── start-shell.bat                   # 开发启动器
├── tools/
│   └── subcompare.py                     # 字幕 WER/CER 对照
├── data/                                 # 运行期状态（gitignore）：history、queue、
│                                         # audio_cache、chunk_cache、jobs、logs、webview_profile
├── .github/workflows/ci.yml              # 前端 typecheck+build，后端 mock 测试
├── environment.yml                       # conda 环境定义
├── run.py                                # 入口：uvicorn，就绪后再开浏览器
├── asr-app.spec                          # PyInstaller 配置（浏览器模式 asr-app.exe）
├── setup.bat                             # 建 conda 环境并装依赖
├── start.bat                             # 开发启动，浏览器模式
├── build.bat                             # build.bat [app|shell|both]
├── README.md
└── README.zh-CN.md
```

## 安装

```powershell
setup.bat
```

建好 conda 环境、装完依赖，并检查应用需要的文件是否就位。想手动装：

```powershell
conda env create -f environment.yml
conda activate qwen3asr
cd frontend
npm install
```

然后从 [llama.cpp releases](https://github.com/ggml-org/llama.cpp/releases/tag/b9637) 下载 `llama-b9637-bin-win-cuda-12.4-x64.zip`，解压到 `bin/`。

## 运行

浏览器模式，开发时一般用这个：

```powershell
start.bat
```

它会激活 conda 环境、构建前端，然后在 http://localhost:8000 提供服务。

桌面壳把后端跑在一个 1280×820 的 WebView2 窗口进程里：

```powershell
shell\start-shell.bat
```

它挑一个随机回环端口，给页面传一次性令牌，页面拿令牌换 HttpOnly Cookie；窗口里的外部链接交给系统浏览器打开，不做站内跳转；并且持有一个命名互斥量，第二次启动不会跟端口、队列和浏览器配置打架。日志在 `data/logs/asr.log`。WebView2 不可用时退回系统浏览器。

不走批处理的话：

```powershell
conda activate qwen3asr
cd frontend && npm run build && cd ..
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

改前端要热更新，就一个终端 `SKIP_LLAMA=1` 起 API，另一个终端 `npm run dev`。Vite 会把 `/api` 代理到 8000。

```powershell
# 终端 1
conda activate qwen3asr
$env:SKIP_LLAMA = "1"
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000

# 终端 2
cd frontend
npm run dev
```

### 打包

```powershell
build.bat app      # PyInstaller，Qwen3-ASR\asr-app.exe（系统浏览器）
build.bat shell    # PyInstaller，Qwen3-ASR-Shell\asr-shell.exe（WebView2 窗口）
build.bat both
```

## API

| 方法 | 路径 | 说明 |
|--------|------|-------------|
| `GET` | `/api/health` | `backend`、`llama_server`、`aligner_available`、`aligner_running`、`aligner_backend` |
| `GET` | `/api/readiness` | 启动序列各阶段：模型加载、预热、对齐器检查 |
| `POST` | `/api/readiness/retry` | 启动终态失败后重跑一遍启动序列 |
| `GET` | `/api/status` | 当前任务的 `status`、`progress`、`message` |
| `POST` | `/api/transcribe` | 上传媒体，返回 `text`、`segments[]`、`stats`、`history_id` |
| `POST` | `/api/abort` | 在下一个分块边界取消当前转写 |
| `POST` | `/api/align` | 独立对齐，音频加文本进，词级时间戳出 |
| `GET` | `/api/models` | `models/asr/` 下的 GGUF、当前模型、调参 |
| `POST` | `/api/models/switch` | 热切换 ASR 模型，忙的时候拒绝 |
| `GET` | `/api/history` | 历史列表 |
| `GET`、`DELETE` | `/api/history/{hid}` | 单条记录 |
| `DELETE` | `/api/history` | 清空历史 |
| `GET`、`DELETE` | `/api/audio-cache` | 音轨缓存的列表 / 清空 |
| `GET` | `/api/audio-cache/{name}` | 流式返回某个缓存文件，内置播放器用 |
| `POST` | `/api/queue/items` | 入队（multipart，一次可带多个文件） |
| `GET` | `/api/queue` | 快照：`items[]`、`paused`、`active_id`、`version` |
| `POST` | `/api/queue/reorder`、`/api/queue/sort` | 手动重排 / 规则排序 |
| `DELETE` | `/api/queue/items/{qid}` | 移除；该项在跑就顺带取消 |
| `POST` | `/api/queue/items/{qid}/retry` | 把失败或已取消的项重新入队 |
| `POST` | `/api/queue/pause`、`/api/queue/resume` | worker 开关 |
| `DELETE` | `/api/queue/finished` | 清掉已完成和已取消的项 |

`/api/transcribe` 收一个必填的 `file`，加一个可选的布尔 `align`，默认 `false`。

```powershell
curl -X POST http://localhost:8000/api/transcribe -F "file=@audio.wav"
curl -X POST http://localhost:8000/api/transcribe -F "file=@audio.wav" -F "align=true"
curl -X POST http://localhost:8000/api/queue/items -F "files=@a.mp3" -F "files=@b.mp4"
```

响应形如：

```json
{
  "text": "完整转写文本",
  "segments": [
    {"start": 0.0, "end": 3.5, "text": "分段文本"}
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

## 测试

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

`test_main.py` 把 llama-server、ffmpeg 和对齐器都 mock 掉了，没有显卡也能跑。CI（`.github/workflows/ci.yml`）在每次 push 和 PR 时用 Node 22 跑前端 typecheck 与构建，用 Python 3.13 跑 `compileall` 加那份 mock 测试。

## 字幕对照

```powershell
python tools\subcompare.py ref.srt hyp.srt
python tools\subcompare.py ref.srt hyp.vtt --html report.html
python tools\subcompare.py ref.srt hyp.srt --min-iou 0.3
```

`subcompare.py` 解析 SRT、VTT、ASS，编码自己猜，按时间段配对片段，在终端输出 WER、CER 与起止时间漂移；`--html` 把同一份结果写成 HTML 报告，`--min-iou` 把时间重叠太小的配对当作不同行丢掉。改了分块或断句之后，用它判断到底是变好了还是变差了。

## 模型文件

模型不在仓库里。放到 `models/` 下，后端启动时自己找，缺什么就停掉对应功能，不会拒绝启动。往 `models/asr/` 里丢任何额外的 `.gguf`，都会出现在 `/api/models` 里可以热切换。

| 文件 | 位置 | 大小 | 必需 | 用途 |
|------|----------|------|----------|------|
| `Qwen3-ASR-1.7B-Q8_0.gguf` | `models/asr/` | ~2.2 GB | 是 | ASR 模型本体，Q8 |
| `mmproj-Qwen3-ASR-1.7B-Q8_0.gguf` | `models/asr/` | ~356 MB | 是 | 它的多模态投影器 |
| `qwen3-forced-aligner-0.6b-q8_0.gguf` | `models/aligner/` | ~940 MB | 否 | CPU 后端的对齐器 |
| Qwen3-ForcedAligner-0.6B（safetensors） | `models/aligner/official/` | ~1.8 GB | 否 | GPU 后端的对齐器 |

ASR 模型：[ggml-org/Qwen3-ASR-1.7B-GGUF](https://huggingface.co/ggml-org/Qwen3-ASR-1.7B-GGUF)
对齐器 GGUF：[cstr/qwen3-forced-aligner-0.6b-GGUF](https://huggingface.co/cstr/qwen3-forced-aligner-0.6b-GGUF)

## 已知的限制

- 同一时刻只转一个文件。队列是让你不用盯着显卡，不是用来并行的。
- `HOST=0.0.0.0` 是无鉴权的共享。Origin 和 Host 检查能挡住路人浏览器的请求，挡不住同一网段里想看你的历史、想往队列里塞文件的人。
- 只在 Windows 上可用。启动脚本是批处理，桌面壳依赖 WebView2，互斥量和对齐器 DLL 走的都是 Windows 专有接口。
- 1.5 秒的重叠能减少跨界词的错字，但不能消掉。
- CPU 后端下，超出 `ALIGN_MAX_CHARS` 的文本在对齐前就被截断了，那些词只保留段级时间戳，没有词级的。
- 上传按设计卡在 2 GiB 和 12 小时。

## 许可证

模型文件仍受 Qwen 原始许可证约束。应用代码按原样提供。
