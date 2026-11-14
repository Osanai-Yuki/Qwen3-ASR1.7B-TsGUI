# Gizmodo v1 修复验收记录

日期：2026-10-30。分支：`feat/gizmodo-workspace`。

**结论：A01–A11 全部有对应的代码改动和验证证据。** 其中 9 项在真实运行程序上逐项验证通过；A07 的"桌面/手机两套滚动容器分别保存位置"与 A10 的"桌面外壳退出确认"两项只完成了一部分，已在下文明确标为**未完全验证**，并写明原因与复验步骤，不计为通过。

对照依据：[设计方案](../../design/gizmodo-v1/README.md) 第 2–6 节、[修复方案](../plans/gizmodo-v1-repair-handoff.md)、[上一轮验收报告](../gizmodo-v1-20261030/README.md)。

## 1. 运行环境与证据范围

| 项目 | 实际值 |
| --- | --- |
| 项目解释器 | `E:\anaconda3\envs\qwen3asr\python.exe`，实测 **Python 3.12.13**（全局 `python` 为 3.15.1，未使用） |
| 后端真实服务 | `127.0.0.1:8765`，`readiness.ready=true`，模型 `Qwen3-ASR-1.7B-Q8_0`，对齐器 `gpu:qwen-asr` |
| 前端 | 生产构建 `frontend/dist`（`index-Byki_hca.js` md5 `7c307fe2abe5140a5e863f11ceb59e5e`，`index-B7udBvg5.css` md5 `b9ecb1363036bde4ed08211348163e42`） |
| 隔离 runtime | `build/acceptance-repair-20261030-r1/runtime`，含 `backend/`、`shell/`、`run.py`、`frontend/dist`；`bin`、`models` 为指向项目目录的 junction |
| 真实音频 | Windows SAPI 合成：`real-short.wav`（7.777s）、`real-long.wav`（25.3s）、`real-300s.wav`（161.4s） |
| 用户数据 | 全程未使用。验收数据写在 `build/acceptance-repair-20261030-r1/runtime/data`，未对用户 `data/` 执行导入、删除或取消 |

隔离 runtime 中的数据为虚构记录：205 份超出 200 条上限的旧格式历史、同一秒生成 v1/v2 的同一任务、完整/部分/取消/媒体缺失记录、保存失败项、包内重名与清单同名结果。

## 2. 逐项结果

| 编号 | 状态 | 验证方式 |
| --- | --- | --- |
| A01 导入身份 | **通过** | 真实界面 + 后端用例 |
| A02 搜索/查找分离 | **通过** | 真实界面 |
| A03 分页不重复 | **通过** | 真实界面（72 条开返回三次） |
| A04 保存失败恢复 | **通过** | 真实故障注入 + 重启 |
| A05 版本选择 | **通过** | 真实界面 + 后端用例 |
| A06 后台刷新 | **通过** | 真实界面 + 网络时间线 |
| A07 窄屏入口与滚动 | **部分通过** | 入口与位置恢复通过；桌面/手机两套容器分别保存未分别取证 |
| A08 导出路径与清单 | **通过** | 真实导出并解包校验 |
| A09 字号/触控/布局 | **通过** | 真实界面多处量测 |
| A10 交互补齐 | **通过** | 真实界面（退出说明除外，见 A10 小节） |
| A11 时间能力表达 | **通过** | 真实无对齐结果 + 真实对齐记录 |

### A01 · 普通 File 同名同大小不再静默丢弃

**改动位置**：`frontend/src/utils/importSession.ts`（新增）、`frontend/src/Workspace.tsx`（`addFiles`/`submitImport`）、`frontend/src/api.ts`（`enqueueFiles`）、`backend/queue_api.py`（`queue_add` 的 `client_import_ids`）、`backend/queue_store.py`（`add` 增加 `client_import_id`）。

