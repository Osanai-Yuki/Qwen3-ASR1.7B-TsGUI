/* Gizmodo-inspired views for this standalone design draft. */
"use strict";
const meetingTexts = [
  "今天先确认本周的交付范围。用户访谈已经完成整理，待处理的问题需要保留到下一次评审。",
  "对会议记录来说，最重要的是之后能找到当时的原话，而不只是记得大概讨论过什么。",
  "字幕和文字记录导出后，需要保留对应的原文件名。同事拿到文件时，也能知道这段内容来自哪一次会议。",
  "接下来分两步推进：先确认任务范围，再把已经完成的内容交付。没有处理好的素材单独列出。",
  "如果一份视频只有局部没有识别好，我们可以先查看其他部分，再决定是否重新处理。",
  "最后再确认一下决定与后续动作。这个版本的会议记录会保留下来，下次可以接着查看。",
];
const titles = {
  t1: "产品周会：这周我们决定了什么",
  t2: "陈女士访谈：第一次使用时，我卡在了哪里",
  t3: "林先生访谈：从一个问题开始找答案",
  t4: "订单流程评审：把交付范围说清楚",
  t5: "移动端项目复盘：记录那些容易忘记的细节",
  t6: "周女士访谈：查找、回听与再次确认",
  t13: "增长例会：留住讨论的上下文",
  t14: "上海渠道访谈：把现场反馈带回来",
};
function displayTitle(entry) { return titles[entry.id] || plainName(entry); }
// Categories here are explicit sample metadata; production uses user-set fields.
function kindOf(entry) {
  if (["t1", "t4", "t5", "t7", "t11", "t13"].includes(entry.id)) return "meeting";
  if (["t2", "t3", "t6", "t8", "t9", "t10", "t12", "t14"].includes(entry.id)) return "interview";
  return "unclassified";
}
function transcriptFor(entry) { return entry && kindOf(entry) === "meeting" ? meetingTexts : texts; }
function segmentTime(entry, index) {
  const interviewTimes = ["12:40", "13:08", "14:12", "15:30", "17:00", "18:25", "19:10"];
  return entry && kindOf(entry) !== "meeting" ? interviewTimes[index] || "20:00" : String(index).padStart(2, "0") + ":00";
}
function waveform(seed = 1) {
  const bars = Array.from({length:92}, (_, index) => {
    const height = 5 + Math.abs(Math.sin(index * 1.73 + seed) * Math.cos(index * .39 + seed)) * 65;
    const x = index * 8 + 4;
    return `<line x1="${x}" y1="${48-height/2}" x2="${x}" y2="${48+height/2}" stroke="currentColor" stroke-width="3.2" stroke-linecap="round"/>`;
  }).join("");
  return `<svg viewBox="0 0 742 96" preserveAspectRatio="none" aria-hidden="true">${bars}</svg>`;
}
function resultCard(entry, index) {
  const source = parent(entry.id);
  const query = state.query.trim();
  const matchIndex = transcriptFor(entry).findIndex(text => text.toLowerCase().includes(query.toLowerCase()));
  const snippet = query && matchIndex >= 0 ? `<p class="snippet"><span class="snippet-time">${segmentTime(entry, matchIndex)}</span>${highlight(transcriptFor(entry)[matchIndex], query)}</p>` : "";
  return `<article class="result-card"><div class="media"><label class="card-select"><input type="checkbox" data-select="${entry.id}" aria-label="选择 ${esc(entry.filename)}" ${state.selected.has(entry.id) ? "checked" : ""}></label>${waveform(index + 3)}<span class="media-label">音频示意 · ${duration(entry)}</span></div><button class="card-title" data-action="open" data-id="${entry.id}">${esc(displayTitle(entry))}</button><p class="card-meta">${esc(entry.filename)}<br>${esc(source.name)} / ${esc(entry.path)}</p>${snippet}<div class="card-state">${entry.status === "warning" ? '<span class="warning-text">△ 这份结果有缺失</span>' : entry.seen ? '✓ 已查看' : '<span class="unseen">未查看</span>'}</div></article>`;
}
function renderLibrary() {
  const rows = filteredRows();
  const complete = rows.filter(entry => entry.status === "done").length;
  const filtered = Boolean(state.query.trim() || state.filter !== "all" || state.kind !== "all");
  const featured = task(state.lastReadId) && readable(task(state.lastReadId)) ? task(state.lastReadId) : rows[0];
  const selected = [...state.selected].map(task).filter(Boolean);
  const selectionBar = selected.length ? `<div class="batch-bar"><span>已选 ${selected.length} 份</span><span class="subtle">选择跨筛选保留</span><span class="spacer"></span><button class="button small" data-action="export-selected">导出所选</button><button class="text-button" data-action="clear-selection">取消选择</button></div>` : "";
  const feature = !filtered && featured ? `<article class="feature"><div class="feature-meta"><span class="display">Oct 30,2026</span><span>继续阅读</span><span>上次读到 ${segmentTime(featured, 0)}</span></div><button class="feature-title" data-action="open" data-id="${featured.id}">${esc(displayTitle(featured))}</button><p class="feature-source">${esc(featured.filename)} / ${esc(parent(featured.id).name)}</p><p class="feature-quote">${esc(transcriptFor(featured)[3] || transcriptFor(featured)[0])}</p><div class="feature-media">${waveform(8)}<span class="media-label">示例音频图形 · ${duration(featured)}</span></div><div class="feature-bottom"><span>${featured.status === "warning" ? "△ 有缺失的部分结果" : "完整转写已保留"}</span><button class="button" data-action="open" data-id="${featured.id}">接着阅读</button></div></article>` : "";
  $("#main").innerHTML = `<div class="library-heading"><div><h1><span class="display">Library</span><span class="cn">结果库</span></h1><p>按会议主题、访谈对象或一句原话查找。</p></div><span class="library-count">${rows.length} 份可查看</span></div>${selectionBar}${feature}<div class="section-heading"><h2 class="display">${filtered ? "Found" : "Latest"}</h2><span>${filtered ? `当前条件下有 ${rows.length} 份结果` : "最近生成的会议与访谈"}</span></div><div class="library-toolbar"><span>${state.query ? `正文与素材搜索：${esc(state.query)}` : "打开即标记为已查看，原文件名和来源继续保留。"}</span><button class="text-button" data-action="export-all" ${complete ? "" : "disabled"}>导出 ${complete} 份完整结果</button></div><div class="result-grid">${rows.map(resultCard).join("") || `<div class="empty">没有符合当前条件的结果。<br><button class="text-button" data-action="clear-library-filter">清除搜索与筛选</button></div>`}</div>`;
  $("#context-hint").textContent = "结果与批次持续关联；已查看不等于已经人工校对。";
}
function renderSidebar() {
  renderBaseSidebar();
  const globalInput = $("#global-search");
  if (document.activeElement !== globalInput) globalInput.value = state.view === "library" ? state.query : "";
  const kinds = [["all", "全部"], ["meeting", "会议"], ["interview", "访谈"]];
  $("#sidebar-filters").innerHTML = kinds.map(([id, label]) => `<button class="tag" data-action="kind" data-id="${id}" aria-pressed="${state.view === "library" && state.kind === id}"># ${label}</button>`).join("") + [["unseen", "未查看"], ["attention", "有缺失"]].map(([id, label]) => `<button class="tag" data-action="library-filter" data-id="${id}" aria-pressed="${state.view === "library" && state.filter === id}"># ${label}</button>`).join("");
  const current = running();
  const pending = all().filter(entry => entry.status === "queued").length;
  $("#queue-context").innerHTML = `<div class="side-heading"><h2 class="display">Queue</h2><span>${state.paused ? current ? "等待暂停" : "已暂停" : current ? "正在处理" : "就绪"}</span></div>${current ? `<p class="queue-name">${esc(current.filename)}</p><p class="queue-description">${state.paused ? "当前文件完成后暂停全部处理。" : "你可以继续查看旧结果，处理会在本机继续。"}</p><div class="queue-progress"><span style="width:${current.progress || 3}%"></span></div><div class="queue-meta"><span>本文件 ${current.progress || 3}%</span><span>${pending} 项等待</span></div>` : `<p class="queue-description">${pending ? `${pending} 项等待${state.paused ? "，恢复后继续处理。" : "。"}` : "本次处理已结束，结果继续保留。"}</p>`}<button class="text-button" data-action="pause">${state.paused ? current ? "取消暂停" : "继续处理" : "当前文件完成后暂停"}</button>`;
  if (state.view === "tasks") {
    $("#queue-context").innerHTML = `<div class="side-heading"><h2 class="display">Setup</h2><span>本批配置</span></div><p class="queue-name">Qwen3-ASR-1.7B</p><p class="queue-description">语种自动检测<br>${batch().align ? "词级时间戳已开启" : "段级时间戳"}<br>按文件逐个处理</p><button class="text-button" data-action="menu">查看模型与存储说明</button>`;
  }
  const context = $("#context-results");
  context.hidden = state.view !== "reader";
  if (state.view === "reader") {
    const siblings = state.originIds.map(task).filter(entry => entry && readable(entry));
    context.innerHTML = `<div class="side-heading"><h2 class="display">In view</h2><span>当前结果</span></div>${siblings.map(entry => `<button class="context-result ${entry.id === state.resultId ? "current" : ""}" data-action="open" data-id="${entry.id}"><span>${esc(displayTitle(entry))}</span><small>${entry.status === "warning" ? "有缺失" : "完整结果"} · ${entry.seen ? "已查看" : "未查看"}</small></button>`).join("")}`;
  }
}

