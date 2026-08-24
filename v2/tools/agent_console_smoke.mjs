"use strict";

/**
 * The agent view, driven the way a reader drives it.
 *
 * The radar's own smoke test proves the page boots; this one proves the third
 * position of the switcher does what the owner's decisions require: that a
 * generated answer never renders without its notice and its evidence, that every
 * label reaching the reader is a label the base put there, and that a tab fetches
 * its own data and nobody else's.
 *
 * Same shape as tools/frontend_console_smoke.mjs - a fake DOM and a stubbed
 * fetch - because the module is a browser script and there is no browser here.
 */

class FakeElement {
  constructor(tag = "div") {
    this.tagName = tag.toUpperCase();
    this.attributes = new Map();
    this.dataset = {};
    this.hidden = false;
    this.innerHTML = "";
    this.style = {};
    this.textContent = "";
    this.value = "";
    this.classList = { add() {}, remove() {}, toggle() {} };
  }

  addEventListener(event, handler) { this.handler = handler; }
  closest() { return null; }
  focus() {}
  insertAdjacentHTML(_where, html) { this.innerHTML += html; }
  scrollIntoView() {}
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  toggleAttribute(name, force) { this[name] = Boolean(force); }
}

const ids = [
  "activeFilter", "agentAnswer", "agentAsk", "agentForm", "agentGaps", "agentObservatory",
  "agentPage", "agentQuestion", "agentTopicCard", "agentTopics", "agentView", "agentWiki",
  "columns", "cut", "dailyAnalysis", "dailyAnalysisBody", "dailyAnalysisHeadline", "farChip",
  "footerSources", "gazetteView", "heatmap", "included", "includedShare", "issueDate",
  "midChip", "nearChip", "nearShare", "perimeters", "printGazette", "radarTitle", "radarViz",
  "resetFilters", "rubricator", "rubrics", "search", "sources", "sparkline", "theses",
  "thesesTitle", "timeline", "trendBars", "trendRange", "viewed",
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
  location: { hash: "", hostname: "radar.test", origin: "https://radar.test", port: "" },
  matchMedia() { return { matches: true }; },
  open() { return null; },
  addEventListener() {},
};
globalThis.requestAnimationFrame = callback => callback(0);

// Real timers on purpose. Stubbing setTimeout to fire synchronously never yields
// to the microtask queue, so an awaited fetch has not resolved when the assertion
// runs and every panel looks empty - which is a bug in the harness that reads
// exactly like a bug in the page.
const settle = () => new Promise(resolve => setTimeout(resolve, 25));

const ISSUE = {
  issueDate: "2026-08-21",
  stats: { viewed: 0, included: 0, cut: 0, near: 0, mid: 0, far: 0, core: 0, adjacent: 0 },
  items: [],
  rubrics: [],
  sources: [],
  theses: [],
};

const STATEMENT = {
  claim_id: "c1",
  statement: "порог автономии определяет границу классов решений",
  quote_text: "Порог автономии определяет границу между классами решений.",
  char_start: 100,
  char_end: 158,
  source_url: "https://example.org/a",
  source_title: "Пороги автономии",
  material_kind: "fact",
  status: "canon",
  primary_source: "Gartner",
  is_retelling: true,
  shown_on: "2026-06-01",
  shown_kind: "published",
  matched_by: ["слова", "смысл"],
};

// EXACTLY what `/kb/ask` returns: `EvidenceElement.as_json()` merged with
// `Labels.as_json()`, which is camelCase throughout. The first version of this
// fixture spread the snake_case database row instead, so the four assertions
// below passed while the live Ask tab was silently dropping every label, the
// date and the character range. A fixture in a shape the server never sends
// certifies the client against a server that does not exist.
const ASK_EVIDENCE = {
  n: 1,
  claimId: "c1",
  quote: "Порог автономии определяет границу между классами решений.",
  sourceUrl: "https://example.org/a",
  charStart: 100,
  charEnd: 158,
  relevance: 0.5,
  audience: "public",
  materialKind: "fact",
  admission: "knowledge",
  status: "canon",
  primarySource: "Gartner",
  isRetelling: true,
  shownOn: "2026-06-01",
  shownKind: "published",
  topics: ["Пороги автономии"],
  matchedBy: ["слова", "смысл"],
};

const ANSWER = {
  question: "что такое порог автономии",
  answer: "Порог автономии — решение организации.",
  refusalReason: null,
  machineNotice: "Машинный ответ, не редакция базы.",
  signature: "AgPM Radar, машинная сборка",
  evidence: [ASK_EVIDENCE],
};