**实际验证**：在真实导入对话框中用 `DataTransfer` 注入两个同名、同长度（2048 字节）、**内容不同**（分别填充 0x01 与 0x07）的 `File`：

- 修复前清单只剩 1 项；修复后 `发现 2 项 · 已选 2 项`，两行都显示，来源标注为"来源目录未提供"而不是编造绝对路径。
- 两项各自可勾选；移除第一项后第二项保留。
- 模拟后端只接受第一项的 413 回执：`2 项 → 1 项`，对话框保持打开并提示"已提交文件已从清单移除，其余文件仍保留"。

后端用例：`test_client_import_ids_are_echoed_per_file`、`test_client_import_ids_validation`（长度不符/重复/非法 JSON 均 422，缺字段仍可用）、`test_partial_accept_returns_only_the_accepted_prefix`。

### A02 · 全局搜索与文内查找分离

**改动位置**：`frontend/src/Workspace.tsx`（reader 加载 effect、`openReaderFromLibrary`、新增 `asr-reader-tools`）。

**实际验证**（真实界面）：搜索"历史验收记录-000"命中 1 份，打开后：

- 文内查找框为**空**（不再把全局词写进查找条件），正文完整可见 1 段（修复前为 0 行）。
- 后端 `match_fields`/`first_match` 正确：标题命中 `['title','filename']` / `field='title'`；正文命中 `[]` / `field='text'`。
- 零命中有明确反馈："本份查询没有匹配结果…清除关键词与筛选"，并保留条件不自动重置。

### A03 · 加载更多后返回不重复追加

**改动位置**：`frontend/src/hooks/useHistoryResults.ts`（新增，按 offset 存页、替换不追加、generation + AbortController 防竞态）、`frontend/src/Workspace.tsx`。

**实际验证**（真实界面，1280×820）：加载到 **72 条（72 唯一）**，连续"打开结果 → 返回"三次，每次仍是 **72 条 / 72 唯一**，"已显示 72 / 216"。修复前第二次返回会变成 108 条含 72 唯一。

修复过程中发现并修掉两个自身引入的问题：一是 reader 在 `enabled` 翻转时重复拉取导致返回后 72 塌回 36；二是查询词变化时清空了页面却没有重新请求。两者都在真实界面上复验通过。

### A04 · 保存失败后可读、可恢复、不计入成功数

**改动位置**：`backend/pending_results.py`（新增）、`backend/queue_api.py`（worker 保留 payload、新增 `GET /api/queue/items/{qid}/result` 与 `POST /api/queue/items/{qid}/save`、`reconcile_attempts`）、`backend/queue_store.py`（`begin_attempt`/`set_attempt_version`/`set_persistence`）、`backend/main.py`（`_save_result_payload`、注入 collaborator）。

**真实故障注入**：用 `icacls … /deny Everyone:(W)` 把隔离 runtime 的 `data/history` 设为不可写，然后走**真实转写**：

| 观测 | 结果 |
| --- | --- |
| 队列状态 | `done`，`persistence_state=failed`，`error=history_save_failed`，`history_id=None` |
| 暂存原文件 | **保留**（`staged kept: True`） |
| `GET /api/queue/items/{qid}/result` | `kind=temporary`，正文为真实识别文字 |
| 结果库总数 | **不增加**（保存失败项未计入可长期回访的成功数） |
| 任务页 | "结果未保存 · 转写已完成，本次会话可读，长期记录保存失败"，动作含"查看未保存 / 重新保存 / 详情" |

**恢复**：解除目录写保护后点击"重新保存" → 提示"已保存到长期记录"，状态转为"已完成"，结果库可查到该记录且 `result_version=1`（保存重试**没有**重新识别，也没有取新版本号）。

**重启**：保留写保护的状态下重启后端，`GET /result` 仍返回 `kind=temporary` 与真实正文，队列行仍为 `failed / temporary_result_available / can_retry_save`。

