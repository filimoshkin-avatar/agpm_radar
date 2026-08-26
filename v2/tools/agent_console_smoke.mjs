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
    this.classList = { add() {}, remove() {}, toggle() {}, contains: () => false };
  }

  addEventListener(event, handler) { this.handler = handler; }
  append(child) { this.children = [...(this.children || []), child]; }
  appendChild(child) { this.append(child); }
  closest() { return null; }
  focus() {}
  insertAdjacentHTML(_where, html) { this.innerHTML += html; }
  scrollIntoView() {}
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  toggleAttribute(name, force) { this[name] = Boolean(force); }
  remove() {}
  /* The conversation builds its conveyor with document.createElement and then
     asks the node for its steps; a string of HTML cannot be parsed here, so a
     step selector hands back a stub, and the list of steps comes back empty -
     the conveyor advances over nothing, which is what a one-step turn does. */
  querySelector(selector) {
    if (selector.startsWith("[data-step")) return new FakeElement();
    return null;
  }
  querySelectorAll() { return []; }
  get firstElementChild() {
    if (!this._first) this._first = new FakeElement();
    return this._first;
  }
}

const ids = [
  "activeFilter", "agentThread", "agentWelcome", "promptGrid", "poolNote", "morePrompts",
  "newDialog", "agentMic", "agentAsk", "agentForm", "agentGaps", "agentObservatory",
  "agentSend", "agentChars", "chatHistoryToggle", "chatHistoryCount", "chatHistoryPos", "chatHistoryList", "chatDown", "chatDownCount", "chatUp", "chatMicLive",
  "agentObservatoryBody", "agentObservatoryFilters", "agentContradictions", "agentFind",
  "agentGraph", "agentGraphBody", "agentGraphTopic", "agentGraphLegend",
  "agentFindForm", "agentFindQuery", "agentFindResults", "agentFindFilters", "agentFindTopic",
  "agentGraphCanvas", "agentGraphMeta", "agentGraphEdge", "agentGraphEntity",
  "agentPage", "agentQuestion", "agentTopicCard", "agentTopics", "agentView", "agentWiki",
  "columns", "cut", "dailyAnalysis", "dailyAnalysisBody", "dailyAnalysisHeadline", "farChip",
  "footerSources", "gazetteView", "heatmap", "included", "includedShare", "issueDate",
  "midChip", "nearChip", "nearShare", "perimeters", "printGazetteTop", "radarTitle", "radarViz",
  "resetFilters", "rubricator", "rubrics", "search", "sparkline", "theses",
  "thesesTitle", "trendBars", "trendRange", "viewed",
];
const elements = new Map(ids.map(id => [id, new FakeElement()]));
const unhandled = [];
const requests = [];
const sentBodies = new Map();
const documentHandlers = new Map();
process.on("unhandledRejection", error => unhandled.push(error));

globalThis.HTMLElement = FakeElement;

// Attribute selectors, because the find and observatory controls are addressed
// that way rather than by id. A registry the test fills, so a selector nobody
// registered returns nothing instead of quietly matching everything.
const bySelector = new Map();
const register = (selector, element) => {
  bySelector.set(selector, element);
  return element;
};

globalThis.CSS = { escape: value => String(value) };
globalThis.document = {
  body: new FakeElement("body"),
  addEventListener(event, handler) { documentHandlers.set(event, handler); },
  createElement(tag) { return new FakeElement(tag); },
  getElementById(id) { return elements.get(id) || null; },
  querySelector(selector) {
    if (selector.startsWith("#")) return elements.get(selector.slice(1)) || null;
    return bySelector.get(selector) || null;
  },
  querySelectorAll(selector) {
    const matches = [];
    for (const [key, element] of bySelector) {
      if (key.startsWith(selector.replace(/\]$/, "")) || key === selector) matches.push(element);
    }
    return matches;
  },
};
// Answers nothing, on purpose. This used to answer «agent» to `agpmRadarViewMode`
// further down the file, because `initViewMode()` restored the stored mode during
// module evaluation and, for that mode alone, `setViewMode` reached `agentState` -
// a throw there blanked the page in every mode and shipped twice past a green
// gate. A reload now always opens «Радар»: the stored mode is gone, and the
// import-time hazard that key guarded has no code path left to guard. A stub that
// forces a branch nobody can reach reads as coverage and is not.
//
// Still stubbed, because three other things read it: the access key, the rail
// width, the saved conversation. The branch none of this covers is a *live*
// access key at import - answering null keeps the module on the unsubscribed path.
globalThis.localStorage = { getItem() { return null; }, setItem() {}, removeItem() {} };
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

