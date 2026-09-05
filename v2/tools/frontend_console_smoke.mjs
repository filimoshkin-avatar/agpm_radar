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
  "printGazetteTop", "radarTitle", "radarViz", "resetFilters", "rubricator", "rubrics",
  "search", "sparkline", "theses", "thesesTitle", "trendBars",
  "trendRange", "viewed",
];
const elements = new Map(ids.map(id => [id, new FakeElement()]));
const unhandled = [];
const requests = [];
const documentHandlers = new Map();
process.on("unhandledRejection", error => unhandled.push(error));

// A reader arriving by link: the address names a past issue, and the page must
// open that issue - and write its canonical address back. Until 05.09.2026 the
// address was never read, and the daily notification's link opened the latest.
const deepLink = process.argv.includes("--deep-link");
// The already-sent form: every daily notification before 05.09.2026 links with
// `?date=`. It must open its issue and leave the canonical address behind it.
const LINKED_DATE = "2026-08-20";
const historyCalls = [];
const windowHandlers = new Map();
const fakeLocation = {
  hash: "",
  hostname: "radar.test",
  origin: "https://radar.test",
  pathname: "/",
  port: "",
  search: deepLink ? `?date=${LINKED_DATE}` : "",
};
const fakeHistory = {
  pushState(_state, _title, url) { historyCalls.push(["pushState", url]); },
  replaceState(_state, _title, url) { historyCalls.push(["replaceState", url]); },
};
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
  addEventListener(event, handler) { windowHandlers.set(event, handler); },
  history: fakeHistory,
  location: fakeLocation,
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
//
// Driven by running this smoke a second time with `--pre-contract` rather than
// by re-driving the loaded module: `app.mjs` caches the issue it has, so a
// second payload in one process is a race. A second process has no module
// cache and no race - and the branch is live code, serving every issue
// published before the contract, so it is not optional to cover.
const preContract = process.argv.includes("--pre-contract");
// Rubrics are a secondary panel, and their failure must not take the issue with
// them. The branch is live: /api/rubrics answers 503 when its anchor is past the
// edge of the archive.
const rubricsDown = process.argv.includes("--rubrics-down");
// A card whose LLM texts succeeded: the description slot must show the LLM's
// short text and «ВЫВОД ДЛЯ AgPM» its angle, and the rule-based summary and
// takeaway of that material must not be on the card at all. The fixtures of the
// other routes carry only `status: "fallback"`, so this is the one place the
// success branch of renderCard() runs under the gate.
const llmMaterial = {
  ...pageMaterial,
  agpmTakeaway: "Детерминированный вывод.",
  canonicalUrl: "https://example.test/llm",
  id: "llm-material",
  llm: { effectiveModel: "openai/gpt-5.5", status: "success" },
  llmAgpmAngle: "Вывод по фактам статьи.",
  llmShortText: "Факты из статьи про Rovo.",
  summary: "Детерминированное описание.",
  title: "Карточка с текстами модели",
  url: "https://example.test/llm",
};
const oldIssue = {
  ...issue,
  analysis: {
    blocks: [
      { kind: "overview", text: "Старый сигнал.", title: "Сигнал" },
      { kind: "actions", text: "Старое что-дальше.", title: "Что смотреть дальше" },
    ],
  },
  materials: [{ ...pageMaterial, keyMaterial: true, title: "Ключевой материал" }, llmMaterial],
  title: "Старый выпуск",
};

