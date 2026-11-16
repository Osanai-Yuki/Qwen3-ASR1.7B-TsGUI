// Real-Chrome verification of the repaired UI behaviours via CDP.
// Launched by the repair round against an isolated backend (fixture_server.py).
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.dirname(fileURLToPath(import.meta.url));
const OUT = path.join(ROOT, "browser-evidence");
fs.mkdirSync(OUT, { recursive: true });
const seeds = JSON.parse(fs.readFileSync(path.join(ROOT, "runtime", "seeds.json"), "utf8"));
const BASE = "http://127.0.0.1:8765";

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

class CDP {
  constructor(ws) {
    this.ws = ws;
    this.nextId = 0;
    this.pending = new Map();
    this.browserEvents = [];
    ws.onmessage = (event) => {
      const message = JSON.parse(event.data);
      if (message.method === "Runtime.exceptionThrown") {
        this.browserEvents.push({
          type: "exception",
          text: message.params.exceptionDetails?.text,
          description: message.params.exceptionDetails?.exception?.description,
        });
      }
      if (message.method === "Log.entryAdded" && message.params.entry.level === "error") {
        this.browserEvents.push({ type: "log", text: message.params.entry.text });
      }
      if (message.id && this.pending.has(message.id)) {
        const { resolve, reject } = this.pending.get(message.id);
        this.pending.delete(message.id);
        if (message.error) reject(new Error(JSON.stringify(message.error)));
        else resolve(message.result);
      }
    };
  }
  send(method, params = {}) {
    return new Promise((resolve, reject) => {
      const id = ++this.nextId;
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }
}

async function newPage() {
  const created = await fetch(`http://127.0.0.1:9223/json/new?${encodeURIComponent("about:blank")}`, {
    method: "PUT",
  }).then((response) => response.json());
  const ws = new WebSocket(created.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    ws.onopen = resolve;
    ws.onerror = reject;
  });
  const cdp = new CDP(ws);
  await cdp.send("Page.enable");
  await cdp.send("Runtime.enable");
  await cdp.send("Log.enable");
  await cdp.send("Network.enable");
  await cdp.send("Network.setCacheDisabled", { cacheDisabled: true });
  return { cdp, targetId: created.id };
}

async function main() {
  const { cdp, targetId } = await newPage();
  const evaluate = async (body) => {
    const result = await cdp.send("Runtime.evaluate", {
      expression: `(async () => { ${body} })()`,
      awaitPromise: true,
      returnByValue: true,
      userGesture: true,
    });
    if (result.exceptionDetails) {
      throw new Error(result.exceptionDetails.exception?.description || JSON.stringify(result.exceptionDetails));
    }
    return result.result.value;
  };
  const waitFor = async (body, timeout = 15000, label = body) => {
    const started = Date.now();
    while (Date.now() - started < timeout) {
      try {
        const value = await evaluate(body);
        if (value) return value;
      } catch {
        // page may still be navigating
      }
      await sleep(200);
    }
    throw new Error(`timeout waiting for ${label}`);
  };
  const clickText = async (selector, text) => {
    const ok = await evaluate(`
      const nodes = [...document.querySelectorAll(${JSON.stringify(selector)})];
      const node = nodes.find((n) => (n.textContent || "").includes(${JSON.stringify(text)}));
      if (!node) return false;
      node.click();
      return true;
    `);
    if (!ok) throw new Error(`could not click ${selector} containing ${text}`);
  };
  const setInput = async (selector, value) => {
    const ok = await evaluate(`
      const input = document.querySelector(${JSON.stringify(selector)});
      if (!input) return false;
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
      setter.call(input, ${JSON.stringify(value)});
      input.dispatchEvent(new Event("input", { bubbles: true }));
      return true;
    `);
    if (!ok) throw new Error(`no input ${selector}`);
  };
  const setViewport = async (width, height, mobile) => {
    await cdp.send("Emulation.setDeviceMetricsOverride", {
      width, height, deviceScaleFactor: 1, mobile,
      screenWidth: width, screenHeight: height,
    });
    await sleep(250);
  };
  const navigate = async (url) => {
    await cdp.send("Page.navigate", { url });
    await waitFor("return document.readyState === 'complete'", 20000, "load");
    await waitFor(
      "const r = await fetch('/api/readiness').then((x) => x.json()); return r.ready === true;",
      20000, "readiness",
    );
    await waitFor("return !!document.querySelector('.asr-result-grid, .asr-empty')", 15000, "library");
  };
  const screenshot = async (name) => {
    const { data } = await cdp.send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
    fs.writeFileSync(path.join(OUT, name), Buffer.from(data, "base64"));
  };
  const openCard = async (title) => {
    await waitFor("return !!document.querySelector('.asr-result-grid')", 15000, "library cards");
    await clickText(".asr-result-open", title);
    await waitFor("return !!document.querySelector('.asr-reader-document h1')", 15000, "reader");
    await sleep(400);
  };
  const backToLibrary = async () => {
    await clickText(".asr-reader-top .asr-text-button", "返回");
    await waitFor("return !!document.querySelector('.asr-result-grid')", 15000, "library after back");
  };
  const anchorVisibility = async (segmentIndex) => evaluate(`
    const paragraphs = [...document.querySelectorAll(".asr-paragraph-view p")];
    const lines = [...document.querySelectorAll(".asr-transcript-line")];
    let chosen = null;
    if (lines.length) chosen = lines[${Number(segmentIndex)}] || null;
    if (!chosen) {
      for (const node of paragraphs) {
        const index = Number(node.dataset.segmentIndex ?? -1);
        if (index >= 0 && index <= ${Number(segmentIndex)}) chosen = node;
      }
    }
    const all = [...paragraphs, ...lines];
    const first = all.find((node) => {
      const rect = node.getBoundingClientRect();
      return rect.bottom > 0 && rect.top < window.innerHeight;
    });
    const rect = chosen?.getBoundingClientRect();
    return {
      segmentIndex: ${Number(segmentIndex)},
      paragraphs: paragraphs.length,
      lines: lines.length,
      chosenText: chosen?.textContent?.slice(0, 60) || null,
      visible: Boolean(rect && rect.bottom > 0 && rect.top < window.innerHeight),
      top: rect ? Math.round(rect.top) : null,
      bottom: rect ? Math.round(rect.bottom) : null,
      viewport: window.innerHeight,
      firstVisible: first?.textContent?.slice(0, 60) || null,
      scrollY: Math.round(window.scrollY),
    };
  `);

  const results = { generated_at: new Date().toISOString(), checks: [] };
  const record = (name, pass, detail) => {
    results.checks.push({ name, pass, detail });
    console.log(`${pass ? "PASS" : "FAIL"} ${name}`, JSON.stringify(detail));
  };

  try {
    await setViewport(1280, 820, false);
    await navigate(BASE + "/");

    // 1. Plain-text find: 80 highlighted lines and no false "no match".
    await openCard("复验纯文本记录");
    await setInput(".asr-reader-search input", "独立复验");
    await waitFor("const s = document.querySelector('.asr-reader-hits [role=status]'); return !!s && s.textContent.includes('/');", 5000, "hit status");
    await sleep(400);
    const plain = await evaluate(`
      const status = document.querySelector(".asr-reader-hits [role=status]")?.textContent || "";
      const zero = document.querySelector(".asr-reader-document .asr-empty-inline[role=status]")?.textContent || "";
      return { status, marks: document.querySelectorAll(".asr-reader-document mark").length, zero };
    `);
    record("plain-text search counts highlights without a no-match message", plain.marks > 0 && plain.status.includes("80") && !plain.zero, plain);
    await screenshot("01-plain-text-search.png");

    // 2. Paragraph find: navigation controls and the hit scrolled into view.
    await backToLibrary();
    await openCard("复验分段记录");
    await setInput(".asr-reader-search input", "定位词");
    await waitFor("const s = document.querySelector('.asr-reader-hits [role=status]'); return !!s && s.textContent.includes('/');", 5000, "hit status");
    await sleep(900);
    const paragraph = await evaluate(`
      const status = document.querySelector(".asr-reader-hits [role=status]")?.textContent || "";
      const buttons = [...document.querySelectorAll(".asr-reader-hits button")].map((b) => ({ text: b.textContent, disabled: b.disabled }));
      const mark = document.querySelector(".asr-reader-document mark");
      const rect = mark?.getBoundingClientRect();
      return { status, buttons, markVisible: Boolean(rect && rect.bottom > 0 && rect.top < window.innerHeight), top: rect ? Math.round(rect.top) : null, viewport: window.innerHeight };
    `);
    record("paragraph search exposes prev/next and positions the hit", paragraph.status.includes("1 / 1") && paragraph.buttons.length === 2 && paragraph.buttons.every((b) => !b.disabled) && paragraph.markVisible, paragraph);
    await screenshot("02-paragraph-hit-navigation.png");

    // 3. Selection survives a filter that hides it.
    await evaluate("document.querySelector('.asr-nav-links button')?.click(); return true;");
    await waitFor("return !!document.querySelector('.asr-tasks-page')", 15000, "tasks page");
    await clickText(".asr-batch-link", "筛选验证批次");
    await waitFor("return [...document.querySelectorAll('.asr-task-row')].some((r) => r.textContent.includes('可导出-甲.wav'));", 15000, "selection batch rows");
    const picked = await evaluate(`
      const click = (label) => {
        const box = document.querySelector('input[aria-label="选择 ' + label + '"]');
        if (!box) return false;
        box.click();
        return true;
      };
      return { a: click("可导出-甲.wav"), c: click("重试-丙.wav") };
    `);
    await sleep(300);
    const beforeFilter = await evaluate("return document.querySelector('.asr-bulk-bar')?.textContent || '';");
    await setInput(".asr-task-toolbar input", "乙");
    await sleep(500);
    const afterFilter = await evaluate(`
      const bar = document.querySelector(".asr-bulk-bar")?.textContent || "";
      const retry = [...document.querySelectorAll(".asr-bulk-bar button")].find((b) => b.textContent.includes("批量重新处理"));
      return { bar, retry: retry ? { text: retry.textContent, disabled: retry.disabled } : null, rows: document.querySelectorAll(".asr-task-row").length };
    `);
    record(
      "hidden selection keeps full export/retry scope",
      picked.a && picked.c && beforeFilter.includes("可导出 1 项") && beforeFilter.includes("可重试 1 项")
        && afterFilter.bar.includes("已选 2 项") && afterFilter.bar.includes("可导出 1 项")
        && afterFilter.bar.includes("可重试 1 项") && afterFilter.bar.includes("在当前筛选外")
        && afterFilter.retry && !afterFilter.retry.disabled,
      { beforeFilter, afterFilter, picked },
    );
    await screenshot("03-hidden-selection.png");

    // 4. Mobile: a new scroll position is saved and restored after reload.
    await setViewport(390, 844, true);
    await navigate(BASE + "/");
    // Strict reset: prove the *new* scroll is what got saved, not an old value.
    await evaluate(`await fetch("/api/history/${seeds.segmented_id}/view", { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ reader_position: { segment_index: 0, scroll_ratio: 0 } }) }); return true;`);
    await openCard("复验分段记录");
    await evaluate("window.scrollTo(0, 4000); return { y: window.scrollY, height: document.documentElement.scrollHeight };");
    await sleep(2800);
    const mobileSaved = await evaluate(`const d = await fetch("/api/history/${seeds.segmented_id}").then((r) => r.json()); return d.reader_position;`);
    const beforeReload = await anchorVisibility(mobileSaved?.segment_index ?? 0);
    await cdp.send("Page.reload");
    await waitFor("return !!document.querySelector('.asr-reader-document h1')", 20000, "reader reload");
    await sleep(1800);
    const afterReload = await anchorVisibility(mobileSaved?.segment_index ?? 0);
    record(
      "mobile scroll saves a new anchor and reload restores it",
      Boolean(mobileSaved && mobileSaved.segment_index > 0) && beforeReload.visible && afterReload.visible,
      { saved: mobileSaved, beforeReload, afterReload },
    );
    await screenshot("04-mobile-restored.png");

    // 4b. Plain-text (no segments) keeps a character anchor across reload.
    await navigate(BASE + "/");
    await evaluate(`await fetch("/api/history/${seeds.plain_id}/view", { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ reader_position: { text_offset: 0, scroll_ratio: 0 } }) }); return true;`);
    await openCard("复验纯文本记录");
    await evaluate("window.scrollTo(0, 3000); return { y: window.scrollY, height: document.documentElement.scrollHeight };");
    await sleep(2800);
    const plainSaved = await evaluate(`const d = await fetch("/api/history/${seeds.plain_id}").then((r) => r.json()); return d.reader_position;`);
    const plainBefore = await evaluate(`
      const paragraphs = [...document.querySelectorAll(".asr-plain-transcript p")];
      const chosen = paragraphs.find((p) => Number(p.dataset.offset ?? -1) >= ${Number(plainSaved?.text_offset ?? 0)});
      const rect = chosen?.getBoundingClientRect();
      return { text: chosen?.textContent?.slice(0, 40) || null, visible: Boolean(rect && rect.bottom > 0 && rect.top < window.innerHeight), top: rect ? Math.round(rect.top) : null };
    `);
    await cdp.send("Page.reload");
    await waitFor("return !!document.querySelector('.asr-reader-document h1')", 20000, "plain reader reload");
    await sleep(1800);
    const plainAfter = await evaluate(`
      const paragraphs = [...document.querySelectorAll(".asr-plain-transcript p")];
      const chosen = paragraphs.find((p) => Number(p.dataset.offset ?? -1) >= ${Number(plainSaved?.text_offset ?? 0)});
      const rect = chosen?.getBoundingClientRect();
      return { text: chosen?.textContent?.slice(0, 40) || null, visible: Boolean(rect && rect.bottom > 0 && rect.top < window.innerHeight), top: rect ? Math.round(rect.top) : null, viewport: window.innerHeight };
    `);
    record(
      "plain-text mobile scroll restores a character anchor",
      Boolean(plainSaved && plainSaved.text_offset > 0) && plainBefore.visible && plainAfter.visible,
      { saved: plainSaved, before: plainBefore, after: plainAfter },
    );
    await screenshot("05-plain-mobile-restored.png");

    // 5. Breakpoint change keeps the reading anchor visible.
    await setViewport(1280, 820, false);
    await navigate(BASE + "/");
    await openCard("复验分段记录");
    await evaluate(`
      const main = document.querySelector("main");
      if (main) main.scrollTop = 2400;
      window.scrollTo(0, 0);
      return main ? { top: main.scrollTop, scrollHeight: main.scrollHeight } : null;
    `);
    await sleep(2800);
    const desktopAnchor = await evaluate(`const d = await fetch("/api/history/${seeds.segmented_id}").then((r) => r.json()); return d.reader_position;`);
    await setViewport(390, 844, true);
    await sleep(1800);
    const afterBreakpoint = await anchorVisibility(desktopAnchor?.segment_index ?? 0);
    record(
      "breakpoint change keeps the saved segment in view",
      Boolean(desktopAnchor && desktopAnchor.segment_index > 0) && afterBreakpoint.visible,
      { saved: desktopAnchor, afterBreakpoint },
    );
    await screenshot("06-mobile-after-breakpoint.png");

    record("no unhandled browser exceptions", cdp.browserEvents.length === 0, cdp.browserEvents);

    results.pass = results.checks.every((check) => check.pass);
    fs.writeFileSync(path.join(OUT, "browser-check.json"), JSON.stringify(results, null, 2), "utf8");
    console.log(results.pass ? "ALL BROWSER CHECKS PASSED" : "BROWSER CHECKS FAILED");
    if (!results.pass) process.exitCode = 1;
  } finally {
    try {
      await fetch(`http://127.0.0.1:9223/json/close/${targetId}`);
    } catch {
      // best effort
    }
    cdp.ws.close();
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