globalThis.fetch = async (raw, options) => {
  const path = String(raw).replace("https://radar.test", "");
  requests.push(path);
  let payload = ISSUE;
  if (path === "/kb/ask") payload = ANSWER;
  else if (path === "/kb/observatory") payload = { observatory: [{ ...STATEMENT, material_kind: "incident" }] };
  else if (path === "/kb/topics") payload = { topics: [{ topic_key: "porogi", title: "Пороги автономии", path: "Управление / Пороги", statements: 12 }] };
  else if (path.startsWith("/kb/topics/")) payload = { title: "Пороги автономии", path: "Управление / Пороги", statements: [STATEMENT] };
  else if (path === "/kb/pages") payload = { pages: [{ relative_path: "wiki/a.md", title: "Страница", chars: 100 }] };
  else if (path.startsWith("/kb/pages/")) payload = { relative_path: "wiki/a.md", title: "Страница", body: "текст", signature: "автор методики — владелец базы" };
  else if (path.startsWith("/kb/gaps")) payload = { gaps: [{ claim_id: "c9", missing: "нет темы про страхование", statement: "полис покрывает ущерб" }] };
  else if (path.startsWith("/api/timeseries")) payload = { items: [] };
  else if (path.startsWith("/api/rubrics") || path.startsWith("/api/sources")) payload = [];
  else if (path.startsWith("/api/issues")) payload = { items: [], nextCursor: null };
  else if (path.startsWith("/api/materials") || path.startsWith("/api/search")) payload = { items: [], nextCursor: null };
  void options;
  return { ok: true, status: 200, async json() { return payload; } };
};

await import("../apps/web/app.mjs");
await settle();

const fail = message => { throw new Error(`agent console smoke failed: ${message}`); };
const click = button => documentHandlers.get("click")({ target: { closest() { return button; } } });

// The reader opens the agent mode.
click({ dataset: { viewMode: "agent" }, classList: { toggle() {} }, setAttribute() {} });
if (elements.get("agentView").hidden !== false) fail("the agent view stayed hidden");
if (elements.get("gazetteView").hidden !== true) fail("the gazette view did not step aside");

// ...and asks a question.
elements.get("agentQuestion").value = "что такое порог автономии";
await elements.get("agentForm").handler({ preventDefault() {} });
await settle();

const answered = elements.get("agentAnswer").innerHTML;
if (!requests.includes("/kb/ask")) fail("the question never reached the base");
if (!answered.includes("Машинный ответ")) fail("an answer rendered without the owner's notice");
if (!answered.includes("Порог автономии — решение организации")) fail("the answer text is missing");
if (!answered.includes("Порог автономии определяет границу между классами"))
  fail("an answer rendered without the quotation under it");
if (!answered.includes("знаки 100–158")) fail("the quotation rendered without its character range");
if (!answered.includes("канон")) fail("the status label is missing");
if (!answered.includes("пересказ → Gartner")) fail("the retelling label is missing");
if (!answered.includes("дата публикации")) fail("the reader is not told which date is shown");
if (!answered.includes("по словам") || !answered.includes("по смыслу"))
  fail("the reader is not told why the evidence was found");
if (!answered.includes("https://example.org/a")) fail("the source link is missing");

// Every tab fetches its own data, and only when it is opened.
for (const [tab, path, marker] of [
  ["observatory", "/kb/observatory", "инцидент"],
  ["topics", "/kb/topics", "Пороги автономии"],
  ["wiki", "/kb/pages", "Страница"],
  ["gaps", "/kb/gaps?limit=60", "нет темы про страхование"],
]) {
  click({ dataset: { agentTab: tab }, classList: { toggle() {} }, setAttribute() {} });
  await settle();
  if (!requests.includes(path)) fail(`the ${tab} tab did not fetch ${path}`);
  const panel = elements.get({ observatory: "agentObservatory", topics: "agentTopics", wiki: "agentWiki", gaps: "agentGaps" }[tab]);
  if (!panel.innerHTML.includes(marker)) fail(`the ${tab} tab rendered nothing recognisable`);
}

// The panels speak snake_case and the answer speaks camelCase. Both must render
// the same labels, which is why the client normalises at one boundary instead of
// falling back field by field - three fields got a fallback and the rest did not,
// and the Ask tab lost every label in production while this file stayed green.
globalThis.fetch = async raw => {
  requests.push(String(raw));
  return {
    ok: true,
    status: 200,
    async json() {
      return { observatory: [{ ...STATEMENT, material_kind: "incident" }] };
    },
  };
};
const observatory = elements.get("agentObservatory");
observatory.dataset.loaded = "";
observatory.innerHTML = "";
click({ dataset: { agentTab: "observatory" }, classList: { toggle() {} }, setAttribute() {} });
await settle();
for (const marker of ["знаки 100–158", "канон", "пересказ → Gartner", "дата публикации"]) {
  if (!observatory.innerHTML.includes(marker)) fail(`a snake_case row lost "${marker}"`);
}

// A refusal is still an answer with a notice, and never a blank panel.
globalThis.fetch = async raw => {
  requests.push(String(raw));
  return {
    ok: true,
    status: 200,
    async json() {
      return { ...ANSWER, answer: null, refusalReason: "no_evidence", evidence: [] };
    },
  };
};
elements.get("agentQuestion").value = "вопрос, на который нет ответа";
await elements.get("agentForm").handler({ preventDefault() {} });
await settle();
const refused = elements.get("agentAnswer").innerHTML;
if (!refused.includes("Машинный ответ")) fail("a refusal rendered without the notice");
if (!refused.includes("нет подтверждений")) fail("a refusal did not say what it was");

if (unhandled.length) fail(unhandled.map(String).join("; "));

process.stdout.write("Agent view console smoke: PASS (answer, labels, four tabs, refusal)\n");