const LINKS = [
  { link_type: "contradicts", direction: "outgoing", claim_id: "c2", statement: "порога не существует" },
  { link_type: "qualifies", direction: "incoming", claim_id: "c3", statement: "только выше суммы" },
];

const GRAPH = {
  schemaVersion: "2.0",
  centre: "statement:c1",
  nodes: [
    { id: "statement:c1", kind: "statement", key: "c1", label: "порог автономии определяет границу" },
    { id: "statement:c2", kind: "statement", key: "c2", label: "порога не существует" },
    { id: "topic:porogi", kind: "topic", key: "porogi", label: "Пороги автономии", hiddenNeighborCount: 17 },
    { id: "entity:e1", kind: "entity", key: "e1", label: "Gartner" },
  ],
  edges: [
    {
      from: "statement:c1", to: "statement:c2", relation: "contradicts",
      layer: "authorial", method: "model", reviewStatus: "unreviewed",
      explanation: "Модель определила утверждения как противоречащие"
    },
    { from: "statement:c1", to: "topic:porogi", relation: "about", layer: "structural",
      explanation: "Утверждение отнесено к теме скелета" },
    { from: "statement:c1", to: "entity:e1", relation: "mentions", layer: "structural",
      explanation: "Сущность названа в утверждении" },
  ],
  meta: {
    totalNeighborCount: 19, returnedNeighborCount: 3, hiddenNodeCount: 16,
    truncated: true, selectionPolicy: "most-recent-knowledge"
  },
};

const CONTRADICTIONS = {
  total: 283,
  pairs: [{
    from_id: "c1", to_id: "c2",
    first_statement: "порог автономии определяет границу",
    first_quote: "Порог автономии определяет границу между классами решений.",
    first_char_start: 100, first_char_end: 158,
    first_source_url: "https://example.org/a", first_source_title: "Пороги",
    first_material_kind: "fact", first_status: "canon",
    first_shown_on: "2026-06-01", first_shown_kind: "published",
    first_primary_source: "Gartner", first_is_retelling: true, first_valid_until: null,
    second_statement: "порога не существует",
    second_quote: "Никакого порога автономии в практике не наблюдается.",
    second_char_start: 10, second_char_end: 61,
    second_source_url: "https://example.org/b", second_source_title: "Против порогов",
    second_material_kind: "opinion", second_status: "observed_signal",
    second_shown_on: "2026-07-02", second_shown_kind: "published",
    second_primary_source: "", second_is_retelling: false, second_valid_until: null,
  }],
};

const ANSWER = {
  question: "что такое порог автономии",
  answer: "Порог автономии — решение организации.",
  refusalReason: null,
  machineNotice: "Агентный ответ, не редакция базы.",
  signature: "AgPM Radar, агентная сборка",
  evidence: [
    ASK_EVIDENCE,
    { ...ASK_EVIDENCE, n: 2, claimId: "c2", quote: "Никакого порога автономии в практике не наблюдается.",
      sourceUrl: "https://example.org/b", sourceTitle: "Против порогов", charStart: 10, charEnd: 61,
      materialKind: "opinion", status: "observed_signal", isRetelling: false, shownOn: "2026-07-02" },
  ],
  clauses: [{ text: "Порог автономии — решение организации.", evidence: [1] }],
  stages: [
    { step: "search", done: true, hits: 2, cache: false },
    { step: "draft", done: true },
    { step: "verify", done: true, passes: true },
  ],
  session: "smoke",
  tool: "find",
  toolCards: [],
};

