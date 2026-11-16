# Gizmodo v1 复查问题修复报告（第二轮）

日期：2026-10-31。分支：`feat/gizmodo-workspace`。范围：逐项修复[独立复查报告](../gizmodo-v1-repair-review-20261031/README.md)第 3–4 节的 R01–R10。

**结论：R01–R09 已由自动测试闭环；R10 的选择范围、批量重试与桌面关闭确认已完成实现，其中跨筛选选择和批量重试通过真实浏览器检查，桌面原生对话框完成处理器单测但未做真实 WebView2 点击验收。** 本轮没有重新执行真实 GPU 转写、200% 缩放、对比度/辅助技术、深分页竞态矩阵和真实 ZIP 下载解包；A01/A03/A06/A09/A11 的既有未验证边界仍然保留，不能据此宣布 A01–A11 全部验收通过。

## 1. 修复结果

### R01 · 恢复存储不可用时文字仍不可读

- 会话内 payload 现在是独立的第一副本，写入任何队列状态之前先保存；恢复目录/队列写入失败不再跳过保留文字。[queue_api.py](../../../backend/queue_api.py#L815)
- `GET /result`、保存、删除统一通过 `_read_pending` 读取：先尝试磁盘恢复存储，异常时回退会话副本，不再返回 409/500。[queue_api.py](../../../backend/queue_api.py#L114)
- `temporary_result_available` / `can_retry_save` 只按实际可读性设置；取消分支也先保留再写状态。[queue_api.py](../../../backend/queue_api.py#L725)
- 回归：复查的 `test_pending_directory_failure_keeps_readable_session_copy`、`test_cancelled_queue_write_failure_keeps_partial_payload` 通过，并已固化到 [test_repair_review.py](../../../backend/test_repair_review.py)。

### R02 · 重新保存与重新处理并发互相覆盖

- 新增 `requeue_for_reprocess`：`saving` claim 未释放时 `POST /retry` 返回 409，不再两边都报成功。[queue_store.py](../../../backend/queue_store.py#L346)、[queue_api.py](../../../backend/queue_api.py#L414)
- 删除同样在保存期间返回 409；[queue_api.py](../../../backend/queue_api.py#L383)
- 保存完成后用 `commit_save` 在锁内重新核对 attempt 和 claim，只有仍属于本次保存才回写状态、删除来源；否则返回冲突，不覆盖新所有权。[queue_store.py](../../../backend/queue_store.py#L374)、[queue_api.py](../../../backend/queue_api.py#L509)
- 回归：`test_save_retry_blocks_reprocessing_until_claim_is_resolved`、`test_delete_is_rejected_while_a_save_owns_the_attempt` 通过。

### R03 · 重启后持久化的 saving 标记永久阻塞保存

- `QueueStore._load` 在启动时释放所有上一进程遗留的 `saving` claim；[queue_store.py](../../../backend/queue_store.py#L63)
- `reconcile_attempts` 重新核对 attempt 的历史与 pending payload：已提交则补关联，未提交且 payload 可读则退回 `failed`/可重试，不可读则清为 `unavailable` 并撤下假能力。[queue_api.py](../../../backend/queue_api.py#L140)
- 回归：`test_restart_releases_a_stale_saving_claim`、`test_interrupted_save_can_be_retried_after_restart`、`test_reconcile_clears_capabilities_without_a_readable_payload` 通过。

### R04 · 取消结果重新保存后源文件被删

- 保存成功后的来源清理与文字持久化分开：只有非取消任务才删除暂存来源，取消任务保留从头重跑能力。[queue_api.py](../../../backend/queue_api.py#L615)
- 启动 reconcile 采用同一策略；已提交的取消任务保持 `cancelled` 状态并保留来源。[queue_api.py](../../../backend/queue_api.py#L176)
- 回归：`test_cancelled_result_still_reprocessable_after_save_retry`、`test_reconcile_keeps_cancelled_status_and_source` 通过。

### R05 · 特定重名导出在主线程无限循环

- 冲突后缀中的计数现在进入最终文件名（`__<id8>-2`、`-3`…），不再被 8 位截断吞掉；manifest 保留名使用同一规则。[exportPlan.ts](../../../frontend/src/utils/exportPlan.ts#L92)
- 顺带修正绝对盘符规范化：`C:` 在替换非法字符前被丢弃。[exportPlan.ts](../../../frontend/src/utils/exportPlan.ts#L65)
- 回归：新增 `frontend/tests/exportPlan.test.mjs`，用 3000ms 子进程看门狗保护，覆盖普通冲突、三个共享前 8 位 ID、选择顺序变化、大小写碰撞、manifest 重名和 ZIP 重复名拒绝。`npm run test:export-plan` 通过。

### R06 · 手机新阅读位置未保存，刷新/断点切换丢位

- 滚动事件改为同时绑定 `window` 与真正的滚动容器；节流增加尾随保存，用户停下时的最后位置不会被丢掉。[Workspace.tsx](../../../frontend/src/Workspace.tsx#L955)、[Workspace.tsx](../../../frontend/src/Workspace.tsx#L904)
- `pagehide` / `visibilitychange` 用 PATCH + `keepalive` 提交最后位置；`sendBeacon` 不能发 PATCH，未使用。[api.ts](../../../frontend/src/api.ts#L126)、[Workspace.tsx](../../../frontend/src/Workspace.tsx#L1008)
- 恢复逻辑统一为 `scrollToAnchor`：字幕行、段落视图、纯文本字符锚点都能恢复，并在字体加载后重试一次。[Workspace.tsx](../../../frontend/src/Workspace.tsx#L382)
- 断点变化时先保存最后锚点，等新布局完成后再恢复。[Workspace.tsx](../../../frontend/src/Workspace.tsx#L977)
- 真实浏览器证据见 [browser-check.json](browser-check.json)：390×844 归零后滚动到 4000px，保存 `segment_index=61`，刷新后该段可见（top 405 / 844）；纯文本保存 `text_offset=816`，刷新后同一行可见；桌面锚点 40 切到手机后仍可见。

### R07 · 恢复预算耗尽后没有暂停，内存继续积累

- `PendingResultStore.put` 返回 `over_budget`；磁盘预算或内存预算任一超限都会让 worker 暂停队列，保留当前结果并停止启动后续任务。[pending_results.py](../../../backend/pending_results.py#L128)、[queue_api.py](../../../backend/queue_api.py#L738)
- 替换/移除时准确扣减内存字节统计；从磁盘恢复的 payload 也计入。[pending_results.py](../../../backend/pending_results.py#L103)
- 回归：`test_pending_budget_exhaustion_pauses_following_work`、`test_pending_store_accounts_bytes_on_replace_and_remove` 通过。

### R08 · 版本预留失败仍继续识别

- `HistoryStore.reserve_version` 不再吞掉写入异常：没有可靠预留就抛错。[history.py](../../../backend/history.py#L188)
- worker 在预留失败时标记 `version_reservation_failed`、暂停队列并在调用 ASR 前返回。[queue_api.py](../../../backend/queue_api.py#L668)
- 回归：`test_failed_version_reservation_does_not_start_recognition`、`test_reserve_version_reports_a_write_failure` 通过。

### R09 · 纯文本查找提示矛盾，段落命中无导航

- 命中计数按当前呈现方式计算：字幕行按 segment、段落视图按段落、纯文本按实际文本行；纯文本 80 行命中显示 `1 / 80 处`，不再同时提示“未找到”。[Workspace.tsx](../../../frontend/src/Workspace.tsx#L1040)
- 新增“上一处/下一处”导航，命中项滚动进入视口；段落和纯文本行都写入稳定 `data-*` 锚点。[Workspace.tsx](../../../frontend/src/Workspace.tsx#L1088)、[Workspace.tsx](../../../frontend/src/Workspace.tsx#L1215)
- 真实浏览器证据：纯文本 80 处高亮且无错误提示；段落查询 `1 / 1 处`，两个导航按钮可用，高亮在视口内（top 663 / 820）。

### R10 · 跨筛选选择、批量重试、桌面关闭确认

- 导出/重试范围改为完整选中集合（`activeBatch.items`），不再从筛选后的 `taskRows` 计算；隐藏选中的数量会明确显示。[Workspace.tsx](../../../frontend/src/Workspace.tsx#L698)、[Workspace.tsx](../../../frontend/src/Workspace.tsx#L1174)
- 新增真实的顺序批量重试动作，逐项反馈失败，不承诺并发。[Workspace.tsx](../../../frontend/src/Workspace.tsx#L777)
- 桌面外壳绑定 `closing` 可取消事件：只在队列仍有等待/处理中任务时显示原生确认；拒绝关闭返回 False，不触发 `closed`/shutdown；对话框自身失败时允许关闭。[shell_main.py](../../../shell/shell_main.py#L120)、[shell_main.py](../../../shell/shell_main.py#L148)、[shell_main.py](../../../shell/shell_main.py#L275)
- 真实浏览器证据：选中 2 项后用搜索隐藏 2 项，仍显示“可导出 1 项 · 可重试 1 项”，批量重试按钮可用，并标注“其中 2 项在当前筛选外”。
- 边界：原生 WebView2 对话框没有在本轮进行人工点击验收；`make_close_handler` 的接受/拒绝/空闲/异常路径已用假窗口单测。

## 2. 回归与验证

### 2.1 后端

在 `build/repair-20261031-final/runtime` 隔离副本中，使用项目解释器 Python 3.12.13：

```text
python -m pytest backend checks_backend.py checks_independent.py -q
166 passed in 7.36s
```

- 基线 143 项全部通过。
- 新增 16 项永久回归：6 项 `backend/test_repair.py`、3 项 `backend/test_shell.py`、7 项 `backend/test_repair_review.py`（复查的 7 项独立检查已固化）。
- 复查目录的 7 项检查单独运行：`7 passed in 0.69s`，见 [independent-tests.xml](independent-tests.xml)。
- 完整结果见 [backend-tests.xml](backend-tests.xml)。
- `backend.main` 在运行副本中实际 import 成功。

### 2.2 前端

```text
npm run typecheck       通过
npm run test:export-plan 通过（3000ms 看门狗，无超时）
npm run build          通过
```

生产资源为 `index-CFMVZlAm.js`、`index-Wo95jN-5.css`。记录见 [frontend-checks.txt](frontend-checks.txt)。

CI 已加入 `npm run test:export-plan`，后端步骤扩展到 `test_main + test_repair + test_repair_review`；本地等价命令 101 passed。[ci.yml](../../../.github/workflows/ci.yml)

### 2.3 真实浏览器

Chrome 156.0.8078.79，headless + CDP，1280×820 与 390×844；使用隔离 FastAPI（`SKIP_LLAMA=1`）和虚构记录/队列，不接触真实用户 data。7 项检查全部通过且无未处理异常，见 [browser-check.json](browser-check.json)：

| 检查 | 结果 |
| --- | --- |
| 纯文本查找计数与提示一致 | 80 处高亮，`1 / 80 处`，无“未找到” |
| 段落查找可导航并定位命中 | `1 / 1 处`，上一处/下一处可用，高亮在视口内 |
| 隐藏选中项保留导出/重试范围 | 已选 2、隐藏 2，仍可导出 1、可重试 1，批量按钮可用 |
| 手机新位置保存并在刷新后恢复 | 归零后新保存段 61；刷新后仍可见 |
| 纯文本字符锚点保存并恢复 | `text_offset=816`；刷新后同一行可见 |
| 桌面锚点跨断点保留 | 段 40 在 390×844 下仍可见 |
| 无未处理浏览器异常 | `[]` |

截图：[纯文本查找](01-plain-text-search.png)、[段落命中导航](02-paragraph-hit-navigation.png)、[隐藏选择范围](03-hidden-selection.png)、[手机刷新恢复](04-mobile-restored.png)、[纯文本手机恢复](05-plain-mobile-restored.png)、[跨断点恢复](06-mobile-after-breakpoint.png)。

### 2.4 代码一致性

核心后端、外壳和生产构建与最终运行副本逐字节一致，fixture 运行副本也一致，见 [code-snapshot.json](code-snapshot.json)。仓库 `frontend/dist` 与浏览器实际加载资源 hash 相同。

## 3. A01–A11 状态边界

| 项目 | 本轮后判断 | 说明 |
| --- | --- | --- |
| A01 导入身份 | 未改动，维持复查“主要问题已修复” | 本轮未重测。 |
| A02 搜索/查找 | 复查 R09 已关闭 | 纯文本计数、段落导航通过浏览器；标题/来源/批次入口沿用前轮证据。 |
| A03 返回分页 | 未改动，未重测 | 复查的深分页竞态矩阵仍不在本轮证据内。 |
| A04 保存失败恢复 | R01–R04 已关闭 | 故障注入、并发、重启、取消来源均有回归。 |
| A05 结果版本 | R08 已关闭 | 原有版本用例通过；真实 GPU 对齐未重测。 |
| A06 后台刷新 | 未改动，未重测 | 沿用前轮实现与证据。 |
| A07 手机入口/位置 | R06 已关闭 | 390×844 保存、刷新、跨断点、纯文本锚点通过真实浏览器。 |
| A08 导出计划 | R05 已关闭 | 路径分配与终止性由看门狗测试覆盖；本轮未实际下载并解包 ZIP。 |
| A09 字号/布局 | 未改动 | 200% 缩放、对比度、辅助技术仍未验证。 |
| A10 交互补齐 | 选择范围/批量重试/关闭确认已补齐 | 选择与重试通过浏览器；原生关闭对话框仅处理器单测，未做 WebView2 点击验收。 |
| A11 时间能力 | 未改动，未重测 | 真实 GPU 对齐和未知时长导出组合仍保留为未验证。 |

## 4. 未覆盖与保留项

- 真实 Qwen3-ASR/对齐器推理。本轮浏览器验证使用 `SKIP_LLAMA=1` 的受控后端；ASR 流水线没有改动，但不能把本轮结果当作真实 GPU 通过。
- WebView2 原生关闭对话框的真实点击。处理器语义已按 pywebview 6.2.1 的 `closing` 契约单测：返回 False 取消关闭；对话框异常不困住用户。
- 浏览器实际下载 ZIP、CRC 与 manifest 解包。分配器用实际 TypeScript 模块的看门狗测试覆盖；ZIP 编码器未改。
- A03 深分页并发矩阵、A06 完整刷新竞态、A09 的可访问性/200% 缩放、A11 的真实对齐与未知时长导出组合。
- 本轮没有提交 git commit；改动保留在工作区。

## 5. 复现命令

后端（隔离副本）：

```powershell
$repo = 'E:\Code_Projects\Python_Dev\Qwen3-ASR1.7B-TsGUI'
$rt = Join-Path $repo 'build\repair-20261031-final\runtime'
Copy-Item (Join-Path $repo 'backend') $rt -Recurse
Copy-Item (Join-Path $repo 'run.py') $rt
Copy-Item (Join-Path $repo 'shell') $rt -Recurse
New-Item -ItemType Directory (Join-Path $rt 'frontend') -Force | Out-Null
Copy-Item (Join-Path $repo 'frontend\dist') (Join-Path $rt 'frontend') -Recurse
Copy-Item (Join-Path $repo 'docs\acceptance\gizmodo-v1-20261030\checks_backend.py') $rt
Copy-Item (Join-Path $repo 'docs\acceptance\gizmodo-v1-repair-review-20261031\checks_independent.py') $rt
Push-Location $rt
try {
    & 'E:\anaconda3\envs\qwen3asr\python.exe' -m pytest backend checks_backend.py checks_independent.py -q --junitxml=../backend-tests.xml
} finally { Pop-Location }
```

前端：

```powershell
Push-Location (Join-Path $repo 'frontend')
try {
    npm run typecheck
    npm run test:export-plan
    npm run build
} finally { Pop-Location }
```

浏览器证据：把 [fixture_server.py](fixture_server.py) 放在包含 `runtime/` 的目录中（脚本写入 `runtime/data` 并输出 `runtime/seeds.json`），启动后以 `--remote-debugging-port=9223` 启动 Chrome，再在包含 `runtime/` 和 `browser-evidence/` 的目录运行 [browser_check.mjs](browser_check.mjs)。脚本不会访问真实 `data/`。

## 6. 证据索引

| 证据 | 内容 |
| --- | --- |
| [backend-tests.xml](backend-tests.xml) | 166 项完整后端测试，0 失败。 |
| [independent-tests.xml](independent-tests.xml) | 复查 7 项独立检查，0 失败。 |
| [frontend-checks.txt](frontend-checks.txt) | typecheck、export-plan 看门狗测试、生产构建。 |
| [browser-check.json](browser-check.json) | 真实 Chrome 7 项界面检查及数值。 |
| [code-snapshot.json](code-snapshot.json) | 源码/构建 hash、Python/Chrome 版本、运行副本一致性。 |
| [browser_check.mjs](browser_check.mjs)、[fixture_server.py](fixture_server.py) | 浏览器复现脚本与隔离后端脚本。 |
| 截图 01–06 | 查找、选择范围、手机刷新/纯文本/跨断点恢复。 |