**幂等与边界**（后端用例）：重复保存返回同一 `history_id` 且不二次写库；旧 attempt 迟到的保存返回 409；payload 缺失返回 410；恢复文件写失败与会话内快照被删除（`pending()` 抛错）时转写结果本身不被掩盖。

### A05 · 按 result_version 选择最新结果

**改动位置**：`backend/history.py`（`reserve_version`、`save` 接受显式版本、`batches()` 改为按版本选择）、`backend/queue_api.py`（识别前预留版本）、`backend/queue_store.py`。

**实际验证**：

- 真实界面批次摘要中"同秒任务"显示 **v2**（`4a4703ab9c2c`），同任务只剩一行。
- 查看 v1 会改写它的 mtime，再读批次仍是 v2（后端用例 `test_same_second_versions_keep_latest_after_view_and_mtime` 还额外把 v1 的 mtime 提前 500 秒、并用另一个 `HistoryStore` 实例复核）。
- 版本预留可复用：`reserve_version` 连续调用返回同一编号；保存重试沿用该编号；新一次执行才取下一个号。
- 无 `task_id` 的旧记录按各自 `history_id` 独立展示，不按文件名猜测为同一任务。

**注意**：`batches()` 与 `search()` 现在用 `(created_at, id)` 作为稳定次级排序；`list()` 亦然。这解决了同秒记录在分页中互换位置的问题。

### A06 · 后台完成后结果库更新

**改动位置**：`frontend/src/Workspace.tsx`（`savedSignature` → `invalidateToken`）、`frontend/src/hooks/useHistoryResults.ts`。

**实际验证**：连续 8 次真实转写（7.8s–161s 音频）期间页面持续停留在结果库，不做任何点击或刷新，结果库计数由 219 依次变为 **220、221、222、223、224、225、226**，且新结果出现在列表顶部。

`performance` 资源时间线记录了页面**自行发起**的 `/api/history/search?limit=36&filter=all` 请求（t≈115s、158s、241s、356s、418s），即失效确实由队列快照驱动，而不是用户操作。

**诚实的限制**：转写耗时（约 0.12 RTF）短于工具调用往返时间，多次尝试都没能采到"处理中"的中间帧。因此"处理中不跳数、完成才跳数"这一半只有代码路径与资源时间线佐证，没有逐帧截图。

### A07 · 窄屏入口与阅读位置

**改动位置**：`frontend/src/index.css`（`.asr-nav-panel`、`.asr-nav-panel-toggle`、`@media (max-height:560px)`）、`frontend/src/Workspace.tsx`（`scrollAdapter`、`captureReaderPositionFromDom`、`applyScrollRatio`、`reader_position` 保存与恢复）。

**实际验证**（真实界面，390×844，DPR 3）：

- 导航旁出现"搜索与批次"触发器（`display:flex`，桌面为 `none`）；展开后搜索框距页顶 **158px**（修复前 8325px），批次按钮 2 个可见。
- 390px 下无横向溢出。
- 阅读位置：把 40 段长文滚到 2433/3041（比例 0.80），后端存下 `reader_position={segment_index:24, scroll_ratio:0.70}`，且 `playback_position` 保持 7 不被滚动覆盖；返回再打开后滚动位置**精确恢复到 2433（比例 0.80）**。

**未完全验证的部分**：手机与桌面分别使用 window / `main` 两套滚动容器的**分别**取证没有完成——390px 与 1280×820 各自的位置恢复都通过了，但没有在同一次会话里切换断点后对比两套锚点。复验步骤见第 4 节。

### A08 · 先定最终包内路径再生成清单

**改动位置**：`frontend/src/utils/exportPlan.ts`（新增 `buildExportPlan`/`allocateExportPaths`/`zipBlob`）、`frontend/src/Workspace.tsx`（`exportNow`）。

**实际验证**：在真实界面选中"同名目录"下的 3 份结果（含与清单同名的 `export-manifest.json`）导出 TXT，拦截真实产出的 `Blob` 并解包：