const PROMPTS = {
  prompts: [
    { text: "Чем подтверждённые положения отличаются от наблюдаемых сигналов?", category: "find", hint: "поиск с доказательствами" },
    { text: "Расскажи про «Пороги автономии»", category: "concept", hint: "карточка темы" },
  ],
  pool: 17,
  poolCurated: 14,
};

/* The conversation endpoint streams its conveyor; the stub emits the frames
   one read, exactly as the wire carries them. */
const sseBody = payload => {
  const frames = payload.stages
    .map(stage => `event: stage\ndata: ${JSON.stringify(stage)}\n\n`)
    .join("") + `event: result\ndata: ${JSON.stringify(payload)}\n\n`;
  const encoder = new TextEncoder();
  return {
    getReader() {
      let done = false;
      return {
        async read() {
          if (done) return { done: true, value: undefined };
          done = true;
          return { done: false, value: encoder.encode(frames) };
        },
      };
    },
  };
};

globalThis.fetch = async (raw, options) => {
  const path = String(raw).replace("https://radar.test", "");
  requests.push(path);
  // The path alone cannot show a filter that was silently dropped: the shelf the
  // reader picked travels in the body, and that is where it went wrong before.
  if (options?.body) sentBodies.set(path, JSON.parse(options.body));
  if (path === "/kb/chat/stream") {
    return { ok: true, status: 200, body: sseBody(ANSWER) };
  }
  if (path === "/kb/chat") {
    return { ok: true, status: 200, async json() { return ANSWER; } };
  }
  if (path === "/kb/prompts") {
    return { ok: true, status: 200, async json() { return PROMPTS; } };
  }
  let payload = ISSUE;
  if (path.startsWith("/kb/observatory")) payload = { observatory: [{ ...STATEMENT, material_kind: "incident" }] };
  else if (path.startsWith("/kb/search")) payload = { hits: [{ ...STATEMENT, valid_until: "2020-01-01T00:00:00+00:00" }] };
  else if (path.startsWith("/kb/contradictions")) payload = CONTRADICTIONS;
  else if (path.startsWith("/kb/graph")) payload = GRAPH;
  else if (path.startsWith("/kb/statement/")) payload = { ...STATEMENT, links: LINKS };
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

// The module has to finish evaluating. Everything below asserts on behaviour,
// and behaviour cannot tell «this branch did nothing» from «the module died
// half-way and no listener past that line exists».
//
// The proof used to be the restored «agent» mode, and it proved less than it
// looked: `initViewMode()` runs near the top of the file, so a module that died
// anywhere below it still passed. A reload now always opens «Радар» and that
// proof is gone, so the check moved to the end - the change listener below is
// the last statement in `app.mjs`, and a handler on it means evaluation reached
// the final line.
if (typeof elements.get("agentGraphEntity").handler !== "function")
  fail("module evaluation aborted - the last listener in app.mjs never registered");
// And the mode a reload lands in is the contract now, not a leftover of whatever
// the reader had open last.
if (elements.get("agentView").hidden !== true)
  fail("a reload did not start on «Радар»");
// `closest` has to answer per selector, not "yes" to everything. The page asks
// twice on every click - once for a graph node, once for a button - and a stub
// that hands the button back both times sends every click down the wrong branch.
const click = button => documentHandlers.get("click")({
  target: {
    closest(selector) {
      if (selector === "button") return button;
      if (selector === "[data-graph-node]") return button.dataset?.graphNode ? button : null;
      return null;
    },
  },
});

const clickGraphNode = value => click({ dataset: { graphNode: value }, classList: { toggle() {} }, setAttribute() {} });

// The reader opens the agent mode.
click({ dataset: { viewMode: "agent" }, classList: { toggle() {} }, setAttribute() {} });
if (elements.get("agentView").hidden !== false) fail("the agent view stayed hidden");
if (elements.get("gazetteView").hidden !== true) fail("the gazette view did not step aside");

// ...and asks a question.
elements.get("agentQuestion").value = "что такое порог автономии";
await elements.get("agentForm").handler({ preventDefault() {} });
await settle();

const answered = elements.get("agentThread").innerHTML;
if (!requests.includes("/kb/chat/stream")) fail("the question never reached the base");
if (!answered.includes("Агентный ответ")) fail("an answer rendered without the owner's notice");
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
// The answer carries its own shape: a graph sketch button, and a subscribe
// teaser that says plainly what it costs rather than pretending to work.
if (!answered.includes("data-graph-mini")) fail("an answer with evidence offers no graph sketch");
if (!answered.includes("доступна по подписке")) fail("the subscribe teaser does not say what it costs");
if (!answered.includes("Пороги автономии")) fail("the answer's topics row is missing");

// ── The «искать в:» chips ───────────────────────────────────────────────────
//
// Both are lit on load, and both being lit has to reach the base as «all»
// (ADR-0012). This is the assertion the bug needed: the front end sent `""`,
// `_answer_flow` turned anything unnamed into knowledge - quietly and correctly -
// and the lit «хронике рынка» chip did nothing at all. The answer came back
// looking exactly like an answer, so nothing above this line could have noticed.
const askedWith = sentBodies.get("/kb/chat/stream");
if (askedWith?.admission !== "all")
  fail(`both chips lit went to the base as «${askedWith?.admission}», not «all»`);

const chips = {
  knowledge: register('[data-agent-admission="knowledge"]', new FakeElement("button")),
  observatory: register('[data-agent-admission="observatory"]', new FakeElement("button")),
};
for (const [name, chip] of Object.entries(chips)) chip.dataset.agentAdmission = name;
const pressed = name => chips[name].attributes.get("aria-pressed");

// One shelf off: the other is what the question is aimed at, and the chip says
// so out loud as well as in colour.
click({ dataset: { agentAdmission: "observatory" }, classList: { toggle() {} }, setAttribute() {} });
if (pressed("observatory") !== "false") fail("an unset chip still reads as pressed");
if (pressed("knowledge") !== "true") fail("the remaining chip stopped reading as pressed");
elements.get("agentQuestion").value = "второй вопрос, одна полка";
await elements.get("agentForm").handler({ preventDefault() {} });
await settle();
if (sentBodies.get("/kb/chat/stream")?.admission !== "knowledge")
  fail("one lit chip did not reach the base as its own name");

// The last one does not come off: «искать нигде» is not a question.
click({ dataset: { agentAdmission: "knowledge" }, classList: { toggle() {} }, setAttribute() {} });
if (pressed("knowledge") !== "true") fail("the last chip came off and left an empty search");

// Back to both, so everything below runs against the state a reader loads into.
click({ dataset: { agentAdmission: "observatory" }, classList: { toggle() {} }, setAttribute() {} });
if (pressed("observatory") !== "true") fail("a chip switched back on did not say so");

// The graph opens on whichever subject the picker holds.
elements.get("agentGraphTopic").value = "porogi";

// Every tab fetches its own data, and only when it is opened. The Links tab
// renders the neighbourhood as a real list first: this smoke runs without the
// vendored Cytoscape, and the list - not a canvas - must be the complete
// interface there.
for (const [tab, path, marker] of [
  ["observatory", "/kb/observatory", "инцидент"],
  ["graph", "/kb/graph?topic=porogi&limit=40", "agent-links__row"],
  ["contradictions", "/kb/contradictions?limit=40", "порога не существует"],
  ["topics", "/kb/topics", "Пороги автономии"],
  ["wiki", "/kb/pages", "Страница"],
  ["gaps", "/kb/gaps?limit=60", "нет темы про страхование"],
]) {
  click({ dataset: { agentTab: tab }, classList: { toggle() {} }, setAttribute() {} });
  await settle();
  if (!requests.includes(path)) fail(`the ${tab} tab did not fetch ${path}`);
  const panel = elements.get({
    observatory: "agentObservatoryBody", graph: "agentGraphBody",
    contradictions: "agentContradictions",
    topics: "agentTopics", wiki: "agentWiki", gaps: "agentGaps",
  }[tab]);
  if (!panel.innerHTML.includes(marker)) fail(`the ${tab} tab rendered nothing recognisable`);
}

// The Links tab says how much of the neighbourhood it is holding back, and
// which lines are the machine's unconfirmed suggestions.
const linksMeta = elements.get("agentGraphMeta").textContent;
if (!linksMeta.includes("из 19")) fail("the reader is not told how much of the neighbourhood is shown");
if (!elements.get("agentGraphBody").innerHTML.includes("предложила машина"))
  fail("an authorial edge was drawn without its machine provenance");

// An entity node routes to `entity=`: it used to land on `claim=` and draw an
// empty picture for a name that is not a claim.
clickGraphNode("entity:e1");
await settle();
if (!requests.includes("/kb/graph?entity=e1&limit=40"))
  fail("an entity node did not route through entity=");

// The canvas is the other half of the promise. Without a stub it never runs at
// all - so «the list is complete» was the only thing this smoke ever proved.
// With one, the drawing must build from the same neighbourhood the list shows,
// explain a tapped edge in its own line, walk the same routes, and tear itself
// down when the reader leaves.
let drawnWith = null;
let destroyed = 0;
const taps = {};
globalThis.cytoscape = options => {
  drawnWith = options;
  return {
    on(event, selector, handler) { taps[`${event}:${selector}`] = handler; },
    destroy() { destroyed += 1; },
  };
};
clickGraphNode("statement:c2");
await settle();
if (!drawnWith) fail("the canvas never drew even with Cytoscape present");
const drawnNodes = drawnWith.elements.filter(element => !element.data.source);
const drawnEdges = drawnWith.elements.filter(element => element.data.source);
if (drawnNodes.length !== GRAPH.nodes.length)
  fail("the canvas drew a different set of nodes than the list");
if (drawnEdges.length !== GRAPH.edges.length)
  fail("the canvas dropped edges the list shows");
if (elements.get("agentGraphCanvas").hidden !== false)
  fail("the canvas drew but stayed hidden");

// An edge explains itself in its own line. The meta line carries how much of
// the neighbourhood is held back, and a tap must never overwrite that.
const metaBeforeTap = elements.get("agentGraphMeta").textContent;
taps["tap:edge"]({
  target: {
    data: key => (key === "explanation"
      ? "Модель определила утверждения как противоречащие; владелец базы это не подтверждал"
      : "contradicts"),
  },
});
if (!elements.get("agentGraphEdge").textContent.includes("не подтверждал"))
  fail("a tapped edge did not say the machine proposed it and nobody confirmed it");
if (elements.get("agentGraphMeta").textContent !== metaBeforeTap)
  fail("a tapped edge overwrote how much of the neighbourhood is shown");

// A tapped node walks the same route a list button walks.
const beforeTapRequests = requests.length;
taps["tap:node"]({ target: { id: () => "entity:e1" } });
await settle();
if (!requests.slice(beforeTapRequests).includes("/kb/graph?entity=e1&limit=40"))
  fail("a canvas node tap did not route through entity=");

// Leaving the tab tears the canvas down: a hidden canvas keeps its listeners.
// Counted as an increment, not as a total - re-rendering also destroys, and a
// bare `if (destroyed)` would pass with the tab teardown deleted entirely.
const destroyedBeforeLeaving = destroyed;
click({ dataset: { agentTab: "topics" }, classList: { toggle() {} }, setAttribute() {} });
await settle();
if (destroyed === destroyedBeforeLeaving)
  fail("the canvas survived the reader leaving the tab");
delete globalThis.cytoscape;

// UC-11: a contradiction is a pair, and half of one is not the finding.
const clash = elements.get("agentContradictions").innerHTML;
if (!clash.includes("порог автономии определяет границу")) fail("the first side of a clash is missing");
if (!clash.includes("Никакого порога автономии")) fail("the second side of a clash is missing");
if (!clash.includes("283")) fail("the reader is not told how many disagreements the base holds");

// UC-01: finding costs the reader nothing, and says which arm found each hit.
// `/ask` and the conversation endpoints reach a paid model behind a limit; this
// must not.
//
// Counted as a difference across the search, not as a running total. The total
// was "exactly one so far", which is a fact about every line above this one:
// asking one more question anywhere earlier turned this into a failure about
// finding, which is not what it is about.
const modelCall = path => path === "/kb/ask" || path.startsWith("/kb/chat");
const paidBefore = requests.filter(modelCall).length;
const findQuery = register('[data-find-filter="material_kind"]', new FakeElement("select"));
findQuery.dataset.findFilter = "material_kind";
findQuery.value = "fact";
elements.get("agentFindQuery").value = "порог автономии";
await elements.get("agentFindForm").handler({ preventDefault() {} });
await settle();
const found = elements.get("agentFindResults").innerHTML;
const searched = requests.find(path => path.startsWith("/kb/search"));
if (!searched) fail("the find tab never reached the base");
if (!searched.includes("material_kind=fact")) fail("the find tab dropped its filter");
if (requests.filter(modelCall).length !== paidBefore)
  fail("finding must not spend a model call");
if (!found.includes("по словам") || !found.includes("по смыслу"))
  fail("a hit rendered without saying which arm found it");
if (!found.includes("знаки 100–158")) fail("a hit rendered without its character range");

// Decision 11 on screen: an expiry is a review due, and the reader sees the date.
if (!found.includes("срок истёк 2020-01-01")) fail("an expired statement rendered as fresh");

// Level five: what the base linked to this, on demand and not before.
const before = requests.filter(path => path.startsWith("/kb/statement/")).length;
if (before) fail("links were fetched before anybody asked for them");
const linksBox = register('[data-agent-links-for="c1"]', new FakeElement());
linksBox.hidden = true;
click({ dataset: { agentLinks: "c1" }, classList: { toggle() {} }, setAttribute() {} });
await settle();
if (!requests.some(path => path.startsWith("/kb/statement/c1"))) fail("the links never loaded");
if (!linksBox.innerHTML.includes("противоречит")) fail("a contradiction is not named in the card");
// "второе уточняет первое": an incoming `qualifies` means this one qualifies the other.
if (!linksBox.innerHTML.includes("уточняет")) fail("the qualifying link lost its direction");

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
const observatory = elements.get("agentObservatoryBody");
observatory.dataset.loaded = "";
observatory.innerHTML = "";
click({ dataset: { agentTab: "observatory" }, classList: { toggle() {} }, setAttribute() {} });
await settle();
for (const marker of ["знаки 100–158", "канон", "пересказ → Gartner", "дата публикации"]) {
  if (!observatory.innerHTML.includes(marker)) fail(`a snake_case row lost "${marker}"`);
}

// A tool card is the base's own data, so it must reach the reader in the shape
// the base sends it: the pair fields are `first_`/`second_`, and a card that
// reads any other name renders two empty columns and says nothing.
globalThis.fetch = async raw => {
  requests.push(String(raw));
  return {
    ok: true,
    status: 200,
    async json() {
      return {
        ...ANSWER,
        tool: "contra",
        toolCards: [{ type: "contradictions", data: CONTRADICTIONS }],
      };
    },
  };
};
elements.get("agentQuestion").value = "где база видит разногласия?";
await elements.get("agentForm").handler({ preventDefault() {} });
await settle();
const carded = elements.get("agentThread").innerHTML;
if (!carded.includes("порог автономии определяет границу"))
  fail("a contradiction card lost the first side");
if (!carded.includes("порога не существует"))
  fail("a contradiction card lost the second side");

// A refusal is still an answer with a notice, and never a blank panel.
globalThis.fetch = async raw => {
  requests.push(String(raw));
  return {
    ok: true,
    status: 200,
    async json() {
      return { ...ANSWER, answer: null, refusalReason: "no_evidence", evidence: [], clauses: [] };
    },
  };
};
elements.get("agentQuestion").value = "вопрос, на который нет ответа";
await elements.get("agentForm").handler({ preventDefault() {} });
await settle();
const refused = elements.get("agentThread").innerHTML;
if (!refused.includes("Агентный ответ")) fail("a refusal rendered without the notice");
if (!refused.includes("нет подтверждений")) fail("a refusal did not say what it was");

if (unhandled.length) fail(unhandled.map(String).join("; "));

process.stdout.write(
  "Agent view console smoke: PASS (answer, labels, find, graph, contradictions, links, expiry, eight tabs, refusal)\n"
);
