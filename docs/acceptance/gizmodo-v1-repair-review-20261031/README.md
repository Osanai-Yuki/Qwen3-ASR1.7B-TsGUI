# Gizmodo v1 修复独立复查

日期：2026-10-31。分支：`feat/gizmodo-workspace`。

**结论：修复有明显进展，但仍未达到整体验收通过条件。** 现有 143 项测试、前端类型检查和生产构建均通过；独立补充的 7 项后端边界检查全部复现了缺口，导出和实际手机页面也有未关闭问题。不能将原修复报告中的“常规用例通过”视为 A01–A11 已全部闭合。

依据：[修复交接方案](../../plans/gizmodo-v1-repair-handoff.md)、[原验收](../gizmodo-v1-20261030/README.md)、[修复方验收](../gizmodo-v1-repair-20261030/README.md)。

## 1. 本轮范围

- 核对当前工作区实际代码，重新执行 typecheck/build，构建输出仍为 `index-Byki_hca.js` 和 `index-B7udBvg5.css`。
- 使用项目解释器 Python 3.12.13，在新的 `build/repair-review-20261031/runtime` 副本中运行后端测试。原有测试、修复测试和原 9 项验收合计 **143 passed in 5.31s**，见 [baseline-tests.xml](baseline-tests.xml)。
- 新增的 7 个边界用例针对交接要求中的恢复、并发、崩溃窗口和资源上限，不修改业务实现。分别执行 6 项及 1 项重启用例，见 [测试源码](checks_independent.py)、[六项结果](independent-tests.xml)、[重启结果](restart-edge-tests.xml)。
- 用实际 FastAPI、真实 Qwen3-ASR 模型和 Chrome 检查界面。使用独立虚构数据，未向用户真实 data 执行导入、删除或取消。
- 核心后端源码和生产构建与运行副本逐字节一致，见 [code-snapshot.json](code-snapshot.json)。
- 本轮真实短音频任务成功保存，具有 task/attempt/version 身份；读 A 时 B 完成，reader/audio 实例和 2.5 秒位置保持不变。见 [真实结果](real-asr-result.json)、[浏览器记录](browser-review.json)。
- 只新增复查资料和隔离测试；业务代码未修正。本次创建的后端、模型及浏览器进程已关闭。

## 2. 对 A01–A11 的判断

| 项目 | 复查判断 | 证据和剩余范围 |
| --- | --- | --- |
| A01 导入身份 | 主要问题已修复 | 实际界面同名同大小、不同内容的 File 保留 2 项；ID 回执相关现有用例通过。 |
| A02 搜索/查找 | 部分修复 | 文件名搜索打开 6 段全文通过；纯文本匹配计数和段落命中导航仍有问题，见 R09。 |
| A03 返回分页 | 主要复现已修复 | 72 条结果连续打开返回三次，仍为 72 条；请求乱序与所有深分页插入组合未全量重测。 |
| A04 保存失败恢复 | **未关闭** | 普通单请求恢复已有实现；新故障、并发及中断重启边界失败，见 R01–R04、R07。 |
| A05 结果版本 | 常规选择已修复，失败预留未闭合 | 同秒/mtime 原用例通过；版本预留失败仍执行识别，见 R08。 |
| A06 后台刷新 | 已有对应实现和常规证据 | 本轮真实后台完成不扰动阅读通过；列表刷新可在修复方记录中看到，完整竞态矩阵未重复执行。 |
| A07 手机入口/位置 | **未关闭** | 导航附近搜索入口已出现；新手机位置未及时保存、刷新丢位、跨断点丢锚点，见 R06。 |
| A08 导出计划 | **未关闭** | 常见两份冲突能产生一致路径；有效 ID 的特定三重冲突会无限循环，见 R05。 |
| A09 字号/布局 | 有明显修复，仍保留验收边界 | 实际页面字体/触控区较原版改进；完整对比度、辅助技术和真实 200% 缩放本轮未覆盖。 |
| A10 交互补齐 | **部分完成** | 列表/段落偏好和任务详情已有；跨筛选选择范围、批量重试、桌面原生退出确认未完成，见 R10。 |
| A11 时间能力 | 主要表达问题已修复 | 真实未对齐结果明确为 none；本轮未重测真实 GPU 对齐全过程和全部未知时长导出组合。 |

