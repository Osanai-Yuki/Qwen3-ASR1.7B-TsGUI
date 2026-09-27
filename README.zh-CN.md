# Qwen3-ASR 语音转写应用

[English](README.md) | 简体中文

基于 CUDA 加速的自动语音识别应用，使用 Qwen3-ASR-1.7B GGUF 模型，支持可选的词级强制对齐、批量转写队列、多格式字幕导出、实时性能统计，以及极简玻璃质感界面。既可以作为浏览器应用运行，也可以作为 WebView2 桌面壳运行。

## 功能特性

- **音频与视频转写** — 支持音频（WAV、MP3、FLAC、OGG、M4A 等）与视频（MP4、MKV、MOV、AVI、WebM 等）上传；视频音轨会提取为 MP3 并缓存以便回放
- **单文件与批量模式** — 既可逐个转写，也可将整个文件夹拖入持久化转写队列（支持暂停 / 恢复 / 重排 / 重试 / 跳过）
- **音频分块** — 长音频按 25 秒分块并保留 1.5 秒重叠以适配上下文窗口；失败块自动重试并再切分，合并文本时对分块接缝处的重叠内容去重
- **字幕级断句** — 分块产出的词通过打分模型（停顿、标点、语气词、行首尾规则）合并为字幕行，中文行宽单独收紧
- **强制对齐（可选）** — 通过 GPU 后端（qwen-asr / Qwen3-ForcedAligner-0.6B）或 CPU 端 CrispASR DLL 获得词级时间戳，缺失时自动回退到 ASR 段级时间戳
- **模型热切换** — 在界面中直接切换 `models/asr/` 下的 GGUF 模型，无需重启
- **多格式导出** — SRT、VTT、ASS、TXT、JSON
- **性能统计** — RTF、处理耗时、字符/词数、分块数
- **GPU 加速** — 经 llama-server 全量 CUDA 卸载；按显存自适应的上下文/KV 预设，进程级 API key 保护
- **多语言** — 自动语种检测
- **极简界面** — 玻璃质感面板、语义化设计令牌、完整键盘导航与减弱动效支持

## 架构

```
前端 (React + Vite + Tailwind)
        |
        v
后端 (Python FastAPI)
        |
        +── llama-server          (Qwen3-ASR-1.7B GGUF, GPU)
        |       ASR 推理，启动时拉起，进程级随机 API key
        |
        +── 对齐器后端            (GPU: qwen-asr / CPU: CrispASR DLL)
        |       词级强制对齐，按需运行
        |
        +── 队列 worker           (串行 asyncio 任务)
                持续消费持久化批量队列，复用 transcribe
                流水线（重试 / 取消 / 对齐 / 历史）
```

| 组件 | 技术栈 | 职责 |
|-----------|-------|------|
| **llama-server** | C++ 二进制 (CUDA) | ASR 推理，`--api-key` 保护 |
| **对齐器 (GPU)** | Python / qwen-asr | 基于 `models/aligner/official` 的词级时间戳 |
| **CrispASR** | C++ DLL (CPU) | 经 `align_words` ABI 的强制对齐回退 |
| **队列 worker** | asyncio 任务 | 串行批量转写，状态持久化于 `data/queue/` |
| **后端** | Python 3.13 / FastAPI / httpx | API 网关、分块、编排、安全响应头 |
| **前端** | React 19 / TypeScript / Vite / Tailwind | 界面、队列抽屉、统计、多格式导出 |

## 环境要求

