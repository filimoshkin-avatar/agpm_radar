"use strict";

import fs from "node:fs";
import vm from "node:vm";
import { fileURLToPath } from "node:url";
import path from "node:path";

const repositoryRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
}

function waitTurn() {
  return new Promise(resolve => setTimeout(resolve, 20));
}

function countOccurrences(value, needle) {
  return String(value || "").split(needle).length - 1;
}

function legacyMaterial(id, perimeter = "near") {
  return {
    agpm_takeaway: "Проверить порядок ответов.",
    canonical_url: `https://example.test/${id}`,
    id,
    key_material: false,
    llm_summary: { agpm_angle: "", short_text: "", status: "fallback" },
    perimeter,
    publication_date_status: "resolved",
    published_at: "2026-08-22T04:00:00Z",
    radar_issue_date: "2026-08-22",
    rubrics: [],
    signal_score: 80,
    source_name: "Synthetic Journal",
    summary: `Материал ${id}`,
    title: `Материал ${id}`,
    trend_notes: "",
    url: `https://example.test/${id}`,
    verdict: "core",
  };
}

function v2Material(id, perimeter = "near") {
  return {
    agpmTakeaway: "Проверить порядок ответов.",
    canonicalUrl: `https://example.test/${id}`,
    id,
    issueDate: "2026-08-22",
    keyMaterial: false,
    llm: { effectiveModel: null, status: "fallback" },
    perimeter,
    publicationDateStatus: "resolved",
    publishedAt: "2026-08-22T04:00:00Z",
    rubrics: [],
    signalScore: 80,
    signalStrength: "strong",
    sourceName: "Synthetic Journal",
    summary: `Материал ${id}`,
    title: `Материал ${id}`,
    trendNotes: "",
    url: `https://example.test/${id}`,
    verdict: "core",
  };
}

function makeStats(included) {
  return {
    adjacent: 0,
    core: included,
    cut: 20 - included,
    far: 0,
    included,
    mid: 0,
    near: included,
    viewed: 20,
  };
}