| 检查 | 结果 |
| --- | --- |
| 包内条目 | `同名目录/重复名称__b75b70d0.txt`、`同名目录/重复名称.txt`、`同名目录/export-manifest.txt`、`export-manifest.json` |
| 条目唯一 | 是（4 条，大小写不敏感去重后仍 4） |
| **manifest 路径 == 包内文件** | **是**（修复前两条 included 都写成 `重复名称.txt`） |
| CRC32 | 4 条全部正确 |
| 内容对应 | 每条 included.path 都能取到非空内容 |
| 冲突后缀稳定性 | 后缀取结果身份前 8 位（`b75b70d0`），与选择顺序无关 |

`zipBlob` 现在**禁止**内部改名，重复名直接抛错——这正是当初清单与包内不一致的根因。

### A09 · 字号、触控区与任务滚动布局

**改动位置**：`frontend/src/index.css`（重写为 tokens→shell→pages→dialog→responsive，重要状态最小 13px、正文 18px/1.9、`--touch:44px`、任务页头 68px、矮窗口整页滚动）。

**实际量测**（真实界面计算样式）：

| 项目 | 修复前 | 实测 |
| --- | --- | --- |
| 任务状态字号 | 11px | **13px** |
| 来源/辅助信息 | 10px（手机 9px） | **12px** |
| 手机任务操作高度 | 28px | **44px**（查看结果/重新处理/查看缺失 均 44） |
| 阅读正文 | 17px 桌面 / 15px 手机 | **18px / 行高 34.2px（1.9）**，390px 与 1280px 一致 |
| 任务页页头 | 88px | **68px**（任务模式） |
| 1280×420 任务表 | 表格从 y=419 开始，几乎不可见 | `.asr-main` 与 `.asr-task-scroll` 均为 `overflow:visible`，整页滚动；滚动到 y=313 时行内动作完整可见（44px） |
| 任务页通用页脚 | 占位 | 任务模式隐藏（内容并入任务详情） |

### A10 · 交互补齐

**改动位置**：`frontend/src/utils/preferences.ts`（新增）、`frontend/src/Workspace.tsx`（列表样式、段落/字幕切换、任务详情、批量范围说明、暂停/退出说明）、`frontend/src/utils/subtitle.ts`。

**实际验证**：

- **偏好持久化**：结果库"切换紧凑列表"→ `localStorage['qwen-asr-workspace-prefs:v1']` 写入 `listStyle:"compact"`，标签变为"切换两列版面"；阅读器"切换字幕行"→ `readerView:"subtitle"`。词级时间戳选项**在勾选时即写入**（修复前只在提交成功时写，关闭重开就丢）。
- **空状态区分**：首次空白（"还没有转写结果" + 新建入口）、查询无命中（"本份查询没有匹配结果" + 清除条件入口）、加载失败（"读取结果库失败" + 重试）三者是不同文案与动作。
- **段落/字幕切换**：2 段原始 segments 在段落视图聚成 2 个阅读块，切换后 2 行字幕；切换**不修改** `segments`，五种导出格式内容不变（后端 `test_subtitles.py` 11 项通过）。
- **任务详情**：无结果的文件标题可打开详情，展示完整文件名、来源、导入时间、时长（未知显示"待检测"）、处理配置、当前阶段、原因——不再只是禁用标题。
- **多选范围**：批量栏写明"选择范围：当前批次已加载的任务，可跨筛选保留"，并给出可导出/可重试计数。
- **暂停/退出说明**：暂停时写"文件会等待，需要点击'继续处理'才会开始下一项"；页脚与设置页写明"退出桌面应用会停止本地处理；重开后未完成文件从头处理，已保存结果保留"。