这里的“主要问题已修复”限定于明确列出的用例，不代表额外未测场景也已通过。

## 3. 需要优先修复的问题

### R01 · P1：恢复存储不可用时，文字没有保留，却仍宣称可读

**关联：A04；测试：`test_pending_directory_failure_keeps_readable_session_copy`、`test_cancelled_queue_write_failure_keeps_partial_payload`。**

两条独立失败路径：

1. 模拟 pending 目录无法创建。worker 吞掉异常后仍设置 `temporary_result_available=true` / `can_retry_save=true`，日志还写着 temporary result retained；之后实际 `GET /result` 返回 **409**，正文不存在。
2. 取消任务收到部分文字后，模拟 queue 写入失败。取消分支先 `set_status(cancelled)` 再保存 payload，前一步异常直接跳过保留文字。恢复 queue 写入后读取结果，仍返回 **409**。

位置：[PendingResultStore 初始化](../../../backend/pending_results.py#L57)、[取消分支](../../../backend/queue_api.py#L614)、[_retain_pending](../../../backend/queue_api.py#L687)。

修复要点：会话内结果副本的存在不能依赖恢复目录成功创建；先确保 payload 有独立可读副本，再尝试磁盘/队列写入。能力字段必须反映实际可读性。回归断言应读取 `GET /result` 并比较正文，不能只断言 worker 没抛错或 error 字段存在。现有 `test_save_failure_survives_recovery_bookkeeping_errors` 正是只检查了后两者，因此没有发现文字已经丢失。

### R02 · P1：重新保存与重新处理并发，会让旧保存覆盖新操作

**关联：A04；测试：`test_save_retry_blocks_reprocessing_until_claim_is_resolved`。**

让 `POST /save` 在实际历史写入前等待，然后同时调用 `POST /retry`。当前两个请求均返回 **200**。后到的旧保存将队列重新标成 done，并删除原文件，覆盖了刚接受的“重新处理”操作。

`try_claim_save` 只避免两个保存请求同时写，并未阻止 retry 清空 attempt；保存结束时也没有再次检查当前 attempt/任务状态。

位置：[queue_retry](../../../backend/queue_api.py#L335)、[保存提交后覆盖状态](../../../backend/queue_api.py#L499)、[try_claim_save](../../../backend/queue_store.py#L295)。

修复要点：保存、重新执行、删除等会改变同一 attempt 所有权的操作要统一互斥/比较交换；提交回写和清理源文件前重新核对 attempt。冲突应被拒绝或显式处理，不能两边都报成功。

### R03 · P1：保存中断后重启，持久化的 saving 标记会永久阻塞保存

**关联：A04；测试：`test_interrupted_save_can_be_retried_after_restart`。**

保存请求获得 claim，写入 `persistence_state=saving` 后进程中断，历史尚未提交。重新创建 QueueStore 并执行 reconcile，后续每次保存都返回：

> a save for this attempt is already in progress; retry once it finishes

状态没有活着的进程拥有，却一直被视为正在保存。重启恢复只处理 running 或已经有历史的 attempt，没有释放未提交的 saving claim。

位置：[QueueStore._load](../../../backend/queue_store.py#L63)、[reconcile_attempts](../../../backend/queue_api.py#L105)、[claim 判定](../../../backend/queue_store.py#L308)。

修复要点：启动时核对 attempt 的实际历史和 pending payload。已提交则补关联，未提交且恢复 payload 可用则退回可重试状态，不能沿用上一进程的互斥占用。

### R04 · P1：取消结果重新保存成功后，原文件被删，无法再重新处理

**关联：A04 及必须保留的取消重试行为；测试：`test_cancelled_result_still_reprocessable_after_save_retry`。**

取消时保存失败的部分结果保留了来源；点击“重新保存”返回 200 后，source_exists 变为 **false**，随后 `POST /retry` 返回 **410 gone**。任务仍为 cancelled，页面仍提供“重新处理”，但来源已经被重新保存接口无条件删除。

位置：[重新保存清理](../../../backend/queue_api.py#L517)。启动 reconcile 的清理路径也应按同一来源策略复查，不能只修正常完成分支。

修复要点：保存文字与删除取消任务来源是两件事。保留取消项原有的从头重跑能力；只有符合实际保留策略且关联持久化成功的源文件才能清理。

### R05 · P1：特定重名导出会在主线程无限循环

**关联：A08；证据：[front-edge.json](front-edge.json)。**

对同一路径导出三份结果，使用以下三个合法 12 位 ID：

```text
12345678aaaa
12345678bbbb
12345678cccc
```

三者前 8 位相同。第二份分配 `same__12345678.txt` 后，第三份进入冲突循环。循环追加 n，但 `suffixId` 始终只取前 8 位，所以每次候选名都完全一样，永远无法退出。

本轮将实际 TypeScript 工具函数转译后放进独立 Worker：普通冲突立即成功；上述案例 **1500ms 内不返回**，随后由测试终止 Worker。实际应用在浏览器主线程同步执行该函数，会卡住页面。触发条件是特定身份碰撞，不是任意两份同名结果。

位置：[suffixId/withSuffix](../../../frontend/src/utils/exportPlan.ts#L86)、[冲突循环](../../../frontend/src/utils/exportPlan.ts#L119)、[第二处相同循环](../../../frontend/src/utils/exportPlan.ts#L149)。

修复要点：冲突计数或完整身份必须进入最终文件名，不能在追加后又被截断。同时验证三重冲突、相同前缀、manifest 保留名及选择顺序变化。

### R06 · P1：手机新阅读位置没有保存，刷新及断点切换会丢位

**关联：A07；证据：[mobile-review.json](mobile-review.json)、[browser-review.json](browser-review.json)。**

为排除旧锚点污染，本轮先把记录明确重置为 `segment_index=0`：

1. 390×844 打开全文，滚到 `scrollY=4000`，第一可见段的 **索引为 24（第 25 段）**。
2. 等待 2.2 秒后，API 中保存的仍为 **segment_index=0 / scroll_ratio=0**。
3. 直接刷新，目标段落位于屏幕外约 **3467px**，没有恢复新位置。

另在桌面滚动保存索引为 15 的段落后直接切到 390px，原段落移到屏幕外约 2807px，阅读锚点没有跨断点保留。

根因：虽然新增了 `scrollAdapter`，实际事件仍只有 main 上的 onScroll，未订阅 window scroll，也没有断点改变时的保存/恢复。离开 React reader 时的 cleanup 有时能补存位置，但不能代表页面关闭/刷新和滚动中保存已正确实现。

位置：[滚动捕获](../../../frontend/src/Workspace.tsx#L807)、[main.onScroll](../../../frontend/src/Workspace.tsx#L919)、[恢复 effect](../../../frontend/src/Workspace.tsx#L374)。

特别注意：只检查“存在非零 reader_position”或“能恢复旧位置”会误判。本轮第一次宽松检查就观察到了先前桌面保存的旧值；严格归零并比较新位置后才确认未修好。

## 4. 其他未闭合项

### R07 · P2：恢复预算耗尽后没有暂停，内存可继续积累

将 pending 预算设为 1 字节，产生约 491 字节失败结果；结果仅保留内存，队列 `paused=false`，仍会继续启动后续任务。

证据：`test_pending_budget_exhaustion_pauses_following_work`。位置：[PendingResultStore.put](../../../backend/pending_results.py#L106)、[_retain_pending](../../../backend/queue_api.py#L717)、[worker_loop](../../../backend/queue_api.py#L729)。

交接方案要求预算耗尽时保留当前结果并停止启动新任务。当前只限制磁盘写入，没有实现背压；磁盘故障持续时会继续扩大内存占用。内存字节统计的替换/移除扣减也需要一起校正。

### R08 · P2：版本预留失败仍继续识别

让 reserve_version collaborator 抛出 OSError，worker 记录日志后仍调用 ASR，结果版本为空。真实 HistoryStore 的预留写入异常也被吞掉并返回未落盘的编号。

证据：`test_failed_version_reservation_does_not_start_recognition`。位置：[worker 预留异常](../../../backend/queue_api.py#L558)、[reserve_version 异常处理](../../../backend/history.py#L216)。

普通版本比较已经修好；这里缺的是失败条件下的执行准入。没有可靠预留时应暂停/明确失败，避免同一任务多个失败执行取得不可靠或重复版本。

### R09 · P2：纯文本查找产生矛盾提示，段落命中缺少导航

- 纯文本记录查找“独立复验”，页面实际产生 **80 处高亮**，同时却提示“本份结果未找到匹配内容”。计数只遍历 segments，没有计算 text-only 结果。
- 段落视图查找最后一段文字，命中高亮位于视口外约 3356px；没有上一处/下一处导航。文件名入口的空正文已修复，但完整文内查找要求尚未闭合。

位置：[matchIndexes](../../../frontend/src/Workspace.tsx#L866)、[零命中提示](../../../frontend/src/Workspace.tsx#L1002)。见 [纯文本截图](07-plain-text-search-contradiction.png)、[段落记录](browser-review.json)。

### R10 · P2：A10 仍有功能未实现，不能整体标为通过

1. 实际选中一项可导出结果后，用任务搜索将其隐藏：多选栏仍显示“已选 1 项”，可导出却从 **1 变为 0**。与“可跨筛选保留”的文字和交接定义不一致。
2. taskExports 仍从筛选后的 taskRows 计算；“可重试 N 项”仍只给出文字，没有真正的批量重试按钮。
3. 桌面原生关闭确认仍未实现；`shell_main.py` 仍只绑定 closed→shutdown。这属于缺少实现，不能仅归类成环境原因下的“未验证”。修复方报告的正文已承认这一点。

位置：[taskExports](../../../frontend/src/Workspace.tsx#L666)、[多选栏](../../../frontend/src/Workspace.tsx#L960)、[shell 关闭绑定](../../../shell/shell_main.py#L211)。见 [范围截图](11-hidden-selection-export-shrinks.png)、[严格界面记录](mobile-review.json)。

## 5. 如何继续修复

1. 先统一同一任务的保存/重跑/删除所有权和启动恢复；会话内 payload 的保留要独立于磁盘/队列成功写入，不能只修状态文字。
2. 修正保存成功后的源文件清理策略，补齐取消任务重新保存之后的重跑用例。
3. 修导出冲突循环，并使用可终止的测试保护 CI/浏览器免受死循环影响。
4. 为 window/容器滚动真正绑定事件，在断点变化时保存并恢复锚点；验证应归零后读取新值，并包含直接刷新。
5. 补齐纯文本匹配、隐藏选中项范围、批量重试和桌面关闭确认，再更新逐项结论。

后续至少让本目录的 7 个补充后端用例通过，修复前端 Worker 超时案例，并重新跑严格手机位置与跨筛选检查；同时保持 143 项原有用例和真实阅读隔离通过。

## 6. 证据与覆盖边界

| 证据 | 内容 |
| --- | --- |
| [baseline-tests.xml](baseline-tests.xml) | 原 143 项回归全部通过。 |
| [checks_independent.py](checks_independent.py) | 本轮 7 项补充后端边界用例，仅应在隔离 runtime 中执行。 |
| [independent-tests.xml](independent-tests.xml)、[restart-edge-tests.xml](restart-edge-tests.xml) | 6 + 1 项失败的断言和栈。 |
| [front-edge.json](front-edge.json) | 通常重名分配通过，特定三重冲突超时。 |
| [browser-review.json](browser-review.json) | 普通交互、真实 ASR、阅读隔离、跨断点和查找。 |
| [mobile-review.json](mobile-review.json) | 归零后的新位置保存、刷新恢复和隐藏选择范围。 |
| [real-asr-result.json](real-asr-result.json) | 本轮真实音频的持久结果和 timing。 |
| [code-snapshot.json](code-snapshot.json) | 核心源码、生产资源 hash 和副本一致性。 |

本轮没有重做真实 WebView2 关闭、真实浏览器 200% 缩放、全量辅助技术/对比度、长视频或 GPU 强制对齐质量验证。对应项目仍保留为未验证；不能由现有测试全绿推断通过。

部分失败通过受控故障/并发注入复现，不是对用户真实文件实施故障。罕见导出碰撞由合法的确定 ID 构造，代表边界缺陷，不代表用户当前记录一定已经碰撞。