async function runCase({ name, relativeScript, v2, sonarTitle }) {
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
        add() {},
        remove() {},
        toggle() {},
      };
    }

    addEventListener() {}
    closest() { return null; }
    focus() {}
    insertAdjacentHTML(_where, html) { this.innerHTML += html; }
    scrollIntoView() {}
    setAttribute(key, value) { this.attributes.set(key, String(value)); }
    toggleAttribute(key, force) {
      if (force) this.attributes.set(key, "");
      else this.attributes.delete(key);
    }
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
  const documentHandlers = new Map();
  const delayedPeriod = deferred();
  let delayedRequestSeen = false;
  const issueStats = makeStats(1);
  const periodStats = makeStats(3);
  const issueMaterials = v2 ? [v2Material("issue")] : [legacyMaterial("issue")];
  const periodMaterials = v2
    ? [v2Material("period-1"), v2Material("period-2"), v2Material("period-3")]
    : [legacyMaterial("period-1"), legacyMaterial("period-2"), legacyMaterial("period-3")];
  const issue = v2 ? {
    analysis: { blocks: [] },
    brief: "Текущий выпуск.",
    issueDate: "2026-08-22",
    issueNumber: 77,
    llm: { effectiveModel: null, status: "fallback" },
    materials: issueMaterials,
    publishedAt: "2026-08-22T05:10:00Z",
    stats: issueStats,
    theses: [],
    title: "Race smoke",
  } : {
    daily_analysis: { analysis: {}, status: "fallback" },
    issue: {
      brief: "Текущий выпуск.",
      issue_date: "2026-08-22",
      issue_number: 77,
      theses: [],
      title: "Race smoke",
    },
    issue_llm_theses: { status: "fallback", theses: [] },
    issue_stats: issueStats,
    materials: issueMaterials,
    site: { title: "Radar" },
    stats: { day: issueStats, "30d": periodStats },
  };

  const fetch = async raw => {
    const requestPath = String(raw).replace("https://radar.test", "");
    if (requestPath.includes("/api/materials?") && requestPath.includes("period=30d")) {
      delayedRequestSeen = true;
      await delayedPeriod.promise;
      return {
        ok: true,
        status: 200,
        async json() {
          return v2
            ? { items: periodMaterials, nextCursor: null }
            : { materials: periodMaterials };
        },
      };
    }
    let payload = issue;
    if (requestPath.startsWith("/api/timeseries")) {
      payload = v2
        ? { items: [{ ...issueStats, date: "2026-08-22" }] }
        : { timeseries: [{ ...issueStats, stat_date: "2026-08-22" }] };
    } else if (requestPath.startsWith("/api/rubrics")) {
      payload = v2 ? [] : { rubrics: [] };
    } else if (requestPath.startsWith("/api/sources")) {
      payload = v2 ? [] : { sources: [] };
    } else if (requestPath.startsWith("/api/issues")) {
      payload = v2 ? { items: [], nextCursor: null } : { issues: [] };
    } else if (requestPath === "/api/stats?period=30d") {
      payload = periodStats;
    }
    return { ok: true, status: 200, async json() { return payload; } };
  };

  const document = {
    body: new FakeElement("body"),
    addEventListener(event, handler) { documentHandlers.set(event, handler); },
    getElementById(id) { return elements.get(id) || null; },
    querySelector(selector) {
      return selector.startsWith("#") ? elements.get(selector.slice(1)) || null : null;
    },
    querySelectorAll() { return []; },
  };
  const context = vm.createContext({
    URL,
    URLSearchParams,
    clearInterval() {},
    clearTimeout,
    console,
    document,
    fetch,
    HTMLElement: FakeElement,
    localStorage: { getItem() { return null; }, setItem() {} },
    setInterval() { return 1; },
    setTimeout,
    window: {
      location: { hash: "", hostname: "radar.test", origin: "https://radar.test", port: "" },
      matchMedia() { return { matches: true }; },
      open() { return null; },
      setTimeout,
    },
  });
  const scriptPath = path.join(repositoryRoot, relativeScript);
  vm.runInContext(fs.readFileSync(scriptPath, "utf8"), context, { filename: scriptPath });
  await waitTurn();
  await waitTurn();

  const click = documentHandlers.get("click");
  if (!click) throw new Error(`${name}: click handler was not registered`);
  const periodButton = period => ({ dataset: { period }, classList: { toggle() {} } });
  click({ target: { closest() { return periodButton("30d"); } } });
  await waitTurn();
  if (!delayedRequestSeen) throw new Error(`${name}: delayed 30-day request was not observed`);
  click({ target: { closest() { return periodButton("issue"); } } });
  await waitTurn();

  delayedPeriod.resolve();
  await waitTurn();
  await waitTurn();

  const blips = countOccurrences(elements.get("radarViz").innerHTML, 'class="sonar-blip"');
  const cards = countOccurrences(elements.get("columns").innerHTML, '<article class="card');
  const included = Number(elements.get("included").textContent);
  const title = elements.get("radarTitle").textContent;
  if (title !== sonarTitle || included !== 1 || blips !== 1 || cards !== 1) {
    throw new Error(
      `${name}: stale 30-day response committed after issue reload `
      + `(title=${title}, included=${included}, blips=${blips}, cards=${cards})`,
    );
  }
}

// The title is what says which period actually won the race, so it is named per
// front end rather than assumed shared. V2 began saying which day the sonar is
// showing on 2026-08-25; Legacy did not, and this smoke is the only place that
// noticed the two had stopped agreeing.
await runCase({
  name: "Legacy",
  relativeScript: "work/radar-app/app.js",
  v2: false,
  sonarTitle: "Сонар",
});
await runCase({
  name: "V2",
  relativeScript: "v2/apps/web/app.mjs",
  v2: true,
  sonarTitle: "Сонар · сегодня",
});

process.stdout.write("Frontend period-switch race smoke: PASS (Legacy + V2)\n");
