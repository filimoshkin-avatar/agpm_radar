"use strict";

import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const web = new URL("../apps/web/", import.meta.url);
const source = fs.readFileSync(new URL("app.mjs", web), "utf8");
const html = fs.readFileSync(new URL("index.html", web), "utf8");
const searchCases = JSON.parse(fs.readFileSync(new URL("../fixtures/synthetic/search-matching.json", import.meta.url)));

class Element {
  constructor() {
    this.innerHTML = "";
    this.textContent = "";
    this.hidden = false;
    this.value = "";
    this.dataset = {};
    this.style = {};
    this.attributes = new Map();
    this.handlers = new Map();
    const classes = new Set();
    this.classList = {
      add: name => classes.add(name),
      remove: name => classes.delete(name),
      contains: name => classes.has(name),
      toggle(name, on = !classes.has(name)) { if (on) classes.add(name); else classes.delete(name); },
    };
  }
  addEventListener(type, handler) { this.handlers.set(type, handler); }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  getAttribute(name) { return this.attributes.get(name) ?? null; }
  toggleAttribute(name, on) { if (name === "hidden") this.hidden = on; }
  querySelector(selector) {
    if (selector === ".api-error__wait") return this.wait || (this.wait = new Element());
    return null;
  }
  querySelectorAll() { return []; }
  insertAdjacentHTML(_where, text) { this.innerHTML += text; }
  insertAdjacentElement(_where, element) { this.banner = element; }
  remove() { this.removed = true; }
}

const stats = (included, viewed) => ({ included, viewed, cut: viewed - included, near: included, mid: 0, far: 0, core: included, adjacent: 0 });
const material = (title, id) => ({ ...searchCases.card, title, id, issueDate: "2026-09-05" });
const latest = {
  issueDate: "2026-09-05", issueNumber: 90,
  materials: [material("Текущий выпуск", "current")],
  stats: stats(1, 100), theses: [],
};
const historical = {
  issueDate: "2026-07-01", issueNumber: 20,
  materials: [material("Исторический выпуск", "old")],
  stats: stats(1, 40), theses: [],
};

function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
}
const turn = () => new Promise(resolve => setImmediate(resolve));

async function harness({ bootFailure = false } = {}) {
  const elements = new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(match => [match[1], new Element()]));
  const documentHandlers = new Map();
  const timers = new Map();
  let timerId = 0;
  const errors = [];
  const requests = [];
  let override = null;
  const body = new Element();
  const response = (payload, status = 200) => ({ ok: status === 200, status, json: async () => payload });
  const context = vm.createContext({
    URL, URLSearchParams, Headers, TextDecoder, AbortController,
    HTMLElement: Element,
    console: { warn() {}, error: (...args) => errors.push(args), log() {} },
    setTimeout(callback, delay) { const id = ++timerId; timers.set(id, { callback, delay }); return id; },
    clearTimeout(id) { timers.delete(id); },
    setInterval() { return 0; }, clearInterval() {}, queueMicrotask,
    localStorage: { getItem() { return null; }, setItem() {} },
    document: {
      body,
      createElement() { return new Element(); },
      getElementById: id => elements.get(id) || null,
      querySelector: selector => selector === ".api-error"
        ? (body.banner?.removed ? null : body.banner || null)
        : selector.startsWith("#") ? elements.get(selector.slice(1)) || null : null,
      querySelectorAll: () => [],
      addEventListener(type, handler) { documentHandlers.set(type, handler); },
    },
    window: {
      location: { hostname: "radar.test", origin: "https://radar.test", pathname: "/", search: "", port: "" },
      matchMedia: () => ({ matches: true }), addEventListener() {}, scrollTo() {},
      history: { replaceState() {}, pushState() {} },
    },
    async fetch(url) {
      const path = new URL(url, "https://radar.test").pathname;
      requests.push(String(url));
      const custom = override?.(new URL(url, "https://radar.test"));
      if (custom) return custom;
      if (path === "/api/latest") return bootFailure ? response({}, 503) : response(latest);
      if (path.startsWith("/api/issues/")) return response(historical);
      if (path === "/api/timeseries") return response({ items: [{ date: latest.issueDate, ...latest.stats }] });
      if (path === "/api/issues") return response({ items: [] });
      if (path === "/api/stats") return response(stats(1, 200));
      if (path === "/api/materials" || path === "/api/search") return response({ items: [material("Материал периода", "period")], nextCursor: null });
      if (path === "/api/rubrics" || path === "/api/sources") return response([]);
      return response({});
    },
  });
  context.window.setTimeout = context.setTimeout;
  vm.runInContext(source, context, { filename: "app.mjs" });
  await turn();
  await turn();
  const run = expression => vm.runInContext(expression, context);
  assert.equal(run("radarHasResults"), !bootFailure);
  assert.equal(errors.length, 0);
  return {
    elements, timers, requests, run, response, body,
    override(handler) { override = handler; },
    clickPeriod(period) {
      const button = { dataset: { period }, classList: { toggle() {} } };
      documentHandlers.get("click")({ target: { closest: () => button } });
    },
    async fireTimer() {
      assert.equal(timers.size, 1, "exactly one retry or debounce is pending");
      const [id, timer] = [...timers][0];
      timers.delete(id);
      await timer.callback();
      await turn();
    },
  };
}

// A deep historical issue outside the series keeps its own viewed/cut counts,
// including after the independently loading series finishes.
{
  const h = await harness();
  await h.run('state.issueDate = "2026-07-01"; reload()');
  assert.equal(h.elements.get("viewed").textContent, 40);
  assert.equal(h.elements.get("cut").textContent, 39);
  assert.equal(h.elements.get("footerMethodLabel").textContent, "МЕТОДИКА ВЫПУСКА 20");
  await h.run("loadTimeseriesData()");
  assert.equal(h.elements.get("viewed").textContent, 40);
  assert.match(h.elements.get("columns").innerHTML, /Исторический выпуск/);
}