- Windows 10/11
- NVIDIA GPU，CUDA 12.4+
- [Miniconda](https://docs.conda.io/en/latest/miniconda.html) 或 Anaconda
- Node.js 22+（前端构建 / CI）
- **ffmpeg** 在 `PATH` 上（分块与视频音轨提取）
- `pywebview`（可选，仅桌面壳需要 — 已声明在 `requirements.txt`）

## 配置

### GPU / 性能

调参默认值**根据检测到的显存自动选择**（启动前设置环境变量即可覆盖任意一项）：

| 显存 | ctx_size | KV 量化 |
|------|----------|---------|
| ≤ 4.5 GB | 16384 | `q4_0` |
| ≤ 8.5 GB | 32768 | `q8_0` |
| > 8.5 GB | 32768 | `f16` |

Qwen3-ASR 将音频编码为很长的多模态 token 序列（约每 10 秒音频 600 个 token），
因此长输入会被**切分为短块**、逐块独立转写，再以修正后的时间戳拼回。

| 变量 | 默认值 | 说明 |
|----------|---------|-------------|
| `VRAM_GB` | 自动检测 | 覆盖驱动上表预设的显存检测。 |
| `CTX_SIZE` | 见上表 | llama-server 上下文窗口。 |
| `KV_QUANT` | 见上表 | KV 缓存量化（`q8_0`、`q4_0`、`f16`）。 |
| `NGL` | `99` | 卸载到 GPU 的层数。 |
| `THREADS` | 自动 | llama-server CPU 线程数。 |
| `CHUNK_SECONDS` | `25` | 音频分块时长（秒）。 |
| `CHUNK_OVERLAP` | `1.5` | 相邻块重叠（秒），让跨界词保有上下文。 |

### 转写

| 变量 | 默认值 | 说明 |
|----------|---------|-------------|
| `ASR_TEMPERATURE` | `0` | 传给 llama-server 的采样温度。 |
| `ASR_LANGUAGE` | _(空)_ | 指定语言提示，代替自动检测。 |
| `ASR_MAX_RETRIES` | `2` | 每块失败重试次数，超过后触发再切分。 |
| `ASR_RESPLIT` | `1` | 失败块二分再切直至通过（设 `0` 关闭）。 |
| `ASR_PROMPT_MAX_CHARS` | `200` | 块间携带提示词的最大字符数。 |

### 限额与缓存

| 变量 | 默认值 | 说明 |
|----------|---------|-------------|
| `ASR_MAX_UPLOAD_BYTES` | 2 GiB | 单文件上传上限（前端同步校验）。 |
| `ASR_MAX_AUDIO_DURATION` | 12 小时 | 接受的最大媒体时长。 |
| `ASR_MAX_ALIGN_TEXT_CHARS` | `50000` | 单次对齐请求的文本上限。 |
| `ASR_AUDIO_CACHE_MAX_BYTES` | 4 GiB | 音轨提取缓存（`data/audio_cache/`）的 LRU 容量上限。 |
| `ASR_AUDIO_CACHE_MAX_ENTRIES` | `200` | 同一缓存的条目数上限。 |

### 批量队列

| 变量 | 默认值 | 说明 |
|----------|---------|-------------|
| `ASR_QUEUE_MAX_ENTRIES` | `500` | 队列最大条目数。 |
| `ASR_QUEUE_MAX_BYTES` | 16 GiB | 暂存上传总大小上限。 |

### 服务器与桌面壳

| 变量 | 默认值 | 说明 |
|----------|---------|-------------|
| `HOST` | `127.0.0.1` | 绑定地址。设为 `0.0.0.0` 可在可信局域网共享（无鉴权 — 仅在可信网络暴露）。 |
| `ASR_ALLOWED_HOSTS` | _(空)_ | 局域网共享时允许的 Host 名（逗号分隔）。留空 = 非回环绑定时允许任意 Host；路人浏览器仍会被 Origin/Sec-Fetch-Site 检查拦截。 |
| `SKIP_LLAMA` | `0` | 设为 `1` 时不拉起 llama-server（UI 开发 / 单元测试模式）。 |
| `ASR_NO_BROWSER` | `0` | 设为 `1` 时启动后不自动打开浏览器。 |
| `ALIGNER_BACKEND` | `gpu` | `gpu`（qwen-asr，需要 torch + `models/aligner/official`）或 `cpu`（CrispASR DLL）。 |
| `SHELL_MODE` / `ASR_SHELL_TOKEN` | — | 由打包后的桌面壳内部设置，用于启用其 HttpOnly Cookie 会话握手。请勿手动设置。 |
| `PORT` | 壳自动选择 | 桌面壳的回环端口（开发用）。 |
| `ASR_SHELL_SMOKE` / `ASR_SHELL_DEBUG` | — | 桌面壳开发旋钮：N 秒后自动关窗 / 详细日志。 |

所有响应均附带 CSP（`script-src 'self'`、`media-src blob:`）、`nosniff` 与 `no-referrer` 头；非回环绑定还会执行来源/Host 检查。

## 强制对齐

词级时间戳通过将 ASR 转写文本对齐回音频获得。对齐是**可选的** —
没有可用后端时，系统自动回退到 ASR 段级时间戳。

有两个后端可选（`ALIGNER_BACKEND`）：

1. **GPU（默认）** — 经 `qwen-asr` transformers 栈运行
   Qwen3-ForcedAligner。需要 `torch`、`transformers`、`qwen-asr`
   （默认不安装；可装进 conda 环境或嵌入式运行时：
   `pip install qwen-asr torch transformers`），并将模型放到：
   ```
   models/aligner/official/          # Qwen3-ForcedAligner-0.6B (safetensors)
   ```

2. **CPU** — 经 `bin/crispasr/crispasr.dll` 调用
   [CrispASR](https://github.com/CrispStrobe/CrispASR) C++ 运行时，配合 GGUF
   `models/aligner/qwen3-forced-aligner-0.6b-q8_0.gguf`（约 940 MB，来自
   [cstr/qwen3-forced-aligner-0.6b-GGUF](https://huggingface.co/cstr/qwen3-forced-aligner-0.6b-GGUF)）。
   不占显存，不影响 ASR 模型。该 GGUF 的 `qwen3asr` 架构标准 llama-server
   并不支持，因此需要这个定制 DLL。

后端在启动时探测所选后端，并在依赖或文件缺失时优雅降级
（GPU → CPU → ASR 段级时间戳）。

## 批量转写队列

多个文件可以入队，由串行 worker 逐个转写，保证 GPU 同一时刻只处理一个任务。

- **入队** — 上传弹窗的 *Batch* 标签页（文件夹选择 / 多选），或繁忙时直接把
  多个文件拖到窗口任意位置；每个文件以随机 ID 暂存于 `data/queue/uploads/`。
- **管理** — 拖拽或键盘重排、按名称/大小/入队时间排序、暂停/恢复 worker、
  重试失败项、清理已完成项。删除正在运行的项会在下一个分块边界协作式取消。
- **持久化** — 状态保存在 `data/queue/queue.json`（原子写、版本号便于高效轮询）；
  中断的运行在下次启动时回到 *queued* 状态，完成的任务进入常规历史记录。

## 项目结构

```
Qwen3-ASR17B-TsGUI/
├── bin/                                  # llama-server 二进制 + CUDA DLL + CrispASR（已 gitignore）
│   └── crispasr/crispasr.dll             # CPU 对齐器运行时
├── models/                               # GGUF + 对齐器模型文件（已 gitignore）
│   ├── asr/                              # Qwen3-ASR GGUF（可经 /api/models 热切换）
│   └── aligner/                          # 对齐器 GGUF（CPU）+ official/（GPU）
├── backend/
│   ├── main.py                           # FastAPI 应用（转写 + 对齐 + 模型 + 历史 + 缓存）
│   ├── llamarunner.py                    # llama-server 子进程管理（API key、健康检查）
│   ├── queue_api.py                      # 批量队列路由 + 串行 worker
│   ├── queue_store.py                    # 持久化队列状态（原子 JSON、崩溃恢复）
│   ├── forced_aligner.py                 # 经 CrispASR DLL 的 CPU 对齐器
│   ├── aligner_gpu.py                    # 经 qwen-asr 的 GPU 对齐器
│   ├── audio_chunk.py                    # ffmpeg 分块（定长 + VAD 规划辅助）
│   ├── resegment.py                      # 词级 → 字幕段的打分/合并
│   ├── text_clean.py                     # ASR 模板伪影清理 + 分块接缝去重
│   ├── history.py                        # 逐任务 JSON 历史（data/history/）
│   ├── test_main.py                      # 核心 API 测试（CI 以 mock 运行，SKIP_LLAMA=1）
│   ├── test_queue.py / test_vad.py       # 队列、分块测试
│   ├── test_subtitles.py / test_shell.py # 断句、壳/安全测试
│   └── requirements.txt
├── frontend/
│   └── src/
│       ├── App.tsx                       # 布局、单文件/批量状态机、拖放
│       ├── api.ts                        # fetch 封装（转写、队列、模型、历史）
│       ├── types.ts                      # API 响应接口
│       ├── hooks/                        # useReadiness / useJobStatus / useQueue / useElapsed
│       ├── utils/                        # subtitle.ts, format.ts, fileTypes.ts, audioClock.ts
│       └── components/                   # UploadZone, BatchUploadZone, QueuePanel, BootOverlay,
│                                         # TranscriptPanel, AudioPlayer, ProgressBar, StatsPanel,
│                                         # ExportBar, HistoryPanel, ModelInfo, icons
├── shell/
│   ├── shell_main.py                     # pywebview/WebView2 桌面壳（进程内后端）
│   ├── asr-shell.spec                    # PyInstaller 配置（无窗口 asr-shell.exe）
│   └── start-shell.bat                   # 桌面壳开发启动器
├── tools/
│   ├── build_embed.py                    # 嵌入式 Python 分发构建器
│   └── subcompare.py                     # 字幕 WER/CER 对照（SRT/VTT）
├── .github/workflows/ci.yml              # CI：前端 typecheck+build，后端 mock 测试
├── environment.yml                       # Conda 环境定义
├── requirements-runtime.txt              # 嵌入式运行时依赖（不含开发/测试依赖）
├── run.py                                # 入口：uvicorn + 轮询 readiness 后打开浏览器
├── asr-app.spec                          # PyInstaller 配置（浏览器模式 asr-app.exe）
├── setup.bat                             # 一键环境安装
├── start.bat                             # 一键启动（开发、浏览器模式）
├── build.bat                             # build.bat [app|shell|both]
├── build-embed.bat                       # 一键嵌入式构建
└── README.md
```

## 环境安装

### 一键安装

```powershell
setup.bat
```

创建 conda 环境、安装全部依赖并检查必需文件。

### 手动安装

#### 1. Conda 环境

```powershell
conda env create -f environment.yml
conda activate qwen3asr
```

#### 2. 前端依赖

```powershell
cd frontend
npm install
```

#### 3. llama-server 二进制

从 [llama.cpp releases](https://github.com/ggml-org/llama.cpp/releases/tag/b9637) 下载
`llama-b9637-bin-win-cuda-12.4-x64.zip`，解压到 `bin/`。

## 运行

### 浏览器模式（开发）

```powershell
start.bat
```

激活 conda 环境、构建前端并启动服务。浏览器打开 **http://localhost:8000**。

### 桌面壳（开发）

```powershell
shell\start-shell.bat
```

在 1280×820 的 WebView2 窗口（pywebview）内以进程方式运行后端：随机回环端口、
一次性令牌握手换取 HttpOnly Cookie、将外部 URL 转交系统浏览器的导航守卫、
单实例互斥锁，日志位于 `data/logs/asr.log`。WebView2 不可用时回退系统浏览器。

### 手动启动

```powershell
conda activate qwen3asr
cd frontend && npm run build && cd ..

python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

### 开发模式

```powershell
# 终端 1：后端（跳过 llama-server，UI 开发用）
conda activate qwen3asr
$env:SKIP_LLAMA = "1"
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000

# 终端 2：前端开发服务器（热更新）
cd frontend
npm run dev
```

Vite 开发服务器将 `/api` 请求代理到 8000 端口的后端。

### 打包构建

```powershell
build.bat app      # PyInstaller → Qwen3-ASR\        (asr-app.exe，系统浏览器界面)
build.bat shell    # PyInstaller → Qwen3-ASR-Shell\  (asr-shell.exe，WebView2 窗口)
build.bat both     # 以上两者
build-embed.bat    # tools/build_embed.py → Qwen3-ASR-Embed\
                   # 嵌入式 Python 3.12 运行时，不经 PyInstaller；源码可编辑且
                   # pip 可用，可用 runtime\python.exe -m pip install qwen-asr torch
                   # transformers 就地恢复 GPU 对齐
```

## API 端点

| 方法 | 路径 | 说明 |
|--------|------|-------------|
| `GET` | `/api/health` | `{ backend, llama_server, aligner_available, aligner_running, aligner_backend }` |
| `GET` | `/api/readiness` | 启动序列各阶段（模型加载 / 预热 / 对齐器检查） |
| `POST` | `/api/readiness/retry` | 启动终态失败后重新执行启动序列 |
| `GET` | `/api/status` | 当前任务状态 `{ status, progress, message }`（含 `cancelled`） |
| `POST` | `/api/transcribe` | 上传音频/视频，返回 `{ text, segments[], stats, history_id }` |
| `POST` | `/api/abort` | 在下一个分块边界取消正在进行的转写 |
| `POST` | `/api/align` | 独立强制对齐（音频 + 文本 → 词级时间戳） |
| `GET` | `/api/models` | 列出 `models/asr/` 下的 GGUF + 当前模型 + 调参 |
| `POST` | `/api/models/switch` | 热切换已加载的 ASR 模型（忙时拒绝） |
| `GET` | `/api/history` / `GET·DELETE /api/history/{hid}` / `DELETE /api/history` | 历史列表 / 单条 / 删除 / 清空 |
| `GET` | `/api/audio-cache` / `DELETE /api/audio-cache` | 音轨缓存列表 / 清空 |
| `GET` | `/api/audio-cache/{name}` | 流式返回缓存音频（驱动内置播放器） |
| `POST` | `/api/queue/items` | 批量入队（multipart，多文件） |
| `GET` | `/api/queue` | 队列快照 `{ items[], paused, active_id, version }` |
| `POST` | `/api/queue/reorder` / `/api/queue/sort` | 手动重排 / 规则排序 |
| `DELETE` | `/api/queue/items/{qid}` | 移除条目；若正在运行则协作式取消（跳过） |
| `POST` | `/api/queue/items/{qid}/retry` | 重新入队失败/已取消的条目 |
| `POST` | `/api/queue/pause` / `/api/queue/resume` | 暂停 / 恢复 worker |
| `DELETE` | `/api/queue/finished` | 清空已完成/已取消条目 |

### 转写参数

| 字段 | 类型 | 默认 | 说明 |
|-------|------|---------|-------------|
| `file` | file | 必填 | 音频或视频文件 |
| `align` | bool | `false` | 启用强制对齐以获得词级时间戳 |

### curl 转写示例

```powershell
# 基础转写
curl -X POST http://localhost:8000/api/transcribe -F "file=@audio.wav"

# 带强制对齐
curl -X POST http://localhost:8000/api/transcribe -F "file=@audio.wav" -F "align=true"

# 批量入队
curl -X POST http://localhost:8000/api/queue/items -F "files=@a.mp3" -F "files=@b.mp4"
```

### 响应格式

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
# 后端测试（单元测试时跳过 llama-server 拉起）
conda activate qwen3asr
$env:SKIP_LLAMA = "1"
python -m pytest backend\test_main.py -v                              # 核心 API（CI 以 mock 运行）
python -m pytest backend\test_queue.py backend\test_vad.py -v         # 队列 + 分块
python -m pytest backend\test_subtitles.py backend\test_shell.py -v   # 断句 + 安全

# 前端类型检查 + 生产构建
cd frontend
npm run typecheck
npm run build
```

CI（`.github/workflows/ci.yml`）在每次 push/PR 时以 Node 22 运行前端
typecheck/build，以 Python 3.13 运行后端 `compileall` + mock 版 `test_main.py`。

## 工具

```powershell
python tools\subcompare.py ref.srt hyp.srt            # WER / CER / 时间戳漂移
python tools\subcompare.py ref.srt hyp.srt --html report.html
```

`subcompare.py` 解析 SRT/VTT（自动探测编码），按时间段配对片段，以 TUI 表格、
HTML 报告或 JSON 输出 WER、CER 与起止时间漂移 — 便于 A/B 对比 ASR 或断句改动。

## 模型文件

模型文件放在 `models/` 下（已 gitignore — 从下方链接或 GitHub Release 下载）。
后端在启动时自动探测各文件，缺失的功能会优雅停用。放入 `models/asr/` 的
其他 `.gguf` 会自动出现在 `/api/models` 中，支持热切换。

| 文件 | 位置 | 大小 | 必需 | 说明 |
|------|----------|------|----------|-------------|
| `Qwen3-ASR-1.7B-Q8_0.gguf` | `models/asr/` | ~2.2 GB | 是 | 主 ASR 模型（Q8 量化） |
| `mmproj-Qwen3-ASR-1.7B-Q8_0.gguf` | `models/asr/` | ~356 MB | 是 | ASR 多模态投影器（Q8） |
| `qwen3-forced-aligner-0.6b-q8_0.gguf` | `models/aligner/` | ~940 MB | 否 | ForcedAligner 模型（Q8，CPU 后端） |
| Qwen3-ForcedAligner-0.6B (safetensors) | `models/aligner/official/` | ~1.8 GB | 否 | ForcedAligner（GPU 后端，经 qwen-asr） |

- ASR 模型：[ggml-org/Qwen3-ASR-1.7B-GGUF](https://huggingface.co/ggml-org/Qwen3-ASR-1.7B-GGUF)
- 对齐器 GGUF：[cstr/qwen3-forced-aligner-0.6b-GGUF](https://huggingface.co/cstr/qwen3-forced-aligner-0.6b-GGUF)

## 许可证

模型文件遵循 Qwen 原始许可证。应用代码按“原样”提供。