**未完全验证的部分**：桌面外壳（`shell/shell_main.py`）的原生关闭确认没有实现，也没有验证。本轮按要求**没有**把退出说明做成"通用 JS→Python 桥"，只保留了文字说明；`pywebview` 的事件与确认接口未经核实，因此不实现也不声称实现。

### A11 · 时间能力如实表达

**改动位置**：`backend/history.py`（`derive_timing_info`）、`backend/main.py`（结果回传 `timing`）、`frontend/src/Workspace.tsx`（`timingLabel`）、`frontend/src/utils/subtitle.ts`（`subtitleCues`）。

**实际验证**：

- 真实无对齐结果（10.272s 音频，`segments=[]`）：`timing.level="none"`、`fallback_reason="alignment_not_requested"`；阅读页显示"没有分段定位时间戳 · 本次未请求对齐，没有分段时间戳"，而**不是**"时间戳来自转写结果"。
- 真实对齐记录：`level="segment"`、`source="forced_alignment"`、`alignment_succeeded=true`、`fallback_reason=null`；页面显示"使用词级对齐生成的句段时间"。
- 强制对齐后经过 `resegment_words` 仍报告 `segment`，不谎报 `word`（后端用例 `test_timing_after_resegment_is_still_segment_level`）。
- 段落/词级判定基于实际数据：`start/end` 有限且有序才可用；乱序 segments 判为 `none`。
- 导出：`subtitleCues` 只在**确有**时长时合成整段粗略 cue 并标 `coarse:true`；时长未知时输出明确说明"没有生成有效时间范围…"而不是全零时间码。清单逐份记录 `timing.{level, coarse, duration_known}`。

## 3. 测试结果

| 检查 | 结果 |
| --- | --- |
| 原有后端测试 | **95 项通过**（与上一轮基线一致） |
| 上一轮新增验收用例 `checks_backend.py` | **9/9 通过**（修复前 7/9，其中 A04、A05 失败；实际复现时为 6/9，`test_versions_and_single_task_in_batch` 也受同一 mtime 缺陷影响） |
| 本轮新增回归 `backend/test_repair.py` | **39 项通过**（导入身份 3、保存失败恢复 8、崩溃窗口 1、版本 5、时间能力 5、阅读/播放位置 3、pending store 4、历史健壮性 6、attempt 生命周期 3、完成清理保留保存失败项 1、并发保存/删除回收 3） |
| 合计 | **143 项通过**，`pytest backend docs/acceptance/gizmodo-v1-20261030/checks_backend.py` |
| 前端 typecheck | 通过（`tsc --noEmit`，`strict` + `noUnusedLocals` + `noUnusedParameters`） |
| 前端生产构建 | 通过（`vite build`） |
| 隔离 runtime 复跑 | 143 项通过，`python -c "import backend.main"` 成功 |

**端口干扰记录**：真实模型服务运行期间跑同一套测试，`test_llama_runner_stops_process_on_startup_failure` 会因 8080 被占用而失败。关闭真实服务后复跑全绿——与上一轮记录的干扰一致，不是回归。

新增用例覆盖（对应修复方案第 14 节）：R01/R02 导入身份与回执、R08/R09 保存失败与快照错误、R10 保存重试幂等与旧 attempt、R11 取消后保存失败、R12 提交成功但关联丢失、R13 同秒版本与 mtime、R15/R16 位置独立与校验、R19 版本预留、R27 时间能力。

## 4. 未验证与受环境限制的项目