// The initial /api/latest failure also has a finite retry budget. After it is
// spent, the visible manual action starts a fresh attempt and restores the page.
{
  const h = await harness({ bootFailure: true });
  for (const expectedDelay of [2000, 5000, 15000]) {
    assert.equal([...h.timers.values()][0].delay, expectedDelay);
    await h.fireTimer();
  }
  assert.equal(h.requests.filter(url => url.includes("/api/latest")).length, 4);
  assert.equal(h.timers.size, 0);
  assert.equal(h.elements.get("columns").innerHTML.includes("skeleton"), false);
  assert.match(h.body.banner.wait.textContent, /попытки закончились/);
  h.override(url => url.pathname === "/api/latest" ? h.response(latest) : null);
  h.body.banner.handlers.get("click")({ target: { closest: selector => selector === "[data-api-retry]" } });
  await turn();
  assert.match(h.elements.get("columns").innerHTML, /Текущий выпуск/);
  assert.equal(h.body.banner.removed, true);
}

// Identical independent examples exercise both the browser and Python repository.
{
  const h = await harness();
  h.run(`state.materials = [legacyMaterial(${JSON.stringify(searchCases.card)})]`);
  for (const sample of searchCases.queries) {
    h.run(`state.q = ${JSON.stringify(sample.q)}`);
    assert.equal(h.run("materialMatches(state.materials[0])"), sample.matches, sample.q);
  }
}

// An actual period click fails: retain the old cards AND summary, expose retry,
// recover automatically, and do not let secondary data redraw the failed choice.
for (const status of [429, 503, "network"]) {
  const h = await harness();
  const previous = h.elements.get("columns").innerHTML;
  let failures = 1;
  h.override(url => {
    if (url.pathname === "/api/materials" && failures-- > 0) {
      return status === "network" ? Promise.reject(new Error("offline")) : h.response({}, status);
    }
    return null;
  });
  h.clickPeriod("7d");
  await turn();
  assert.equal(h.elements.get("columns").innerHTML, previous);
  assert.equal(h.elements.get("columns").classList.contains("loading"), false);
  assert.match(h.elements.get("radarLoadMessage").textContent, /показаны предыдущие/);
  assert.equal(h.elements.get("radarLoadNotice").hidden, false);
  await h.run("loadTimeseriesData()");
  assert.equal(h.elements.get("viewed").textContent, 100);
  await h.fireTimer();
  assert.match(h.elements.get("columns").innerHTML, /Материал периода/);
  assert.equal(h.elements.get("viewed").textContent, 200);
  assert.equal(h.elements.get("radarLoadNotice").hidden, true);
  assert.equal(h.timers.size, 0);
}

// Three automatic retries, then an explicit manual retry can still recover.
{
  const h = await harness();
  h.override(url => url.pathname === "/api/materials" ? h.response({}, 503) : null);
  h.clickPeriod("30d");
  await turn();
  for (const expectedDelay of [2000, 5000, 15000]) {
    assert.equal([...h.timers.values()][0].delay, expectedDelay);
    await h.fireTimer();
  }
  assert.equal(h.timers.size, 0);
  assert.equal(h.requests.filter(url => url.includes("/api/materials")).length, 4);
  h.override(() => null);
  h.elements.get("radarLoadRetry").handlers.get("click")();
  await turn();
  assert.equal(h.elements.get("radarLoadNotice").hidden, true);
}

// A failed obsolete request cannot install an error, replace cards, or schedule
// retries after a newer selection has already succeeded.
{
  const h = await harness();
  const slow = deferred();
  h.override(url => url.pathname === "/api/materials" ? slow.promise : null);
  h.clickPeriod("30d");
  await turn();
  h.clickPeriod("issue");
  await turn();
  slow.resolve(h.response({}, 503));
  await turn();
  assert.match(h.elements.get("columns").innerHTML, /Текущий выпуск/);
  assert.equal(h.elements.get("radarLoadNotice").hidden, true);
  assert.equal(h.timers.size, 0);
}

// Typing cancels old retries immediately, before the search debounce elapses.
{
  const h = await harness();
  h.override(url => url.pathname === "/api/materials" ? h.response({}, 503) : null);
  h.clickPeriod("7d");
  await turn();
  h.elements.get("search").handlers.get("input")({ target: { value: "контроль" } });
  assert.equal([...h.timers.values()][0].delay, 180);
  assert.equal(h.timers.size, 1);
  await h.fireTimer();
  assert.equal(h.timers.size, 0);
}

// For the agent's search, late success, empty and error replies are all stale.
for (const obsolete of ["success", "empty", "error"]) {
  const h = await harness();
  const slow = deferred();
  h.override(url => {
    if (url.pathname !== "/kb/search") return null;
    return url.searchParams.get("q") === "first" ? slow.promise : h.response({ hits: [{ statement: "SECOND RESULT", claim_id: "second" }] });
  });
  h.elements.get("agentFindQuery").value = "first";
  const first = h.run("agentFind()");
  h.elements.get("agentFindQuery").value = "second";
  await h.run("agentFind()");
  const secondResult = h.elements.get("agentFindResults").innerHTML;
  assert.match(secondResult, /SECOND RESULT/);
  slow.resolve(h.response({ hits: obsolete === "empty" ? [] : [{ statement: "FIRST RESULT", claim_id: "first" }] }, obsolete === "error" ? 503 : 200));
  await first;
  assert.equal(h.elements.get("agentFindResults").innerHTML, secondResult);
}

console.log("Frontend recovery smoke: PASS (historical stats, search, retained results, bounded retries, stale replies)");