globalThis.fetch = async raw => {
  const path = String(raw).replace("https://radar.test", "");
  requests.push(path);
  let payload = preContract ? oldIssue : issue;
  if (path.startsWith("/api/timeseries")) payload = { items: [{ ...issue.stats, date: issue.issueDate }] };
  else if (path.startsWith("/api/rubrics")) {
    if (rubricsDown) return { ok: false, status: 503, async json() { return {}; } };
    payload = [];
  }
  else if (path.startsWith("/api/sources")) payload = [];
  else if (path.startsWith("/api/issues?") || path === "/api/issues") payload = { items: [], nextCursor: null };
  else if (path === `/api/issues/${LINKED_DATE}`) payload = { ...issue, issueDate: LINKED_DATE, title: "Выпуск по ссылке" };
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

if (deepLink) {
  if (!requests.includes(`/api/issues/${LINKED_DATE}`)) {
    throw new Error("the linked issue was never requested: the address was not read");
  }
  // `?date=` is answered and then replaced, not pushed: arriving somewhere is
  // not a step the back button should have to walk back through.
  if (JSON.stringify(historyCalls) !== JSON.stringify([["replaceState", `/issues/${LINKED_DATE}`]])) {
    throw new Error(`the canonical address was not written back once: ${JSON.stringify(historyCalls)}`);
  }
  const linkedLabel = elements.get("issueDate").textContent;
  // And back: the address returns to `/`, so the latest issue returns with it.
  fakeLocation.search = "";
  fakeLocation.pathname = "/";
  windowHandlers.get("popstate")?.({});
  await new Promise(resolve => setTimeout(resolve, 50));
  const backLabel = elements.get("issueDate").textContent;
  if (!windowHandlers.has("popstate")) {
    throw new Error("nothing listens for the back button");
  }
  if (backLabel === linkedLabel) {
    throw new Error(`the back button did not leave the linked issue: still "${backLabel}"`);
  }
  if (historyCalls.length !== 1) {
    throw new Error(`walking back wrote a new address: ${JSON.stringify(historyCalls)}`);
  }
  if (unhandled.length) {
    throw new Error(`deep-link smoke failed: ${unhandled.map(String).join("; ")}`);
  }
  process.stdout.write("Frontend console smoke: PASS (a link opens its own issue, back returns)\n");
  process.exit(0);
}
if (historyCalls.length) {
  throw new Error(`the latest issue must stay at / without writing an address: ${JSON.stringify(historyCalls)}`);
}

// The analysis block: the analysis's own headline (never the issue brief),
// «Что смотреть дальше» from the actions block, and the LLM's own evidence
// list, in the LLM's own order.
const dailyHeadline = elements.get("dailyAnalysisHeadline").textContent;
const dailyBody = elements.get("dailyAnalysisBody").innerHTML;
if (preContract) {
  // No analysis headline: the issue title stands in, and the issue brief still
  // must not - that substitution was the defect this branch removed.
  if (dailyHeadline !== "Старый выпуск") {
    throw new Error(`pre-contract headline is "${dailyHeadline}", expected the issue title`);
  }
  if (dailyHeadline === "Пустой выпуск.") {
    throw new Error("the issue brief stood in for the analysis headline");
  }
  // No evidenceTitles: the key-material list stands in.
  if (!dailyBody.includes("Ключевой материал")) {
    throw new Error("the key-material fallback did not render for a pre-contract issue");
  }
  if (!dailyBody.includes("Старое что-дальше.")) {
    throw new Error("the actions block did not render for a pre-contract issue");
  }
  // Both card branches on one page: the LLM card shows the model's texts and
  // none of its rule-based ones, the fallback card shows its rule-based ones.
  const cards = elements.get("columns").innerHTML;
  if (!cards.includes("<p>Факты из статьи про Rovo.</p>")
    || !cards.includes("ВЫВОД ДЛЯ AgPM · </b>Вывод по фактам статьи.")) {
    throw new Error("the LLM card did not render the model's short text and angle in their slots");
  }
  if (cards.includes("Детерминированное описание.") || cards.includes("Детерминированный вывод.")) {
    throw new Error("the LLM card still shows a rule-based text");
  }
  if (!cards.includes("<p>Материал второй страницы.</p>")
    || !cards.includes("ВЫВОД ДЛЯ AgPM · </b>Проверить пагинацию.")) {
    throw new Error("the fallback card did not render its rule-based summary and takeaway");
  }
  process.stdout.write("Frontend console smoke: PASS (pre-contract fallback, LLM and fallback cards)\n");
  process.exit(0);
}
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

process.stdout.write(rubricsDown
  ? "Frontend console smoke: PASS (rubrics unavailable, issue still renders)\n"
  : "Frontend console smoke: PASS (Legacy-parity empty/fallback route)\n");