1. **A07 两套滚动容器分别取证**：390px（window 滚动）与 1280×820（`main` 滚动）的位置保存/恢复各自通过，但没有在同一次会话内切换断点后对比两套锚点。复验：在同一结果上分别在 390px 与 1280px 滚动并返回，比较两次恢复的段落是否与各自滚动位置一致。
2. **A10 桌面外壳关闭确认**：未实现、未验证。`shell/shell_main.py` 目前仍是 `window.closed` 时关闭后端，没有原生确认，也没有开放 JS bridge。按本轮约束不引入通用 JS→Python 执行桥。
3. **A06 中间帧**：未取得"处理中"截图，原因见 A06 小节。
4. **真实浏览器 200% 缩放**：本轮用 CDP 视口模拟（1280×820、390×844、1280×420）完成量测，**没有**在真实桌面 WebView2 上做 200% 缩放验收。640 CSS px 等效重排不能替代。
5. **对齐器全链路质量**：健康接口确认 `gpu:qwen-asr` 可用，真实记录也显示 `alignment_succeeded=true`，但没有评估词级对齐的准确率。
6. **辅助技术与对比度全量审计**：只做了本轮改动相关的字号、触控高度、焦点样式与本地字体加载检查，没有做完整 WCAG 审计。

## 5. 保留的已通过行为（回归确认）

| 行为 | 证据 |
| --- | --- |
| 队列串行；暂停等当前文件完成 | 后端用例 + 真实队列 `active_id` 单值 |
| 取消保留部分结果与来源 | 真实 161s 音频取消：`cancelled` / `persistence_state=saved` / `history_id=8eee3f8d79b7` |
| 读 A 时 B 完成不换正文/音轨/播放器 | 真实界面：B 完成（`done/saved`）后 A 的 reader 节点与 audio 节点仍是**同一实例**，正文、查找词"第 3 段"、高亮数 1、播放位置 2.5s 全部不变 |
| 查看排序不改调度 | 后端用例 `test_store_reorder_only_queued` / `test_store_sort_by`；界面排序只作用于 `taskRows` |
| 超过 200 份仍可搜 | 真实界面搜"梧桐计划"命中第 206 条记录 |
| 缺失音轨时文字可读 | 真实记录 `音轨缺失的文字结果` 无 `audio_cache_name`，正文正常显示 |
| 音轨清理与文字删除分开 | `clearAudioCache` 与 `deleteHistory` 仍为独立接口 |
| 部分结果默认排除 | 导出对话框与用例一致 |
| 本地字体与离线渲染 | 真实界面 `font-family` 为 Special Gothic Expanded One，`performance` 中**零外部资源请求** |
| `queue.json` 轻量 | 队列摘要不内嵌正文，只有 `attempt_id`/`persistence_state` 等标志位 |

## 6. 实际截图

| 页面 | 文件 |
| --- | --- |
| 结果库 1280×820 | [01-library-1280.png](screenshots/01-library-1280.png) |
| 任务中心 1280×820 | [02-tasks-1280.png](screenshots/02-tasks-1280.png) |
| 阅读器 1280×820 | [03-reader-1280.png](screenshots/03-reader-1280.png) |
| 结果库 390×844（窄屏入口展开） | [04-library-390-navpanel.png](screenshots/04-library-390-navpanel.png) |
| 任务中心 390×844 | [05-tasks-390.png](screenshots/05-tasks-390.png) |
| 任务中心 1280×420 矮窗口 | [06-tasks-1280x420-short.png](screenshots/06-tasks-1280x420-short.png) |
| 导入对话框（同名文件两项） | [07-import-duplicates-1280.png](screenshots/07-import-duplicates-1280.png) |
| 保存失败后已恢复 | [08-save-failed-recovered-1280.png](screenshots/08-save-failed-recovered-1280.png) |
| 保存失败任务行 | [09-save-failed-task-1280.png](screenshots/09-save-failed-task-1280.png) |
| 临时结果阅读页 | [10-temporary-result-1280.png](screenshots/10-temporary-result-1280.png) |

机器可读证据：[probe.json](probe.json)（API 逐项观测）、[ui.json](ui.json)（界面断言）、[live.json](live.json)（真实转写/取消/隔离）、[zip.json](zip.json)（真实 ZIP 解包校验）、[tests.xml](tests.xml)（JUnit）。

## 7. 变更清单

