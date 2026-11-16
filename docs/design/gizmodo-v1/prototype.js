/* Standalone UX demonstration. No API calls and no uploaded file contents. */
"use strict";
const $ = (selector) => document.querySelector(selector);
const searchIcon = '<svg class="search-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><circle cx="10.6" cy="10.6" r="5.9"></circle><path d="M15.1 15.1 19.4 19.4"></path></svg>';
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[char]));
const collator = new Intl.Collator("zh-CN", { numeric: true });
const supported = /\.(mp4|mkv|mov|avi|webm|flv|m4v|wmv|mpg|mpeg|ts|3gp|vob|ogv|wav|mp3|flac|ogg|m4a)$/i;
const baseRows = [
  ["产品周会_10月22日.mp4", "done", "会议/产品", 38, false],
  ["用户访谈_陈女士.mp4", "done", "访谈/新用户", 46, true],
  ["用户访谈_林先生.mp4", "done", "访谈/新用户", 29, false],
  ["需求评审_订单流程.mp4", "done", "会议/需求", 57, false],
  ["项目复盘_移动端.mp4", "done", "会议/复盘", 42, true],
  ["用户访谈_周女士.mp4", "warning", "访谈/老用户", 34, false],
  ["产品周会_10月28日.mp4", "error", "会议/产品", 51, false],
  ["用户访谈_何先生.mp4", "running", "访谈/老用户", 41, false],
  ["需求访谈_设计团队.mp4", "queued", "访谈/内部", 25, false],
  ["渠道访谈_广州.mp4", "queued", "访谈/渠道", 35, false],
  ["技术评审_搜索.mp4", "queued", "会议/技术", 49, false],
  ["客户回访_赵先生.mp4", "queued", "访谈/回访", 28, false],
];
const initialBatches = () => [
  { id: "b1", name: "10月会议与访谈", date: "2026-10-30 09:30", items: baseRows.map((row, i) => ({ id: "t" + (i + 1), filename: row[0], status: row[1], path: row[2], duration: row[3], seen: row[4], progress: row[1] === "running" ? 63 : 0 })) },
  { id: "b2", name: "上周会议记录", date: "2026-10-22 15:10", items: [
    { id: "t13", filename: "增长例会_10月15日.mp4", status: "done", path: "例会", duration: 36, seen: true },
    { id: "t14", filename: "渠道访谈_上海.mp4", status: "done", path: "渠道", duration: 44, seen: false },
  ] },
];
const state = { batches: initialBatches(), view: "library", batchId: "b1", query: "", filter: "all", sort: "import", selected: new Set(), paused: false, resultId: null, readerQuery: "", origin: null, originIds: [], positions: {}, importFiles: [], fileCounter: 0, batchCounter: 2, taskCounter: 14, exportIds: [] };
const all = () => state.batches.flatMap((batch) => batch.items);
const batch = () => state.batches.find((entry) => entry.id === state.batchId);
const task = (id) => all().find((entry) => entry.id === id);
const parent = (id) => state.batches.find((entry) => entry.items.some((item) => item.id === id));
const readable = (entry) => entry.status === "done" || entry.status === "warning";
const running = () => all().find((entry) => entry.status === "running");
const attention = (entry) => entry.status === "warning" || entry.status === "error";
const plainName = (entry) => entry.filename.replace(/\.[^.]+$/, "");
const duration = (entry) => entry.duration ? `${entry.duration}:00` : "待检测";
const processed = (entry) => { const seconds = Math.round(entry.duration * 60 * (entry.progress || 0) / 100); return Math.floor(seconds / 60) + ":" + String(seconds % 60).padStart(2, "0"); };
const texts = [
  "我们先从最近一次使用经历谈起。你当时想完成什么，最后是怎么做的？",
  "最花时间的是整理会议和访谈的内容。文件放在几个目录里，过几天回来，经常要重新找一遍。",
  "如果同一批素材里有一份没有处理好，我希望能直接看到是哪一份、哪里出了问题。其他已经完成的内容可以先用。",
  "字幕生成以后，我通常先搜几个关键词，再回听对应的那一小段，确认原话和上下文。",
  "不一定按照文件顺序看。有时候是找某位受访者的回答，有时候是回看上周会议里讨论过的决定。",
  "导出时，文件名最好能和原视频对应上，这样发给同事后，他们也知道每份文字来自哪里。",
  "下次回来，我希望还在上次看到的位置，能继续读，也能很快切到下一份还没有查看的结果。",
];
const transcriptText = (entry) => transcriptFor(entry).join(" ");
const key = () => state.view + ":" + state.batchId;
function savePosition() { state.positions[key()] = { main: $("#main").scrollTop, table: $(".table-scroll")?.scrollTop || 0, windowY: window.scrollY }; }
function toast(message) { const node = $("#toast"); node.textContent = message; node.classList.add("show"); clearTimeout(toast.timer); toast.timer = setTimeout(() => node.classList.remove("show"), 6500); }
function highlight(text, query) { if (!query.trim()) return esc(text); const index = text.toLowerCase().indexOf(query.trim().toLowerCase()); return index < 0 ? esc(text) : esc(text.slice(0, index)) + "<mark>" + esc(text.slice(index, index + query.trim().length)) + "</mark>" + esc(text.slice(index + query.trim().length)); }
function filteredRows() {
  let rows = state.view === "library" ? all().filter(readable) : [...batch().items];
  if (state.view === "library" && state.kind !== "all") rows = rows.filter(entry => kindOf(entry) === state.kind);
  const query = state.query.trim().toLowerCase();
  if (query) rows = rows.filter((entry) => (entry.filename + " " + displayTitle(entry) + " " + entry.path + " " + parent(entry.id).name + (state.view === "library" ? " " + transcriptText(entry) : "")).toLowerCase().includes(query));
  if (state.filter === "attention") rows = rows.filter(attention);
  if (state.filter === "ready") rows = rows.filter(readable);
  if (state.filter === "queued") rows = rows.filter((entry) => entry.status === "queued");
  if (state.filter === "unseen") rows = rows.filter((entry) => readable(entry) && !entry.seen);
  if (state.sort !== "import") rows.sort((a, b) => collator.compare(a.filename, b.filename) * (state.sort === "asc" ? 1 : -1));
  return rows;
}
function statusHTML(entry) {
  const labels = { done: ["", "已完成", "完整结果已保存"], warning: ["warning", "已完成，有缺失", "02:14–02:39 未识别"], error: ["error", "处理失败", "转写请求超时"], running: ["running", entry.duration ? `正在转写 ${entry.progress || 0}%` : "正在准备音轨", entry.duration ? `已处理 ${processed(entry)} / ${duration(entry)}` : "正在检测媒体时长"], queued: ["", "等待处理", "已保存到本机"] };
  const [style, label, note] = labels[entry.status];
  return `<span class="status-label ${style}"><span class="status-dot ${style}" aria-hidden="true"></span>${label}</span><span class="status-note">${note}</span>`;
}
function renderBaseSidebar() {
  $("#nav-tasks").classList.toggle("selected", state.view === "tasks" || (state.view === "reader" && state.origin?.view === "tasks"));
  $("#nav-library").classList.toggle("selected", state.view === "library" || (state.view === "reader" && state.origin?.view === "library"));
  const pending = all().filter((entry) => entry.status === "queued" || entry.status === "running").length;
  $("#pending-count").textContent = String(pending);
  $("#batch-nav").innerHTML = state.batches.map((entry) => {
    const ready = entry.items.filter(readable).length;
    return `<button class="batch-button ${state.view !== "library" && state.batchId === entry.id ? "selected" : ""}" data-action="batch" data-id="${entry.id}" title="${esc(entry.name)}"><strong>${esc(entry.name)}</strong><small>${ready} 份可查看 / ${entry.items.length} 个文件</small></button>`;
  }).join("") + `<button class="storage-link" data-action="storage">存储与退出说明</button>`;
  const current = running();
  $("#runtime-label").textContent = current ? `正在转写 ${current.filename}` : state.paused ? "处理已暂停" : all().some((entry) => entry.status === "queued") ? "等待处理" : "所有任务已结束";
  $(".runtime .status-dot").classList.toggle("running", Boolean(current));
}
function rowHTML(entry) {
  const isReady = readable(entry);
  const selected = state.selected.has(entry.id);
  const meta = state.view === "library" ? parent(entry.id).name + " / " + entry.path : entry.path;
  return `<tr class="${entry.status === "running" ? "current" : ""} ${selected ? "selected" : ""}" data-task="${entry.id}">
    <td><input type="checkbox" data-select="${entry.id}" aria-label="选择 ${esc(entry.filename)}" ${selected ? "checked" : ""}></td>
    <td><button class="file-name ${isReady ? "readable" : ""}" data-action="${isReady ? "open" : "explain"}" data-id="${entry.id}" title="${esc(entry.filename)}">${esc(entry.filename)}</button><div class="file-meta" title="${esc(meta)}">${esc(meta)}${entry.duration ? " · " + duration(entry) : ""}</div></td>
    <td>${statusHTML(entry)}</td><td><span class="seen-label ${!entry.seen && isReady ? "unseen" : ""}">${isReady ? entry.seen ? "已查看" : "未查看" : "—"}</span></td>
    <td>${entry.status === "error" ? `<button class="text-button" data-action="retry" data-id="${entry.id}">重新加入处理</button>` : isReady ? `<button class="text-button" data-action="open" data-id="${entry.id}">${entry.status === "warning" ? "查看缺失" : "查看结果"}</button>` : `<span class="subtle">${entry.status === "running" ? "处理中" : "等待中"}</span>`}</td></tr>`;
}
function renderList() {
  const isLibrary = state.view === "library";
  const scope = isLibrary ? all().filter(readable) : batch().items;
  const count = (status) => scope.filter((entry) => entry.status === status).length;
  const rows = filteredRows();
  const readyCount = (isLibrary ? rows : scope).filter((entry) => entry.status === "done").length;
  if ($("#breadcrumb")) $("#breadcrumb").textContent = isLibrary ? "结果库 / 全部批次" : "任务中心 / " + batch().name;
  const current = batch().items.find((entry) => entry.status === "running");
  const pauseLabel = state.paused ? running() ? "取消暂停" : "继续处理" : "暂停全部";
  const progressNote = current?.duration ? `已处理 ${processed(current)} / ${duration(current)}；预计剩余时间正在估算。` : "正在检测媒体时长，完成后开始转写。";
  let active = "";
  if (!isLibrary && (current || state.paused || count("queued"))) {
    active = current ? `<div class="active-strip ${state.paused ? "paused" : ""}"><div class="active-copy"><strong>${esc(current.filename)} · ${state.paused ? "完成当前文件后暂停全部任务" : current.duration ? "正在转写" : "正在准备音轨"}</strong><span>${state.paused ? "当前文件会继续完成，之后不再启动新文件。" : progressNote}</span>${current.duration ? `<div class="mini-progress"><span style="width:${current.progress}%"></span></div>` : ""}</div>${current.duration ? `<span class="percent">${current.progress}%</span>` : ""}</div>` : `<div class="active-strip ${state.paused ? "paused" : ""}"><div class="active-copy"><strong>${state.paused ? "处理已暂停" : "本批次正在等待"}</strong><span>${state.paused ? "已保存的文件和结果会保留，点击继续处理恢复队列。" : "将在前面的批次处理完成后开始，当前可查看已有结果。"}</span></div></div>`;
  }
  const filters = isLibrary ? [["all", "全部结果"], ["unseen", "未查看"], ["attention", "有缺失"]] : [["all", "全部"], ["attention", `需处理 ${scope.filter(attention).length}`], ["ready", "可查看"], ["queued", "等待"]];
  const selected = [...state.selected].map(task).filter(Boolean);
  const selectedReady = selected.filter(readable).length;
  const batchBar = selected.length ? `<div class="batch-bar"><span>已选 ${selected.length} 项</span><span class="subtle">可导出 ${selectedReady} 项</span><span class="spacer"></span><button class="button small" data-action="export-selected" ${selectedReady ? "" : "disabled"}>导出所选</button><button class="button small" data-action="retry-selected" ${selected.some((entry) => entry.status === "error") ? "" : "disabled"}>重试所选失败项</button><button class="text-button" data-action="clear-selection">取消选择</button></div>` : "";
  $("#main").innerHTML = `<div class="page-heading"><div><p class="section-name display">Tasks</p><h1>${isLibrary ? "结果库" : esc(batch().name)}</h1><p>${isLibrary ? "按会议主题、访谈对象和内容查找，结果保留原批次与来源。" : `创建于 ${esc(batch().date)} · ${scope.length} 个独立素材 · 词级时间戳${batch().align ? "已开启" : "关闭"}`}</p></div><div class="heading-actions">${isLibrary ? "" : `<button class="button" data-action="pause">${pauseLabel}</button>`}<button class="button primary" data-action="export-all" ${readyCount ? "" : "disabled"}>导出 ${readyCount} 个完整结果</button></div></div>
    <div class="summary-line">${isLibrary ? `<span><b>${scope.length}</b> 份结果</span><span><b>${scope.filter((entry) => !entry.seen).length}</b> 未查看</span><span class="warning-text"><b>${count("warning")}</b> 有缺失</span>` : `<span><b>${count("done")}</b> 完整</span><span class="warning-text"><b>${count("warning")}</b> 有缺失</span><span class="error-text"><b>${count("error")}</b> 失败</span><span><b>${count("running")}</b> 处理中</span><span><b>${count("queued")}</b> 等待</span><span class="summary-tail">结果与异常会保留在本批次</span>`}</div>
    ${active}<div class="table-toolbar"><label class="search-wrap"><span aria-hidden="true">${searchIcon}</span><input id="query" type="search" autocomplete="off" aria-label="${isLibrary ? "搜索会议、受访者或正文" : "搜索文件名或来源"}" placeholder="${isLibrary ? "搜索会议、受访者或正文…" : "搜索文件名或来源…"}" value="${esc(state.query)}"></label><div class="filter-set" aria-label="结果筛选">${filters.map(([id, label]) => `<button class="filter-button ${state.filter === id ? "active" : ""}" aria-pressed="${state.filter === id}" data-action="filter" data-id="${id}">${label}</button>`).join("")}</div></div>
    ${batchBar}<div class="table-scroll"><table class="task-table"><thead><tr><th><input type="checkbox" id="select-visible" aria-label="选择当前筛选中的 ${rows.length} 项"></th><th><button class="sort-button" data-action="sort">文件 / 来源 ${state.sort === "asc" ? "↑" : state.sort === "desc" ? "↓" : "↕"}</button></th><th>处理状态</th><th>查看</th><th>操作</th></tr></thead><tbody>${rows.map(rowHTML).join("") || `<tr><td colspan="5"><div class="empty">没有符合条件的文件<br><button class="text-button" data-action="clear-filter">清除搜索和筛选</button></div></td></tr>`}</tbody></table></div><div class="table-count"><span>显示 ${rows.length} 项${state.selected.size ? `，已选 ${state.selected.size} 项（跨筛选保留）` : ""}</span><span>查看排序不改变处理顺序</span></div>`;
  const selector = $("#select-visible");
  const selectedCount = rows.filter((entry) => state.selected.has(entry.id)).length;
  selector.checked = rows.length > 0 && selectedCount === rows.length;
  selector.indeterminate = selectedCount > 0 && selectedCount < rows.length;
  selector.disabled = rows.length === 0;
  $("#context-hint").textContent = isLibrary ? "打开会标记为已查看；已查看不代表已经人工校对。" : "本批任务与结果会保留，处理完成后可继续从这里查看。";
}
function renderTranscript() {
  const query = state.readerQuery;
  const matches = transcriptFor(task(state.resultId)).map((text, index) => ({ text, index })).filter(({ text }) => !query.trim() || text.toLowerCase().includes(query.trim().toLowerCase()));
  $("#transcript-lines").innerHTML = matches.map(({ text, index }) => `<div class="transcript-line"><span class="transcript-time">${segmentTime(task(state.resultId), index)}</span><p>${highlight(text, query)}</p></div>`).join("") || `<div class="empty">没有找到“${esc(query)}”<br>换一个关键词，或清除当前查找。</div>`;
}
function renderReader() {
  const entry = task(state.resultId);
  const owner = parent(entry.id);
  const siblings = state.originIds.map(task).filter((item) => item && readable(item));
  const index = siblings.findIndex((item) => item.id === entry.id);
  if ($("#breadcrumb")) $("#breadcrumb").textContent = owner.name + " / 结果阅读";
  $("#main").innerHTML = `<div class="reader-top"><button class="text-button" data-action="back">‹ 返回${state.origin?.view === "library" ? "结果库" : "批次清单"}</button><div><button class="button small" data-action="previous" ${index > 0 ? "" : "disabled"}>上一份</button><span class="subtle">${index + 1} / ${siblings.length}</span><button class="button small" data-action="next" ${index < siblings.length - 1 ? "" : "disabled"}>下一份</button><button class="button primary small" data-action="export-current">导出此份</button></div></div><div class="reader-layout"><aside class="sibling-list" aria-label="同批与当前筛选结果"><h3>${state.origin?.view === "library" || state.origin?.filter !== "all" || state.origin?.query ? "当前筛选中的结果" : "本批可查看的结果"}</h3>${siblings.map((item) => `<button class="sibling-button ${item.id === entry.id ? "selected" : ""}" data-action="open" data-id="${item.id}"><strong>${esc(plainName(item))}</strong><small>${item.status === "warning" ? "有缺失" : "完整结果"} · ${item.seen ? "已查看" : "未查看"}</small></button>`).join("")}</aside><article class="reader-document"><h1>${esc(displayTitle(entry))}</h1><div class="reader-meta">${esc(owner.name)} / ${esc(entry.path)}<br>源文件 ${esc(entry.filename)} · 时长 ${duration(entry)} · 已查看</div>${entry.status === "warning" ? `<div class="notice-box"><strong>这份结果有缺失</strong><br>02:14–02:39 未能识别，其他内容已保存。导出时会提示结果不完整。</div>` : ""}<label class="search-wrap reader-search"><span aria-hidden="true">${searchIcon}</span><input id="reader-query" type="search" aria-label="在这份结果中查找" placeholder="在这份结果中查找…" value="${esc(state.readerQuery)}"></label><div class="transcript" id="transcript-lines"></div><div class="reader-audio"><span class="play-symbol" aria-hidden="true">▷</span><span>交互示意未连接媒体。正式版本在这里回听，并记住播放位置。<br><span class="subtle">音频被清理时，保留文字并提供重新关联源文件入口。</span></span></div></article></div>`;
  renderTranscript();
  $("#context-hint").textContent = "新任务完成只更新状态，不会切换正在阅读的这份结果。";
}
function render() {
  document.body.dataset.view = state.view;
  const activeId = document.activeElement?.id;
  const cursor = document.activeElement?.selectionStart;
  const activeAction = document.activeElement?.dataset.action;
  const activeTask = document.activeElement?.dataset.id;
  const activeSelect = document.activeElement?.dataset.select;
  renderSidebar();
  $("#main").classList.toggle("list-view", state.view === "tasks");
  state.view === "reader" ? renderReader() : state.view === "library" ? renderLibrary() : renderList();
  const pos = state.positions[key()];
  $("#main").scrollTop = pos?.main || 0;
  window.scrollTo({top: pos?.windowY || 0, left:0, behavior:"instant"});
  if ($(".table-scroll")) $(".table-scroll").scrollTop = pos?.table || 0;
  if (["query", "reader-query"].includes(activeId) && $("#" + activeId)) { const field = $("#" + activeId); field.focus({ preventScroll: true }); if (cursor != null) field.setSelectionRange(cursor, cursor); }
  else if (activeSelect) { $("#main").querySelector(`[data-select="${activeSelect}"]`)?.focus({ preventScroll: true }); }
  else if (activeAction) { [...$("#main").querySelectorAll("[data-action]")].find((button) => button.dataset.action === activeAction && button.dataset.id === activeTask)?.focus({ preventScroll: true }); }
}
function openResult(id) {
  if (!readable(task(id))) return;
  if (state.view !== "reader") { savePosition(); state.origin = { view: state.view, batchId: state.batchId, query: state.query, filter: state.filter, sort: state.sort }; state.originIds = filteredRows().filter(readable).map((entry) => entry.id); }
  state.view = "reader"; state.resultId = id; state.lastReadId = id; state.readerQuery = ""; state.batchId = parent(id).id; task(id).seen = true;
  state.positions[key()] = { main: 0, table: 0 }; render(); $("#main").focus({ preventScroll: true });
}
function startNext() { if (state.paused || running()) return; const next = all().find((entry) => entry.status === "queued"); if (next) { next.status = "running"; next.progress = 3; } }
function advance() {
  savePosition(); const current = running();
  if (!current) { startNext(); render(); toast(running() ? "已模拟开始处理下一个文件。" : state.paused ? "已暂停；继续处理后才会启动下一项。" : "所有文件都已结束。可以在结果库查看。"); return; }
  current.status = "done"; current.seen = false; current.progress = 100; startNext(); render();
  toast(`${current.filename} 已完成。${state.view === "reader" ? "当前阅读内容保持不变。" : state.paused ? "全部处理现已暂停。" : "结果已留在本批次。"}`);
}
function openExport(ids) {
  state.exportIds = ids.filter((id) => readable(task(id)));
  const complete = state.exportIds.filter((id) => task(id).status === "done").length;
  const partial = state.exportIds.length - complete;
  $("#export-summary").textContent = `${complete} 个完整结果${partial ? `，${partial} 个有缺失的结果默认不包含` : ""}。`;
  $("#partial-option").hidden = partial === 0;
  $("#include-partial").checked = false;
  $("#export-format").value = parent(state.exportIds[0])?.format || "SRT";
  $("#export-dialog").showModal(); updateExport();
}
function updateExport() {
  const number = state.exportIds.filter((id) => task(id).status === "done" || $("#include-partial").checked).length;
  $("#submit-export").textContent = `准备导出 ${number} 份结果`;
  $("#submit-export").disabled = number === 0;
}
function importReason(file) { return !supported.test(file.name) ? "不支持的文件类型" : file.size > 2 * 1024 ** 3 ? "超过单文件大小上限" : ""; }
function updateImport() {
  const valid = state.importFiles.filter((file) => file.selected && !importReason(file));
  const excluded = state.importFiles.filter((file) => importReason(file)).length;
  $("#drop-area").classList.toggle("has-files", state.importFiles.length > 0);
  $("#import-summary").textContent = state.importFiles.length ? `发现 ${state.importFiles.length} 项，已选 ${valid.length} 项${excluded ? `，排除 ${excluded} 项` : ""}` : "尚未选择文件";
  $("#import-list").innerHTML = state.importFiles.map((file) => `<div class="import-row"><input type="checkbox" data-import-select="${file.id}" aria-label="选择 ${esc(file.name)}" ${file.selected && !importReason(file) ? "checked" : ""} ${importReason(file) ? "disabled" : ""}><div title="${esc(file.path)}">${esc(file.name)}<div class="file-meta">${esc(file.path)}</div></div><span class="${importReason(file) ? "warning-text" : "subtle"}">${importReason(file) || (file.size / 1048576).toFixed(0) + " MB"}</span><button class="icon-button" data-action="remove-import" data-id="${file.id}" aria-label="移除 ${esc(file.name)}">×</button></div>`).join("");
  $("#submit-import").textContent = running() || state.paused ? `加入待处理 · ${valid.length} 个文件` : `开始处理 ${valid.length} 个文件`;
  $("#submit-import").disabled = !valid.length;
  $("#import-order").textContent = state.paused ? "全部处理当前已暂停。导入后需点击继续处理。" : running() ? "将加入当前批次之后；不会中断正在处理的文件。" : "导入完成后按清单顺序依次处理。";
}
function addFiles(files) {
  for (const file of files) state.importFiles.push({ id: "u" + (++state.fileCounter), name: file.name, path: file.webkitRelativePath || file.name, size: file.size, selected: true });
  const folder = Array.from(files)[0]?.webkitRelativePath?.split("/")[0];
  if (folder) $("#batch-name").value = folder;
  updateImport();
}
const actions = {
  tasks: () => { savePosition(); state.view = "tasks"; state.query = ""; state.filter = "all"; state.selected.clear(); render(); },
  library: () => { savePosition(); state.view = "library"; state.query = ""; state.filter = "all"; state.selected.clear(); render(); },
  batch: (id) => { savePosition(); state.batchId = id; state.view = "tasks"; state.query = ""; state.filter = "all"; state.selected.clear(); render(); },
  open: openResult,
  back: () => { if (!state.origin) return; Object.assign(state, state.origin); state.resultId = null; render(); $("#main").focus({ preventScroll: true }); },
  previous: () => { const at = state.originIds.indexOf(state.resultId); if (at > 0) openResult(state.originIds[at - 1]); },
  next: () => { const at = state.originIds.indexOf(state.resultId); if (at < state.originIds.length - 1) openResult(state.originIds[at + 1]); },
  filter: (id) => { savePosition(); state.filter = id; render(); },
  "clear-filter": () => { state.query = ""; state.filter = "all"; render(); },
  sort: () => { savePosition(); state.sort = state.sort === "asc" ? "desc" : "asc"; render(); toast("已更改查看排序，处理顺序保持不变。"); },
  "clear-selection": () => { savePosition(); state.selected.clear(); render(); },
  pause: () => { savePosition(); state.paused = !state.paused; startNext(); render(); toast(state.paused ? running() ? "已请求暂停：当前文件完成后停止启动新文件。" : "全部处理已暂停。" : "已恢复处理。"); },
  retry: (id) => { savePosition(); task(id).status = "queued"; startNext(); render(); toast("已重新加入待处理，其他结果继续保留。"); },
  "retry-selected": () => { savePosition(); const rows = [...state.selected].map(task).filter((entry) => entry.status === "error"); rows.forEach((entry) => entry.status = "queued"); startNext(); render(); toast(`已重新加入 ${rows.length} 个失败任务。`); },
  explain: (id) => toast(task(id).status === "error" ? "转写请求超时。可以重新加入处理；正式版本会提供可展开的错误详情。" : "文件尚无结果。处理完成后，可以从这里打开。"),
  "export-all": () => openExport((state.view === "library" ? filteredRows() : batch().items).map((entry) => entry.id)),
  "export-selected": () => openExport([...state.selected]),
  "export-current": () => openExport([state.resultId]),
  "close-export": () => $("#export-dialog").close(),
  "submit-export": () => { const count = state.exportIds.filter((id) => task(id).status === "done" || $("#include-partial").checked).length; $("#export-dialog").close(); toast(`导出范围已确认：${count} 份 ${$("#export-format").value}，打包为一个 ZIP（流程示意）。`); },
  import: () => { updateImport(); $("#import-dialog").showModal(); },
  "close-import": () => $("#import-dialog").close(),
  "pick-files": () => $("#file-picker").click(),
  "pick-folder": () => $("#folder-picker").click(),
  sample: () => { state.importFiles = [["访谈/A组/开场.mp4", 85], ["访谈/B组/开场.mp4", 85], ["会议/项目复盘.mp4", 260], ["访谈/用户反馈.mp4", 180], ["会议/议程.txt", 0.02]].map(([path, mb]) => ({ id: "u" + (++state.fileCounter), name: path.split("/").pop(), path, size: mb * 1048576, selected: true })); $("#batch-name").value = "新一批会议与访谈"; updateImport(); },
  "remove-import": (id) => { state.importFiles = state.importFiles.filter((file) => file.id !== id); updateImport(); },
  "submit-import": () => {
    const name = $("#batch-name").value.trim(); if (!name) { toast("请给这一批素材起个名称，方便之后查找。"); $("#batch-name").focus(); return; }
    const selected = state.importFiles.filter((file) => file.selected && !importReason(file)); if (!selected.length) return;
    const entry = { id: "b" + (++state.batchCounter), name, date: "2026-10-30 10:20", align: $("#align-option").checked, format: $("#import-format").value, items: selected.map((file) => ({ id: "t" + (++state.taskCounter), filename: file.name, status: "queued", path: file.path, duration: null, seen: false })) };
    state.batches.push(entry); state.batchId = entry.id; state.view = "tasks"; state.query = ""; state.filter = "all"; state.selected.clear(); state.importFiles = []; startNext(); $("#import-dialog").close(); render(); toast(`已创建“${name}”，${entry.items.length} 个文件已加入待处理（示意）。`);
  },
  storage: () => $("#storage-dialog").showModal(),
  "close-storage": () => $("#storage-dialog").close(),
  advance,
  reset: () => { state.batches = initialBatches(); Object.assign(state, { view: "library", batchId: "b1", query: "", filter: "all", sort: "import", paused: false, resultId: null, readerQuery: "", origin: null, originIds: [], positions: {}, importFiles: [], batchCounter: 2, taskCounter: 14 }); state.selected.clear(); document.querySelectorAll("dialog[open]").forEach((dialog) => dialog.close()); render(); toast("已恢复示例数据。"); },
};
document.addEventListener("click", (event) => { const button = event.target.closest("[data-action]"); if (!button || button.disabled) return; actions[button.dataset.action]?.(button.dataset.id); });
document.addEventListener("input", (event) => {
  if (event.target.id === "query") { savePosition(); state.query = event.target.value; render(); }
  if (event.target.id === "reader-query") { state.readerQuery = event.target.value; renderTranscript(); }
});
document.addEventListener("change", (event) => {
  if (event.target.dataset.select) { savePosition(); event.target.checked ? state.selected.add(event.target.dataset.select) : state.selected.delete(event.target.dataset.select); render(); }
  if (event.target.id === "select-visible") { savePosition(); filteredRows().forEach((entry) => event.target.checked ? state.selected.add(entry.id) : state.selected.delete(entry.id)); render(); }
  if (event.target.dataset.importSelect) { state.importFiles.find((file) => file.id === event.target.dataset.importSelect).selected = event.target.checked; updateImport(); }
  if (event.target.id === "file-picker" || event.target.id === "folder-picker") { addFiles(event.target.files); event.target.value = ""; }
  if (event.target.id === "include-partial") updateExport();
});
const drop = $("#drop-area");
drop.addEventListener("dragover", (event) => { event.preventDefault(); drop.classList.add("over"); });
drop.addEventListener("dragleave", () => drop.classList.remove("over"));
drop.addEventListener("drop", (event) => { event.preventDefault(); drop.classList.remove("over"); addFiles(event.dataTransfer.files); });
/* DESIGN_EXTENSION */

/* The initial render runs in views.js after the design views are defined. */
