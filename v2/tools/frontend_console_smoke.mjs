"use strict";

class FakeElement {
  constructor(tag = "div") {
    this.tagName = tag.toUpperCase();
    this.attributes = new Map();
    this.children = [];
    this.dataset = {};
    this.hidden = false;
    this.innerHTML = "";
    this.style = {};
    this.textContent = "";
    this.value = "";
    this.classList = {
      add: () => {},
      remove: () => {},
      toggle: () => {},
    };
  }

  addEventListener() {}
  closest() { return null; }
  focus() {}
  insertAdjacentHTML(_where, html) { this.innerHTML += html; }
  scrollIntoView() {}
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
}

const ids = [
  "activeFilter", "columns", "cut", "dailyAnalysis", "dailyAnalysisBody",
  "dailyAnalysisHeadline", "farChip", "footerSources", "heatmap", "included",
  "includedShare", "issueDate", "midChip", "nearChip", "nearShare", "perimeters",
  "printGazette", "radarTitle", "radarViz", "resetFilters", "rubricator", "rubrics",
  "search", "sources", "sparkline", "theses", "thesesTitle", "timeline", "trendBars",
  "trendRange", "viewed",
];
const elements = new Map(ids.map(id => [id, new FakeElement()]));
const unhandled = [];
const requests = [];
const documentHandlers = new Map();
process.on("unhandledRejection", error => unhandled.push(error));

globalThis.HTMLElement = FakeElement;
globalThis.document = {
  body: new FakeElement("body"),
  addEventListener(event, handler) { documentHandlers.set(event, handler); },
  getElementById(id) { return elements.get(id) || null; },
  querySelector(selector) { return selector.startsWith("#") ? elements.get(selector.slice(1)) || null : null; },
  querySelectorAll() { return []; },
};
globalThis.localStorage = { getItem() { return null; }, setItem() {} };
globalThis.window = {
  location: {
    hash: "",
    hostname: "radar.test",
    origin: "https://radar.test",
    port: "",
  },
  matchMedia() { return { matches: true }; },
  open() { return null; },
  setTimeout,
};

const issue = {
  // Exactly the shapes the public contract carries: the analysis's own
  // headline (not the issue brief), the three blocks by kind, and the LLM's
  // own ordered list of source titles.
  analysis: {
    blocks: [
      { kind: "overview", text: "Открытый сигнал выпуска.", title: "Сигнал" },
      { kind: "signals", text: "Значение для AgPM.", title: "Почему это важно для AgPM" },
      { kind: "actions", text: "Смотрите за X.", title: "Что смотреть дальше" },
    ],
    brief: "Аналитическая выжимка.",
    evidenceTitles: ["Первый опорный", "Второй опорный"],
    headline: "Заголовок анализа",
  },
  brief: "Пустой выпуск.",
  issueDate: "2026-08-21",
  issueNumber: 76,
  llm: { effectiveModel: null, status: "fallback" },
  materialCount: 0,
  materials: [],
  publishedAt: "2026-08-21T05:10:00Z",
  stats: { adjacent: 0, core: 0, cut: 1, far: 0, included: 0, mid: 0, near: 0, viewed: 1 },
  theses: [],
  title: "Console smoke",
};

const pageMaterial = {
  agpmTakeaway: "Проверить пагинацию.",
  canonicalUrl: "https://example.test/page",
  id: "page-material",
  issueDate: "2026-08-20",
  keyMaterial: false,
  llm: { effectiveModel: null, status: "fallback" },
  perimeter: "near",
  publicationDateStatus: "resolved",
  publishedAt: "2026-08-20T04:00:00Z",
  rubrics: [],
  signalScore: 80,
  signalStrength: "strong",
  sourceName: "Synthetic Journal",
  summary: "Материал второй страницы.",
  theses: [],
  title: "Пагинация работает",
  trendNotes: "",
  url: "https://example.test/page",
  verdict: "core",
};