state.kind = "all";
state.lastReadId = "t2";
actions.kind = id => { savePosition(); state.view = "library"; state.kind = id; state.filter = "all"; render(); };
actions["library-filter"] = id => { savePosition(); state.view = "library"; state.filter = state.filter === id ? "all" : id; render(); };
actions["clear-library-filter"] = () => { state.query = ""; state.kind = "all"; state.filter = "all"; render(); };
actions.menu = () => $("#menu-dialog").showModal();
actions["close-menu"] = () => $("#menu-dialog").close();
actions.batches = () => {
  $("#mobile-batch-list").innerHTML = state.batches.map(entry => `<button class="batch-button" data-action="batch" data-id="${entry.id}"><strong>${esc(entry.name)}</strong><small>${entry.items.length} 个文件，${entry.items.filter(readable).length} 份可查看</small></button>`).join("");
  $("#batches-dialog").showModal();
};
actions["close-batches"] = () => $("#batches-dialog").close();
const originalReset = actions.reset;
actions.reset = () => { state.kind = "all"; state.lastReadId = "t2"; originalReset(); };

document.addEventListener("input", event => {
  if (event.target.id !== "global-search") return;
  savePosition(); state.view = "library"; state.query = event.target.value; render();
  history.replaceState(routeSnapshot(), "", "#library");
});
document.addEventListener("keydown", event => {
  const editing = /INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName || "");
  if ((event.key === "/" && !editing) || ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k")) {
    if (document.querySelector("dialog[open]")) return;
    if (state.view !== "library" && innerWidth < 1100) actions.library();
    event.preventDefault(); $("#global-search").focus();
  }
});