| 文件 | 变更 |
| --- | --- |
| `backend/history.py` | 重写：原子写、`reserve_version`、`derive_timing_info`、按版本选批次、独立阅读/播放位置、`match_fields`/`first_match`、稳定排序 |
| `backend/pending_results.py` | 新增：保存失败 payload 的会话内 + 磁盘恢复存储 |
| `backend/queue_store.py` | `attempt_id`/`result_version`/`persistence_state` 等字段与 `begin_attempt`/`set_persistence` |
| `backend/queue_api.py` | worker 保留 payload、`client_import_ids`、`result`/`save` 端点、`reconcile_attempts` |
| `backend/main.py` | 结果回传 `timing`/attempt/version、`_save_result_payload`、view 端点转发新字段、注入 collaborator |
| `backend/test_repair.py` | 新增 39 项回归 |
| `frontend/src/Workspace.tsx` | 重写：导入身份、分页、reader 隔离、滚动容器、导出计划、偏好、任务详情、时间能力 |
| `frontend/src/hooks/useHistoryResults.ts` | 新增：分页缓存与失效 |
| `frontend/src/utils/exportPlan.ts` | 新增：导出路径分配与 ZIP 编码 |
| `frontend/src/utils/importSession.ts` | 新增：导入项身份与回执 |
| `frontend/src/utils/preferences.ts` | 新增：版本化偏好 |
| `frontend/src/utils/subtitle.ts` | 导出 cue 来源与粗略回退标注 |
| `frontend/src/api.ts`、`frontend/src/types.ts` | 契约扩展 |
| `frontend/src/index.css` | 重写：tokens 化 + 响应式与矮窗口布局 |

`backend/history.py` 中上一轮加入的 `from __future__ import annotations` 已保留。

### 8.1 独立对抗性评审发现并修复的问题

交付前用三条独立视角（后端正确性、前端状态、回归风险）对本轮改动做了对抗性评审，并对每条结论逐一复验。评审发现的 5 个真实问题均已修复并在真实运行程序上复验：

| 发现 | 修复 |
| --- | --- |
| 保存重试返回不可用 id 时仍把任务标记为 `saved`，`history_id` 为 None——正是"把临时结果冒充已保存" | 校验必须是 12 位 hex，否则 500 并保留 payload |
| `queue_item_save` 是"读后写"，两个并发请求会各自提交，产生同版本的两份历史 | 新增 `try_claim_save` 原子认领；并发时只有一方执行，另一方 409 |
| 保存失败项的 `status` 是 `done`，导致 `/retry` 返回 409、保留的原文件永远无法重新处理 | 改为 `error` + `history_save_failed`（与修复方案的状态表一致） |
| `detach_history` 只清 `history_id`，不重置 `persistence_state`，删除历史后批次行仍声称已保存 | 一并重置为 `unavailable` 并清空 attempt/flags |
| `store.remove()` 不回收 pending payload，删除后 payload 在内存与磁盘泄漏并被当作孤儿恢复 | `queue_delete` 在移除项时同步删除对应 payload |

评审同时确认了并发、重复、异常三条路径：并发保存只执行一次（`save calls: 1`，另一方 409）；返回空 id 时状态回到 `failed` 且可重试；`save_fn` 抛异常时 payload 保留、认领释放。

验证过程中另外发现并修掉 3 个自身引入的问题（都在真实界面上复验）：

1. reader 加载 effect 依赖了一个每次渲染都新建的 ref 对象，导致无限重取、标题停在上一条记录。改为依赖稳定的字符串键。
2. 临时结果的 `recordId` 与分页列表的复合 id 形式不一致，`indexOf` 失配使"下一份"回到自身。统一为 `task:attempt` 形式。
3. 保存失败项的 `status` 与"重新处理"按钮的显示条件不一致（见上表第 3 行），修复后任务页会同时提供"重新保存"和"重新处理"。

本轮未提交、未推送、未合并。