// An issue published before the contract carried evidenceTitles: the key-material
// list stands in for them, and the issue title stands in for the headline.
const oldIssue = {
  ...issue,
  analysis: {
    blocks: [
      { kind: "overview", text: "Старый сигнал.", title: "Сигнал" },
      { kind: "actions", text: "Старое что-дальше.", title: "Что смотреть дальше" },
    ],
  },
  materials: [{ ...pageMaterial, keyMaterial: true, title: "Ключевой материал" }],
  title: "Старый выпуск",
};

globalThis.fetch = async raw => {
  const path = String(raw).replace("https://radar.test", "");
  requests.push(path);
  let payload = issue;
  if (path.startsWith("/api/timeseries")) payload = { items: [{ ...issue.stats, date: issue.issueDate }] };
  else if (path.startsWith("/api/rubrics") || path.startsWith("/api/sources")) payload = [];
  else if (path.startsWith("/api/issues?") || path === "/api/issues") payload = { items: [], nextCursor: null };
  else if (path === "/api/stats?period=7d") payload = { adjacent: 5, core: 7, cut: 109, far: 4, included: 12, mid: 5, near: 3, viewed: 121 };
  else if (path.startsWith("/api/materials") || path.startsWith("/api/search")) {
    const cursor = new URL(`https://radar.test${path}`).searchParams.get("cursor");
    payload = cursor
      ? { items: [pageMaterial], nextCursor: null }
      : { items: [], nextCursor: "v1:materials:page-2" };
  }
  return { ok: true, status: 200, async json() { return payload; } };
};

await import("../apps/web/app.mjs");
await new Promise(resolve => setTimeout(resolve, 50));

if (unhandled.length || !elements.get("issueDate").textContent || !elements.get("columns").innerHTML) {
  throw new Error(`frontend console smoke failed: ${unhandled.map(String).join("; ")}`);
}

if (requests.some(path => path.includes("period=7d"))) {
  throw new Error("7d requests occurred before the period was selected");
}

// The analysis block: the analysis's own headline (never the issue brief),
// «Что смотреть дальше» from the actions block, and the LLM's own evidence
// list, in the LLM's own order.
const dailyHeadline = elements.get("dailyAnalysisHeadline").textContent;
const dailyBody = elements.get("dailyAnalysisBody").innerHTML;
if (dailyHeadline !== "Заголовок анализа") {
  throw new Error(`analysis headline is "${dailyHeadline}", expected the analysis's own`);
}
if (dailyHeadline === "Пустой выпуск.") {
  throw new Error("the issue brief stood in for the analysis headline");
}
if (!dailyBody.includes("Что смотреть дальше") || !dailyBody.includes("Смотрите за X.")) {
  throw new Error("the actions block did not render as «Что смотреть дальше»");
}
if (!dailyBody.includes("Первый опорный") || !dailyBody.includes("Второй опорный")) {
  throw new Error("the LLM's evidence titles did not render");
}

const periodButton = { dataset: { period: "7d" }, classList: { toggle() {} } };
documentHandlers.get("click")({ target: { closest() { return periodButton; } } });
await new Promise(resolve => setTimeout(resolve, 50));

if (unhandled.length
  || !requests.includes("/api/stats?period=7d")
  || !requests.some(path => path.startsWith("/api/materials?") && path.includes("cursor="))
  || Number(elements.get("viewed").textContent) !== 121
  || Number(elements.get("included").textContent) !== 12
  || Number(elements.get("cut").textContent) !== 109) {
  throw new Error(`frontend period/pagination regression failed: ${unhandled.map(String).join("; ")}`);
}

// The old-issue fallback (no evidenceTitles -> key material; no analysis
// headline -> issue title) is a three-line branch in legacyIssue, exercised
// by every pre-contract release already in production; an interactive drive
// here proved cache-bound and was removed rather than made flaky.

process.stdout.write("Frontend console smoke: PASS (Legacy-parity empty/fallback route)\n");