function routeSnapshot() {
  return {view:state.view,batchId:state.batchId,resultId:state.resultId,query:state.query,filter:state.filter,kind:state.kind,sort:state.sort,origin:state.origin,originIds:state.originIds,readerQuery:state.readerQuery};
}
function routeHash() { return state.view === "reader" ? "#result/" + state.resultId : "#" + state.view; }
const navigations = new Set(["tasks", "library", "batch", "open", "back", "previous", "next", "kind", "library-filter", "clear-library-filter", "reset", "submit-import"]);
// Capture the list context before the inherited click handler navigates.
document.addEventListener("click", event => {
  const button = event.target.closest("[data-action]");
  if (button?.dataset.action === "batch" && $("#batches-dialog").open) $("#batches-dialog").close();
  if (button && !button.disabled && navigations.has(button.dataset.action)) history.replaceState(routeSnapshot(), "", routeHash());
}, true);
document.addEventListener("click", event => {
  const button = event.target.closest("[data-action]");
  if (button && !button.disabled && navigations.has(button.dataset.action) && location.hash !== routeHash()) history.pushState(routeSnapshot(), "", routeHash());
});
window.addEventListener("popstate", event => {
  savePosition();
  if (event.state) Object.assign(state, event.state);
  else state.view = location.hash === "#tasks" ? "tasks" : "library";
  render();
});
if (location.hash === "#tasks") state.view = "tasks";
if (location.hash.startsWith("#result/")) {
  const id = location.hash.slice(8);
  if (task(id) && readable(task(id))) {
    state.origin = {view:"library",batchId:state.batchId,query:"",filter:"all",sort:"import"};
    state.originIds = all().filter(readable).map(entry => entry.id);
    state.view = "reader"; state.resultId = id; state.batchId = parent(id).id; task(id).seen = true;
  }
}
history.replaceState(routeSnapshot(), "", routeHash());
render();
