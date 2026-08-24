function defaultApiOrigin() {
  const host = window.location.hostname;
  const port = window.location.port;
  if ((host === "127.0.0.1" || host === "localhost") && port !== "8765") {
    return "http://127.0.0.1:8765";
  }
  return window.location.origin;
}

const API = window.RADAR_API || defaultApiOrigin();

function legacyMaterial(item) {
  const llm = item.llm || {};
  return {
    ...item,
    agpm_takeaway: item.agpmTakeaway || "",
    canonical_url: item.canonicalUrl || item.url,
    key_material: Boolean(item.keyMaterial),
    llm_summary: {
      short_text: llm.status === "success" ? (item.llmShortText || "") : "",
      agpm_angle: llm.status === "success" ? (item.llmAgpmAngle || "") : "",
      status: llm.status || "fallback",
      model: llm.effectiveModel || null,
    },
    publication_date_status: item.publicationDateStatus,
    published_at: item.publishedAt,
    radar_issue_date: item.issueDate,
    signal_label: item.signalStrength === "strong" ? "Сильный сигнал" : null,
    signal_score: item.signalScore,
    signal_strength: item.signalStrength,
    source_name: item.sourceName,
    trend_notes: item.trendNotes,
  };
}

function legacyIssue(payload) {
  const blocks = payload.analysis?.blocks || [];
  const block = kind => blocks.find(item => item.kind === kind)?.text || "";
  const analysis = {
    headline: payload.brief || payload.title || "",
    signal: block("overview"),
    why_agpm: block("signals"),
    watch_next: block("watch_next") || block("outlook"),
    evidence_titles: (payload.materials || []).filter(item => item.keyMaterial).map(item => item.title),
  };
  return {
    site: { title: "Радар агентного проектного управления" },
    issue: {
      issue_date: payload.issueDate,
      issue_number: payload.issueNumber,
      title: payload.title,
      brief: payload.brief,
      theses: payload.theses || [],
    },
    issue_stats: payload.stats || {},
    stats: { day: payload.stats || {} },
    issue_llm_theses: {
      status: payload.llm?.status || "fallback",
      theses: payload.theses || [],
    },
    daily_analysis: {
      headline: analysis.headline,
      status: payload.llm?.status || "fallback",
      analysis,
    },
    materials: (payload.materials || []).map(legacyMaterial),
  };
}

function v2Path(path) {
  if (path === "/api/issue/latest") return "/api/latest";
  if (path.startsWith("/api/issue/")) return path.replace("/api/issue/", "/api/issues/");
  if (path.startsWith("/api/materials?") && path.includes("q=")) {
    return path.replace("/api/materials?", "/api/search?");
  }
  return path;
}

function legacyPayload(path, payload) {
  if (path === "/api/issue/latest" || path.startsWith("/api/issue/")) return legacyIssue(payload);
  if (path.startsWith("/api/timeseries")) {
    return { timeseries: (payload.items || []).map(item => ({ ...item, stat_date: item.date })) };
  }
  if (path.startsWith("/api/rubrics")) return { rubrics: payload };
  if (path.startsWith("/api/sources")) return { sources: payload };
  if (path.startsWith("/api/issues")) {
    return {
      issues: (payload.items || []).map(item => ({
        ...item,
        issue_date: item.issueDate,
        issue_number: item.issueNumber,
      })),
    };
  }
  if (path.startsWith("/api/materials")) return { materials: (payload.items || []).map(legacyMaterial) };
  return payload;
}

const state = {
  period: "issue",
  perimeter: "all",
  rubrics: [],
  q: "",
  loading: false,
  openDay: null,
  issueDate: null,
  materials: [],
  viewMode: "radar",
};

let latest = null;
let rubrics = [];
let sources = [];
let timeseries = [];
let publicationTimeseries = [];
let issues = [];
const periodStats = new Map();
const issueCache = new Map();
let ringTimer = null;
let reloadGeneration = 0;

const TIMESERIES_RETRY_DELAYS_MS = [0, 800, 2000];

const perimeters = {
  near: { title: "Близкий периметр", color: "var(--near)", desc: "AgPM, PMO, ИСУП, портфели и проектная координация." },
  mid: { title: "Средний периметр", color: "var(--mid)", desc: "Governance, ответственность, процессы, оркестрация и безопасность." },
  far: { title: "Дальний периметр", color: "var(--far)", desc: "Инфраструктура агентов, внедрение, вендоры, исследования и сделки." },
};

const rubricNames = {
  agpm_pmo_portfolio: "AgPM / PMO",
  isup_coordination: "ИСУП",
  governance_control: "Governance",
  human_responsibility: "Ответственность",
  workflow_orchestration: "Оркестрация",
  security_access: "Безопасность",
  mcp_gateways_infra: "MCP / инфраструктура",
  enterprise_adoption: "Внедрение",
  vendors_releases: "Вендоры",
  research_methodology: "Исследования",
  funding_ma: "Инвестиции",
};

const rubricGroups = [
  { title: "Близкий управленческий контур", ids: ["agpm_pmo_portfolio", "isup_coordination"] },
  { title: "Механизмы контроля и исполнения", ids: ["governance_control", "human_responsibility", "workflow_orchestration", "security_access"] },
  { title: "Инфраструктура и рынок", ids: ["mcp_gateways_infra", "enterprise_adoption", "vendors_releases", "research_methodology", "funding_ma"] },
];

const rubricBlockClasses = {
  agpm_pmo_portfolio: "near",
  isup_coordination: "near",
  governance_control: "mid",
  human_responsibility: "mid",
  workflow_orchestration: "mid",
  security_access: "mid",
  mcp_gateways_infra: "far",
  enterprise_adoption: "far",
  vendors_releases: "far",
  research_methodology: "far",
  funding_ma: "far",
};

const rubricTagClasses = {
  agpm_pmo_portfolio: "tag-agpm",
  isup_coordination: "tag-isup",
  governance_control: "tag-governance",
  human_responsibility: "tag-human",
  workflow_orchestration: "tag-workflow",
  security_access: "tag-security",
  mcp_gateways_infra: "tag-mcp",
  enterprise_adoption: "tag-enterprise",
  vendors_releases: "tag-vendors",
  research_methodology: "tag-research",
  funding_ma: "tag-funding",
};

const weekday = ["вс", "пн", "вт", "ср", "чт", "пт", "сб"];

async function getJson(path) {
  const response = await fetch(API + v2Path(path));
  if (!response.ok) throw new Error(`${response.status} ${path}`);
  return legacyPayload(path, await response.json());
}

function qs(params) {
  const out = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== "" && value !== "all") out.set(key, value);
  });
  return out.toString();
}

function dateUtc(value) {
  if (!value) return null;
  const [year, month, day] = String(value).slice(0, 10).split("-").map(Number);
  if (!year || !month || !day) return null;
  return new Date(Date.UTC(year, month - 1, day));
}

function dayOfMonth(value) {
  return dateUtc(value)?.getUTCDate() || "";
}

function monthIndex(value) {
  return dateUtc(value)?.getUTCMonth() ?? 0;
}

function fullYear(value) {
  return dateUtc(value)?.getUTCFullYear() ?? 0;
}

function weekdayIndex(value) {
  return dateUtc(value)?.getUTCDay() ?? 0;
}

function fmtDate(value, compact = false) {
  if (!value) return "";
  const date = dateUtc(value);
  if (!date) return "";
  const opts = compact ? { day: "numeric", month: "short" } : { day: "numeric", month: "long", year: "numeric" };
  return date.toLocaleDateString("ru-RU", { ...opts, timeZone: "UTC" }).replace(".", "");
}

function shiftDate(value, days) {
  if (!value) return null;
  const date = dateUtc(value);
  if (!date) return null;
  date.setUTCDate(date.getUTCDate() + days);
  return date.toISOString().slice(0, 10);
}

function pluralRu(value, one, few, many) {
  const n = Math.abs(Number(value) || 0);
  const n10 = n % 10;
  const n100 = n % 100;
  if (n10 === 1 && n100 !== 11) return one;
  if (n10 >= 2 && n10 <= 4 && (n100 < 12 || n100 > 14)) return few;
  return many;
}

function activeIssueDate() {
  return state.issueDate || latest?.issue?.issue_date || null;
}

function yesterdayIssueDate() {
  return shiftDate(latest?.issue?.issue_date, -1);
}

function issueLabel(value, issueNumber = latest?.issue?.issue_number) {
  const suffix = issueNumber ? `выпуск ${issueNumber}` : "выбранный выпуск";
  return `${weekday[weekdayIndex(value)]} · ${fmtDate(value)} · ${suffix}`;
}

function sourceHost(url) {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch (_) {
    return "";
  }
}

function sourceLabel(value) {
  const name = String(value || "");
  if (name.startsWith("AI Agents Directory")) return "AI Agents Directory";
  if (name.startsWith("OpenClaw web research")) return "OpenClaw · близкий";
  if (name.startsWith("Perplexity fresh web research")) {
    if (name.includes(": near")) return "Perplexity · близкий";
    if (name.includes(": middle")) return "Perplexity · средний";
    if (name.includes(": far")) return "Perplexity · дальний";
    return "Perplexity";
  }
  return name;
}

function materialDateLabel(item, compact = true) {
  if (item.published_at) return `опубл. ${fmtDate(item.published_at, compact)}`;
  return item.radar_issue_date ? `дата публикации не найдена · выпуск ${fmtDate(item.radar_issue_date, compact)}` : "дата публикации не найдена";
}

function setText(id, value) {
  const el = document.getElementById(id);
  if (el) el.textContent = value;
}

const VIEW_MODES = ["radar", "gazette", "agent"];

function setViewMode(mode) {
  state.viewMode = VIEW_MODES.includes(mode) ? mode : "radar";
  document.body.classList.toggle("is-gazette", state.viewMode === "gazette");
  document.body.classList.toggle("is-agent", state.viewMode === "agent");
  document.getElementById("gazetteView")?.toggleAttribute("hidden", state.viewMode !== "gazette");
  document.getElementById("agentView")?.toggleAttribute("hidden", state.viewMode !== "agent");
  // The opening tab is marked active in the markup, so no `setAgentTab` runs for
  // it and nothing would fill its subject list.
  if (state.viewMode === "agent" && agentState.tab === "find") agentLoadTopicOptions();
  document.querySelectorAll("[data-view-mode]").forEach(button => {
    const active = button.dataset.viewMode === state.viewMode;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-pressed", active ? "true" : "false");
  });
  try {
    localStorage.setItem("agpmRadarViewMode", state.viewMode);
  } catch {
    // Private and embedded browsers may deny storage.
  }
}

function initViewMode() {
  let saved = "radar";
  try {
    saved = localStorage.getItem("agpmRadarViewMode") || "radar";
  } catch {
    saved = "radar";
  }
  setViewMode(window.location.hash === "#gazette" ? "gazette" : saved);
}

function printGazette() {
  const frame = document.querySelector(".gazette-frame");
  const fallback = () => {
    const printWindow = window.open("./gazette-20260803.html", "_blank", "noopener");
    if (!printWindow) return;
    printWindow.addEventListener("load", () => {
      printWindow.focus();
      printWindow.print();
    }, { once: true });
  };

  if (!frame?.contentWindow) {
    fallback();
    return;
  }

  const runPrint = () => {
    try {
      frame.contentWindow.focus();
      frame.contentWindow.print();
    } catch {
      fallback();
    }
  };

  try {
    if (frame.contentDocument?.readyState === "complete") {
      runPrint();
    } else {
      frame.addEventListener("load", runPrint, { once: true });
    }
  } catch {
    fallback();
  }
}

function escapeHtml(value) {
  return String(value || "").replace(/[&<>"']/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" }[ch]));
}

function safeExternalUrl(value) {
  try {
    const url = new URL(String(value || ""));
    return ["http:", "https:"].includes(url.protocol) ? url.href : "#";
  } catch (_) {
    return "#";
  }
}

function currentStats(materials) {
  if (["issue", "yesterday"].includes(state.period)) {
    const issueDate = activeIssueDate();
    const row = timeseries.find(item => item.stat_date === issueDate);
    if (row) return row;
    return latest.issue_stats || latest.stats?.day || countMaterials(materials);
  }
  return periodStats.get(state.period) || countMaterials(materials.filter(materialMatches));
}

function countMaterials(rows) {
  return rows.reduce((acc, row) => {
    acc.viewed += 1;
    acc.included += 1;
    if (["near", "mid", "far"].includes(row.perimeter)) acc[row.perimeter] += 1;
    if (["core", "adjacent"].includes(row.verdict)) acc[row.verdict] += 1;
    return acc;
  }, { viewed: 0, included: 0, cut: 0, near: 0, mid: 0, far: 0, core: 0, adjacent: 0 });
}

function periodStart(period) {
  const dates = timeseries.map(row => row.stat_date).sort();
  const last = dates.at(-1);
  if (!last || period === "day" || period === "yesterday") return last || null;
  return shiftDate(last, -(period === "7d" ? 6 : 29));
}

function updateSummary(stats, materials) {
  setText("viewed", stats.viewed || 0);
  setText("included", stats.included || 0);
  setText("cut", stats.cut || 0);
  document.getElementById("perimeters").innerHTML = `<span class="near-text">${stats.near || 0}</span> / <span class="mid-text">${stats.mid || 0}</span> / <span class="far-text">${stats.far || 0}</span>`;
  setText("nearShare", stats.included ? `${Math.round((stats.near || 0) / stats.included * 100)}%` : "0%");
  setText("includedShare", stats.viewed ? `${Math.round((stats.included || 0) / stats.viewed * 100)}% от просмотренного` : "0% от просмотренного");
  setText("nearChip", stats.near || 0);
  setText("midChip", stats.mid || 0);
  setText("farChip", stats.far || 0);
  renderSparkline();
}

function renderSparkline() {
  const max = Math.max(1, ...timeseries.map(row => Number(row.included) || 0));
  const selected = activeIssueDate();
  document.getElementById("sparkline").innerHTML = timeseries.slice(-30).map((row, index, arr) => {
    const value = Number(row.included) || 0;
    const height = Math.max(4, Math.round(value / max * 32));
    const classes = [index === arr.length - 1 ? "is-last" : "", row.stat_date === selected ? "is-selected" : ""].filter(Boolean).join(" ");
    return `<span class="${classes}" style="height:${height}px" title="${fmtDate(row.stat_date, true)} · ${value}"></span>`;
  }).join("");
}

function renderTheses(materials) {
  const selectedPayload = state.issueDate ? issueCache.get(state.issueDate) : issueCache.get(latest?.issue?.issue_date);
  const selectedIssue = selectedPayload?.issue || latest?.issue;
  const selectedLlmTheses = selectedPayload?.issue_llm_theses;
  const periodTheses = latest?.period_theses?.[state.period]?.theses || [];
  const issueLlmTheses = selectedLlmTheses?.status === "success" && selectedLlmTheses?.theses?.length ? selectedLlmTheses.theses : [];
  const generated = periodTheses.length ? periodTheses : issueLlmTheses.length ? issueLlmTheses : selectedIssue?.theses?.length ? selectedIssue.theses : [
    { lead: "Операционная агентность требует governance.", rest: "В материалах чаще всего появляются контроль доступа, workflow, трассировка и корпоративная эксплуатация." },
    { lead: "Близкий периметр остаётся управленческим фильтром.", rest: "PMO-сценарии важны там, где агент помогает статусу, риску, поручению или портфельной видимости." },
    { lead: "Дата публикации отделена от даты обнаружения.", rest: "Материал живёт в архиве по реальной дате первоисточника, а не по дате выпуска радара." },
    { lead: "Agent-washing остаётся шумом.", rest: "Публично показываем отобранный слой и агрегаты отсечения, полный список шума остаётся внутренним." },
  ];
  setText("thesesTitle", thesesTitle());
  document.getElementById("theses").innerHTML = generated.slice(0, 4).map((item, index) => {
    const lead = item.lead || item[0] || "";
    const rest = item.rest || item[1] || "";
    return `<div class="thesis"><span class="thesis__num">${String(index + 1).padStart(2, "0")}</span><div><strong>${escapeHtml(lead)}</strong> ${escapeHtml(rest)}</div></div>`;
  }).join("");
  renderDailyAnalysis();
  renderRadarWidget(materials);
}

function renderDailyAnalysis() {
  const root = document.getElementById("dailyAnalysis");
  const headline = document.getElementById("dailyAnalysisHeadline");
  const body = document.getElementById("dailyAnalysisBody");
  if (!["issue", "yesterday"].includes(state.period)) {
    root.hidden = true;
    return;
  }
  const issueDate = activeIssueDate();
  const payload = issueDate ? issueCache.get(issueDate) : null;
  const analysis = payload?.daily_analysis?.analysis || {};
  if (!analysis.headline && !analysis.signal && !analysis.why_agpm && !analysis.watch_next) {
    root.hidden = true;
    return;
  }
  root.hidden = false;
  headline.textContent = analysis.headline || payload?.daily_analysis?.headline || "";
  const evidence = Array.isArray(analysis.evidence_titles) ? analysis.evidence_titles.filter(Boolean) : [];
  body.innerHTML = [
    analysis.signal ? `<section class="daily-analysis__section"><h3>Сигнал</h3>${renderAnalysisText(analysis.signal)}</section>` : "",
    analysis.why_agpm ? `<section class="daily-analysis__section"><h3>Почему важно для AgPM</h3>${renderAnalysisText(analysis.why_agpm)}</section>` : "",
    analysis.watch_next ? `<section class="daily-analysis__section"><h3>Что смотреть дальше</h3>${renderAnalysisText(analysis.watch_next)}</section>` : "",
    evidence.length ? `<section class="daily-analysis__section"><h3>Опорные материалы</h3><ul class="daily-analysis__evidence">${evidence.map(title => `<li>${escapeHtml(title)}</li>`).join("")}</ul></section>` : "",
  ].join("");
}

function renderAnalysisText(value) {
  return String(value || "")
    .split(/\n{2,}/)
    .map(text => text.trim())
    .filter(Boolean)
    .map(text => `<p>${escapeHtml(text)}</p>`)
    .join("");
}

function thesesTitle() {
  if (state.period === "7d") return "Что важно для AgPM за 7 дней";
  if (state.period === "30d") return "Что важно для AgPM за 30 дней";
  if (state.period === "yesterday") return "Что важно для AgPM во вчерашнем выпуске";
  if (state.issueDate && state.issueDate !== latest?.issue?.issue_date) return `Что важно для AgPM за ${fmtDate(state.issueDate, true)}`;
  return "Что важно для AgPM сегодня";
}

function renderRadarWidget(materials) {
  const root = document.getElementById("radarViz");
  const stats = currentStats(materials);
  const visible = materials.filter(materialMatches);
  if (ringTimer) {
    clearInterval(ringTimer);
    ringTimer = null;
  }
  if (state.period === "7d") {
    document.getElementById("radarTitle").textContent = "Доли периметров";
    root.innerHTML = renderShareWidget(stats);
    return;
  }
  if (state.period === "30d") {
    document.getElementById("radarTitle").textContent = "Кольцо 30 дней";
    root.innerHTML = renderRingWidget(stats);
    startRingTicker();
    return;
  }
  document.getElementById("radarTitle").textContent = "Сонар";
  root.innerHTML = renderSonarWidget(visible);
}

function renderSonarWidget(materials) {
  return `<div class="radar-widget">
    <svg viewBox="0 0 240 240" class="radar-svg radar-sonar" role="img" aria-label="Сонар: материалы на трёх кольцах">
      <circle cx="120" cy="120" r="34" fill="none" stroke="#e2e5e4" stroke-width="1"></circle>
      <circle cx="120" cy="120" r="64" fill="none" stroke="#e2e5e4" stroke-width="1"></circle>
      <circle cx="120" cy="120" r="94" fill="none" stroke="#dadddc" stroke-width="1"></circle>
      <line x1="120" y1="26" x2="120" y2="20" stroke="#c7cbce" stroke-width="1"></line>
      <line x1="214" y1="120" x2="220" y2="120" stroke="#c7cbce" stroke-width="1"></line>
      <line x1="120" y1="214" x2="120" y2="220" stroke="#c7cbce" stroke-width="1"></line>
      <line x1="26" y1="120" x2="20" y2="120" stroke="#c7cbce" stroke-width="1"></line>
      <text x="120" y="82" text-anchor="middle" font-size="9" fill="#8a9199" paint-order="stroke" stroke="#ffffff" stroke-width="3">близкий</text>
      <text x="120" y="52" text-anchor="middle" font-size="9" fill="#8a9199" paint-order="stroke" stroke="#ffffff" stroke-width="3">средний</text>
      <text x="120" y="22" text-anchor="middle" font-size="9" fill="#8a9199" paint-order="stroke" stroke="#ffffff" stroke-width="3">дальний</text>
      <g class="sonar-sweep" data-anim>
        <path d="M120 120 L120 26 A94 94 0 0 1 167 38.6 Z" fill="rgba(43,74,117,0.06)"></path>
        <line x1="120" y1="120" x2="120" y2="26" stroke="rgba(43,74,117,0.45)" stroke-width="1"></line>
      </g>
      <circle cx="120" cy="120" r="2.6" fill="#1f242a"></circle>
      ${sonarBlips(materials).join("")}
    </svg>
    ${sonarLegend(materials)}
  </div>`;
}

function sonarLegend(materials) {
  const counts = countPerimeters(materials);
  const rows = [
    ["near", "близкий", "#2B4A75", counts.near || 0],
    ["mid", "средний", "#1E6E62", counts.mid || 0],
    ["far", "дальний", "#6B6880", counts.far || 0],
  ];
  return `<div class="sonar-legend">
    <div class="sonar-legend__items">
      ${rows.map(([, label, color, count]) => `<span><i style="background:${color}"></i>${label} · <b class="mono">${count}</b></span>`).join("")}
    </div>
    <div class="sonar-note">${sonarPeriodLabel(materials.length)}</div>
  </div>`;
}

function sonarPeriodLabel(total) {
  const noun = pluralRu(total, "материал", "материала", "материалов");
  if (state.period === "yesterday") {
    const date = activeIssueDate() || publicationTimeseries.at(-2)?.stat_date || latest?.issue?.issue_date;
    return `${total} ${noun} на радаре · вчера за ${fmtDate(date, true)}`;
  }
  return `${total} ${noun} на радаре · выпуск за ${fmtDate(activeIssueDate(), true)}`;
}

function sonarBlips(materials) {
  const byPerimeter = { near: [], mid: [], far: [] };
  materials.forEach(item => {
    if (byPerimeter[item.perimeter]) byPerimeter[item.perimeter].push(item);
  });
  return Object.entries(byPerimeter).flatMap(([perimeter, rows]) => {
    const meta = sonarPerimeterMeta(perimeter);
    const angles = distributeAngles(rows);
    return rows.map((item, index) => sonarBlip(item, angles[index], meta));
  });
}

function sonarPerimeterMeta(perimeter) {
  if (perimeter === "near") return { r: 34, dot: 3.4, color: "#2B4A75" };
  if (perimeter === "mid") return { r: 64, dot: 3, color: "#1E6E62" };
  return { r: 94, dot: 3, color: "#6B6880" };
}

function distributeAngles(rows) {
  const used = [];
  return rows.map((item, index) => {
    let angle = hashAngle(item.id || item.url || item.title || index);
    for (let step = 0; step < 22 && used.some(value => angularDistance(value, angle) < 8); step += 1) {
      angle = (angle + 17) % 360;
    }
    used.push(angle);
    return angle;
  });
}

function hashAngle(value) {
  let hash = 0;
  String(value).split("").forEach(ch => {
    hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
  });
  return hash % 360;
}

function angularDistance(a, b) {
  const diff = Math.abs(a - b) % 360;
  return Math.min(diff, 360 - diff);
}

function sonarBlip(item, angle, meta) {
  const rad = angle * Math.PI / 180;
  const x = 120 + meta.r * Math.cos(rad);
  const y = 120 + meta.r * Math.sin(rad);
  const delay = (((angle + 90) % 360) / 360) * 8 - 8;
  const label = escapeHtml(item.title || "");
  return `<circle class="sonar-blip" data-anim cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="${item.key_material ? meta.dot + .4 : meta.dot}" fill="${meta.color}" style="animation-delay:${delay.toFixed(2)}s"><title>${label}</title></circle>`;
}

function renderShareWidget(stats) {
  const total = Math.max(1, (stats.near || 0) + (stats.mid || 0) + (stats.far || 0));
  const shares = {
    near: (stats.near || 0) / total,
    mid: (stats.mid || 0) / total,
    far: (stats.far || 0) / total,
  };
  const previous = previousSevenDayShares();
  return `<div class="radar-widget radar-widget-share">
    <div class="share-stage">
    <svg viewBox="0 0 240 240" class="radar-svg radar-share" role="img" aria-label="Доли периметров за 7 дней">
      <circle cx="120" cy="120" r="46" class="share-track"></circle>
      <circle cx="120" cy="120" r="72" class="share-track"></circle>
      <circle cx="120" cy="120" r="98" class="share-track"></circle>
      ${shareMarker(46, previous.near)}
      ${shareMarker(72, previous.mid)}
      ${shareMarker(98, previous.far)}
      ${shareArc("near", 46, shares.near, "#2B4A75")}
      ${shareArc("mid", 72, shares.mid, "#1E6E62")}
      ${shareArc("far", 98, shares.far, "#6B6880")}
    </svg>
    <div class="radar-center">
      <div class="mono radar-num">${stats.included || 0}</div>
      <div class="radar-caption">материалов · 7 дней</div>
    </div>
    </div>
    ${shareLegend(stats, shares)}
    <div class="share-note">${ringRangeLabel(timeseries.slice(-7))} · серые засечки — предыдущие 7 дней</div>
  </div>`;
}

function shareArc(perimeter, radius, share, color) {
  const length = 2 * Math.PI * radius;
  const arc = Math.max(0.01, share * length);
  const gap = Math.max(0.01, length - arc);
  return `<circle class="share-arc share-arc-${perimeter}" data-anim cx="120" cy="120" r="${radius}" stroke="${color}" stroke-dasharray="${arc.toFixed(1)} ${gap.toFixed(1)}" stroke-dashoffset="0" style="--arc:${arc.toFixed(1)};--gap:${gap.toFixed(1)}" transform="rotate(-90 120 120)"></circle>`;
}

function shareLegend(stats, shares) {
  const rows = [
    ["near", "близкий", "#2B4A75", stats.near || 0, shares.near],
    ["mid", "средний", "#1E6E62", stats.mid || 0, shares.mid],
    ["far", "дальний", "#6B6880", stats.far || 0, shares.far],
  ];
  return `<div class="share-legend">
    ${rows.map(([, label, color, count, share]) => `<span><i style="background:${color}"></i>${label} ${Math.round((share || 0) * 100)}% · <b>${count}</b></span>`).join("")}
  </div>`;
}

function shareMarker(radius, share) {
  const angle = -90 + Math.max(0, Math.min(1, share || 0)) * 360;
  const a = polar(120, 120, radius + 5, angle);
  const b = polar(120, 120, radius + 19, angle);
  return `<line class="share-mark" x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}"></line>`;
}

function previousSevenDayShares() {
  const rows = timeseries.slice(-14, -7);
  const stats = rows.reduce((acc, row) => {
    acc.near += Number(row.near) || 0;
    acc.mid += Number(row.mid) || 0;
    acc.far += Number(row.far) || 0;
    return acc;
  }, { near: 0, mid: 0, far: 0 });
  const total = Math.max(1, stats.near + stats.mid + stats.far);
  return { near: stats.near / total, mid: stats.mid / total, far: stats.far / total };
}

function renderRingWidget(stats) {
  const rows = timeseries.slice(-30);
  const maxTotal = Math.max(1, ...rows.map(row => ringTotal(row)));
  return `<div class="radar-widget radar-widget-ring">
    <div class="ring-stage">
      <svg viewBox="0 0 300 300" class="radar-svg radar-ring" role="img" aria-label="Включено в радар по дням месяца">
        <circle cx="150" cy="150" r="48" fill="none" stroke="#eef0ef" stroke-width="1"></circle>
        <g>${rows.map((row, index) => ringBar(row, index, rows.length, maxTotal)).join("")}</g>
        <g id="ringHl" class="ring-highlight" data-anim>
          <path d="M150 150 L134.9 10.8 A140 140 0 0 1 165.1 10.8 Z" fill="rgba(43,74,117,0.07)"></path>
        </g>
      </svg>
      <div class="radar-center">
        <div class="mono" id="ringDate" style="font-size:12.5px"></div>
        <div class="radar-caption" style="margin-top:1px">включено <span id="ringTot"></span></div>
        <div class="mono radar-center__split"><span style="color:#2B4A75">Б <span id="ringN"></span></span><span style="color:#1E6E62">С <span id="ringM"></span></span><span style="color:#6B6880">Д <span id="ringF"></span></span></div>
      </div>
    </div>
    <div class="ring-legend">
      <span><i class="legend-dot near"></i>близкий · <b class="mono">${stats.near || 0}</b></span>
      <span><i class="legend-dot mid"></i>средний · <b class="mono">${stats.mid || 0}</b></span>
      <span><i class="legend-dot far"></i>дальний · <b class="mono">${stats.far || 0}</b></span>
    </div>
    <div class="ring-note">включено по дням · ${ringRangeLabel(rows)} · приглушённые — выходные</div>
  </div>`;
}

function ringTotal(row) {
  return (Number(row.near) || 0) + (Number(row.mid) || 0) + (Number(row.far) || 0);
}

function ringBar(row, index, total, maxTotal) {
  const angle = index * (360 / Math.max(total, 1));
  const weekend = isWeekend(row.stat_date) ? .45 : 1;
  let base = 54;
  const dayTotal = ringTotal(row);
  const active = dayTotal > 0;
  const scale = active ? Math.min(16, 76 / Math.max(1, maxTotal)) : 0;
  const parts = [
    [Number(row.near) || 0, "#2B4A75"],
    [Number(row.mid) || 0, "#1E6E62"],
    [Number(row.far) || 0, "#6B6880"],
  ];
  const segments = parts.map(([value, color]) => {
    const length = value * scale;
    const a = ringPoint(base, angle);
    const b = ringPoint(base + length, angle);
    base += length;
    if (!value) return "";
    return `<line x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}" stroke="${color}" stroke-width="7" opacity="${weekend}"><title>${fmtDate(row.stat_date)} · ${row.near || 0}/${row.mid || 0}/${row.far || 0}</title></line>`;
  }).join("");
  if (segments) return segments;
  const a = ringPoint(54, angle);
  const b = ringPoint(60, angle);
  return `<line class="ring-empty" x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}"><title>${fmtDate(row.stat_date)} · 0/0/0</title></line>`;
}

function ringRangeLabel(rows) {
  if (!rows.length) return "30 дней";
  const firstDate = rows[0].stat_date;
  const lastDate = rows.at(-1).stat_date;
  const sameMonth = monthIndex(firstDate) === monthIndex(lastDate) && fullYear(firstDate) === fullYear(lastDate);
  const firstLabel = dateUtc(firstDate).toLocaleDateString("ru-RU", { ...(sameMonth ? { day: "numeric" } : { day: "numeric", month: "long" }), timeZone: "UTC" });
  const lastLabel = dateUtc(lastDate).toLocaleDateString("ru-RU", { day: "numeric", month: "long", timeZone: "UTC" });
  return `${firstLabel}–${lastLabel}`;
}

function startRingTicker() {
  const rows = timeseries.slice(-30);
  if (!rows.length) return;
  const motionOK = !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  let current = Math.max(0, rows.findLastIndex(row => ringTotal(row) > 0));
  showRingDay(rows, current);
  if (!motionOK) return;
  ringTimer = setInterval(() => {
    current = nextRingIndex(rows, current);
    showRingDay(rows, current);
  }, 1400);
}

function nextRingIndex(rows, current) {
  for (let step = 1; step <= rows.length; step += 1) {
    const index = (current + step) % rows.length;
    if (ringTotal(rows[index]) > 0) return index;
  }
  return (current + 1) % rows.length;
}

function showRingDay(rows, index) {
  const row = rows[index];
  const step = 360 / Math.max(rows.length, 1);
  const total = ringTotal(row);
  const hl = document.getElementById("ringHl");
  if (hl) hl.style.transform = `rotate(${(index * step).toFixed(1)}deg)`;
  setText("ringDate", fmtDate(row.stat_date, true));
  setText("ringTot", total);
  setText("ringN", row.near || 0);
  setText("ringM", row.mid || 0);
  setText("ringF", row.far || 0);
}

function ringPoint(radius, degrees) {
  const rad = degrees * Math.PI / 180;
  return {
    x: (150 + radius * Math.sin(rad)).toFixed(1),
    y: (150 - radius * Math.cos(rad)).toFixed(1),
  };
}

function polar(cx, cy, radius, degrees) {
  const rad = degrees * Math.PI / 180;
  return {
    x: (cx + radius * Math.cos(rad)).toFixed(1),
    y: (cy + radius * Math.sin(rad)).toFixed(1),
  };
}

function isWeekend(value) {
  const day = weekdayIndex(value);
  return day === 0 || day === 6;
}

function materialMatches(item) {
  if (state.perimeter !== "all" && item.perimeter !== state.perimeter) return false;
  if (state.rubrics.length && !state.rubrics.some(id => (item.rubrics || []).includes(id))) return false;
  if (state.q) {
    const haystack = [item.title, item.summary, item.agpm_takeaway, item.source_name].join(" ").toLowerCase();
    if (!haystack.includes(state.q.toLowerCase())) return false;
  }
  return true;
}

function renderColumns(materials) {
  const root = document.getElementById("columns");
  root.classList.toggle("loading", state.loading);
  if (state.loading) {
    root.innerHTML = Object.keys(perimeters).map(key => `<section class="column column-${key}"><div class="skeleton"></div><div class="skeleton small"></div><div class="skeleton card-skel"></div><div class="skeleton card-skel"></div></section>`).join("");
    return;
  }
  const visibleKeys = Object.keys(perimeters).filter(key => state.perimeter === "all" || key === state.perimeter);
  root.innerHTML = visibleKeys.map(key => {
    const meta = perimeters[key];
    const rows = materials.filter(item => item.perimeter === key && materialMatches(item));
    return `<section class="column column-${key}" style="color:${meta.color}">
      <header class="column__head">
        <div class="perimeter-glyph" aria-hidden="true">${glyph(key)}</div>
        <div>
          <div class="column__title"><span>${meta.title}</span><span class="mono">${rows.length}</span></div>
          <div class="column__desc">${meta.desc}</div>
        </div>
      </header>
      <div class="cards">${rows.length ? rows.map(renderCard).join("") : '<div class="empty">Материалов по этому фильтру нет.</div>'}</div>
      <footer class="column__foot">${rows.filter(row => row.key_material).length} ключевых из ${rows.length} включённых · полный выпуск</footer>
    </section>`;
  }).join("");
  renderActiveFilter();
}

function glyph(active) {
  return `<svg viewBox="0 0 42 42"><circle class="${active === "far" ? "is-on" : ""}" cx="21" cy="21" r="17"></circle><circle class="${active === "mid" ? "is-on" : ""}" cx="21" cy="21" r="11"></circle><circle class="${active === "near" ? "is-on" : ""}" cx="21" cy="21" r="5"></circle></svg>`;
}

function renderCard(item) {
  const host = sourceHost(item.url) || item.source_name || "источник";
  const date = materialDateLabel(item);
  const signal = signalMeta(item);
  const description = item.brief || item.summary || "";
  const llmTakeaway = item.llm_summary && item.llm_summary.status === "success" ? item.llm_summary.short_text : "";
  const takeaway = llmTakeaway || item.agpm_takeaway || "";
  const tags = (item.rubrics || []).slice(0, 3).map(id => {
    const tagClass = rubricTagClasses[id] || "tag-default";
    return `<span class="tag ${tagClass}">${rubricNames[id] || id}</span>`;
  }).join("");
  return `<article class="card">
    <div class="card__meta">
      <span class="dot"></span>
      <span>${escapeHtml(host)}</span>
      <span class="mono">${escapeHtml(date)}</span>
      <span class="signal ${signal.className}" title="${escapeHtml(signal.title)}">${signal.mark} ${escapeHtml(signal.label)}</span>
    </div>
    <h3>${escapeHtml(item.title)}</h3>
    <p>${escapeHtml(description)}</p>
    <div class="takeaway"><span class="takeaway__label">Вывод для AgPM</span><p>${escapeHtml(takeaway)}</p></div>
    <div class="tags">${tags}</div>
    <a class="source-link" href="${escapeHtml(safeExternalUrl(item.url))}" target="_blank" rel="noopener">первоисточник ↗</a>
  </article>`;
}

function signalMeta(item) {
  const strength = item.signal_strength || (item.verdict === "core" ? "strong" : "context");
  const labels = {
    strong: "Сильный сигнал",
    context: "Контекст",
    watch: "Наблюдать",
  };
  const titles = {
    strong: "Материал можно использовать в методической или управленческой повестке почти сразу.",
    context: "Материал важен для понимания среды, но требует перевода в AgPM через интерпретацию.",
    watch: "Ранний или рыночный сигнал: стоит наблюдать, но не делать главным аргументом выпуска.",
  };
  return {
    label: item.signal_label || labels[strength] || labels.strong,
    title: titles[strength] || titles.strong,
    mark: strength === "watch" ? "◇" : "◆",
    className: `signal-${strength}`,
  };
}

function renderActiveFilter() {
  const parts = [];
  if (state.perimeter !== "all") parts.push(perimeters[state.perimeter].title);
  if (state.rubrics.length) {
    const labels = state.rubrics.map(id => rubricNames[id] || id);
    parts.push(`рубрики: ${labels.join(", ")}`);
  }
  if (state.q) parts.push(`поиск: ${state.q}`);
  const node = document.getElementById("activeFilter");
  document.getElementById("resetFilters").disabled = !parts.length;
  node.hidden = !parts.length;
  node.textContent = parts.length ? `Фильтр: ${parts.join(" · ")}` : "";
}

function renderBars(id, rows, labelKey, valueKey) {
  const max = Math.max(1, ...rows.map(row => Number(row[valueKey]) || 0));
  document.getElementById(id).innerHTML = rows.map(row => {
    const value = Number(row[valueKey]) || 0;
    const blockClass = rubricBlockClasses[row.id] || "default";
    const isActive = state.rubrics.includes(row.id);
    return `<button class="bar-row bar-row-${blockClass} ${isActive ? "is-active" : ""}" data-rubric-bar="${row.id || ""}" aria-pressed="${isActive ? "true" : "false"}">
      <span>${escapeHtml(row[labelKey] || "")}</span>
      <span class="bar-track"><span class="bar-fill" style="width:${Math.max(3, value / max * 100)}%"></span></span>
      <span class="mono">${value}</span>
    </button>`;
  }).join("");
}

function renderSourcesPanel() {
  const max = Math.max(1, ...sources.map(row => Number(row.included) || 0));
  const totalIncluded = sources.reduce((sum, row) => sum + (Number(row.included) || 0), 0);
  const top = sources[0];
  document.getElementById("sources").innerHTML = `${sources.slice(0, 8).map(row => {
    const included = Number(row.included) || 0;
    const includedWidth = Math.max(3, included / max * 100);
    const label = sourceLabel(row.name);
    return `<div class="source-row">
      <span class="source-row__name" title="${escapeHtml(row.name || "")}">${escapeHtml(label)}</span>
      <span class="source-track" title="${escapeHtml(row.name || "")}: включено ${included}">
        <span class="source-included" style="width:${includedWidth}%"></span>
      </span>
      <span class="mono source-row__count">${included}</span>
    </div>`;
  }).join("")}
  <p class="source-note">${sourceInsight(totalIncluded, top)}</p>`;
}

function sourceInsight(included, top) {
  if (!included) return "Источники появятся после следующего выпуска с включёнными материалами.";
  const leader = top?.name ? ` Главный входящий поток: ${sourceLabel(top.name)}.` : "";
  return `Показаны источники материалов, вошедших в публичный радар за выбранный период.${leader}`;
}

function niceAxisMax(value, stepBase = 10) {
  const raw = Math.max(1, Number(value) || 1);
  if (raw <= stepBase) return stepBase;
  return Math.ceil(raw / stepBase) * stepBase;
}

function niceCountAxisMax(value) {
  const raw = Math.max(1, Number(value) || 1);
  if (raw <= 4) return 4;
  if (raw <= 8) return 8;
  if (raw <= 12) return 12;
  if (raw <= 20) return 20;
  return Math.ceil(raw / 10) * 10;
}

function trendRangeLabel(rows) {
  if (!rows.length) return "30 дней · агрегаты радара";
  const firstDate = rows[0].stat_date;
  const lastDate = rows.at(-1).stat_date;
  const lastMonth = dateUtc(lastDate).toLocaleDateString("ru-RU", { month: "long", timeZone: "UTC" });
  if (monthIndex(firstDate) === monthIndex(lastDate) && fullYear(firstDate) === fullYear(lastDate)) {
    return `${dayOfMonth(firstDate)}–${dayOfMonth(lastDate)} ${lastMonth} · агрегаты радара`;
  }
  const firstLabel = dateUtc(firstDate).toLocaleDateString("ru-RU", { day: "numeric", month: "long", timeZone: "UTC" });
  const lastLabel = dateUtc(lastDate).toLocaleDateString("ru-RU", { day: "numeric", month: "long", timeZone: "UTC" });
  return `${firstLabel} – ${lastLabel} · агрегаты радара`;
}

function trendPolyline(points) {
  const valid = points.filter(point => Number.isFinite(point.y));
  return valid.map((point, index) => `${index ? "L" : "M"} ${point.x.toFixed(1)} ${point.y.toFixed(1)}`).join(" ");
}

function renderTrendPanels() {
  const rows = timeseries.slice(-30);
  const countMax = niceCountAxisMax(Math.max(1, ...rows.map(row => Number(row.included) || 0)));
  const maxShare = Math.max(20, ...rows.map(row => {
    const viewed = Number(row.viewed) || 0;
    return viewed ? Math.round((Number(row.included) || 0) / viewed * 100) : 0;
  }));
  const shareMax = Math.min(100, niceAxisMax(maxShare));
  const trendRange = document.getElementById("trendRange");
  if (trendRange) trendRange.textContent = trendRangeLabel(rows);

  const chart = { width: 760, height: 252, left: 42, right: 42, top: 18, bottom: 30 };
  chart.plotWidth = chart.width - chart.left - chart.right;
  chart.plotHeight = chart.height - chart.top - chart.bottom;
  chart.bottomY = chart.top + chart.plotHeight;
  const slot = chart.plotWidth / Math.max(1, rows.length);
  const barWidth = Math.min(18, Math.max(8, slot * 0.56));
  const yForCount = value => chart.bottomY - (Math.max(0, value) / countMax) * chart.plotHeight;
  const yForShare = value => chart.bottomY - (Math.min(shareMax, Math.max(0, value)) / shareMax) * chart.plotHeight;
  const xForIndex = index => chart.left + slot * index + slot / 2;
  const countTicks = [0, .25, .5, .75, 1].map(part => Math.round(countMax * part));
  const shareTicks = [shareMax, Math.round(shareMax / 2), 0];
  const grid = countTicks.map(tick => {
    const y = yForCount(tick);
    return `<line class="trend-gridline" x1="${chart.left}" x2="${chart.width - chart.right}" y1="${y.toFixed(1)}" y2="${y.toFixed(1)}"></line>
      <text class="trend-axis-label" x="${chart.left - 9}" y="${(y + 3).toFixed(1)}" text-anchor="end">${tick}</text>`;
  }).join("");
  const rightAxis = shareTicks.map(tick => {
    const y = yForShare(tick);
    return `<text class="trend-axis-label" x="${chart.width - 6}" y="${(y + 3).toFixed(1)}" text-anchor="end">${tick}%</text>`;
  }).join("");
  const weekendBands = rows.map((row, index) => {
    if (!isWeekend(row.stat_date)) return "";
    const x = chart.left + slot * index;
    return `<rect class="trend-band" x="${x.toFixed(1)}" y="${chart.top}" width="${slot.toFixed(1)}" height="${chart.plotHeight}"></rect>`;
  }).join("");
  const labels = rows.map((row, index) => {
    const day = dayOfMonth(row.stat_date);
    const isLast = index === rows.length - 1;
    if (![1, 8, 15, 22].includes(day) && !isLast) return "";
    const month = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"][monthIndex(row.stat_date)];
    const label = isLast ? `${day} ${month}` : day;
    return `<text class="trend-x-label" x="${xForIndex(index).toFixed(1)}" y="${chart.height - 7}" text-anchor="middle">${label}</text>`;
  }).join("");
  let lastShare = 0;
  const points = rows.map((row, index) => {
    const viewed = Number(row.viewed) || 0;
    const share = viewed ? (Number(row.included) || 0) / viewed * 100 : lastShare;
    if (viewed) lastShare = share;
    const x = xForIndex(index);
    const y = yForShare(share);
    return { x, y, share };
  });
  const bars = rows.map((row, index) => {
    const near = Number(row.near) || 0;
    const mid = Number(row.mid) || 0;
    const far = Number(row.far) || 0;
    const total = near + mid + far;
    const cut = Number(row.cut) || 0;
    const x = xForIndex(index) - barWidth / 2;
    let cursor = chart.bottomY;
    const segment = (value, color) => {
      if (!value) return "";
      const height = Math.max(2, value / countMax * chart.plotHeight);
      cursor -= height;
      return `<rect x="${x.toFixed(1)}" y="${cursor.toFixed(1)}" width="${barWidth.toFixed(1)}" height="${height.toFixed(1)}" fill="${color}"></rect>`;
    };
    return `<g class="trend-bar">
      <title>${fmtDate(row.stat_date)} · включено ${total}, отсечено ${cut}, доля включения ${Math.round(points[index].share)}%</title>
      ${segment(near, "#2B4A75")}
      ${segment(mid, "#1E6E62")}
      ${segment(far, "#6B6880")}
    </g>`;
  }).join("");
  document.getElementById("trendBars").innerHTML = `<svg class="trend-svg" viewBox="0 0 ${chart.width} ${chart.height}" role="img" aria-label="Материалы в радаре по дням">
    ${weekendBands}
    ${grid}
    ${rightAxis}
    ${bars}
    <path class="trend-line" d="${trendPolyline(points)}"></path>
    ${labels}
  </svg>`;
  const max = Math.max(1, ...rows.map(row => Number(row.included) || 0));
  const selected = activeIssueDate();
  document.getElementById("heatmap").innerHTML = timeseries.slice(-30).map(row => {
    const alpha = Math.min(0.85, 0.12 + (Number(row.included) || 0) / max * 0.7);
    const isSelected = ["issue", "yesterday"].includes(state.period) && row.stat_date === selected;
    return `<button class="heatmap-day ${isSelected ? "is-active" : ""}" data-issue-day="${row.stat_date}" style="background:rgba(43,74,117,${alpha})" title="${fmtDate(row.stat_date)} · ${row.included || 0}" aria-label="Показать выпуск за ${fmtDate(row.stat_date)}">${dayOfMonth(row.stat_date)}</button>`;
  }).join("");
}

function renderTimeline() {
  const root = document.getElementById("timeline");
  const rows = issues.slice(0, 5);
  root.innerHTML = rows.map(issue => {
    const isOpen = state.openDay === issue.issue_date;
    const issueMaterials = issue.materials || [];
    const shown = issueMaterials.slice(0, isOpen ? issueMaterials.length : 3);
    const moreCount = Math.max(0, issueMaterials.length - shown.length);
    const counts = countPerimeters(issueMaterials);
    const issueSources = Array.from(new Set(issueMaterials.map(item => sourceHost(item.url) || item.source_name).filter(Boolean))).slice(0, 4);
    return `<article class="timeline-row">
      <div class="timeline-date"><span class="mono">${fmtDate(issue.issue_date, true)}</span><small>${weekday[weekdayIndex(issue.issue_date)]}</small></div>
      <div class="timeline-main">
        <p>${escapeHtml(issue.brief || "Ежедневный выпуск радара AgPM.")}</p>
        <div class="timeline-materials">${shown.map(item => `<span><i class="${item.perimeter}"></i>${escapeHtml(item.title)} <em>${escapeHtml(sourceHost(item.url) || item.source_name || "")} · ${escapeHtml(materialDateLabel(item, true))}</em></span>`).join("")}</div>
        ${moreCount ? `<div class="timeline-more">и ещё ${moreCount} материалов · полный выпуск</div>` : ""}
        ${issueSources.length ? `<div class="timeline-sources">источники: ${issueSources.map(escapeHtml).join(" · ")}</div>` : ""}
      </div>
      <div class="timeline-side">
        <div class="mini-stack"><i class="near" style="width:${counts.near}px"></i><i class="mid" style="width:${counts.mid}px"></i><i class="far" style="width:${counts.far}px"></i></div>
        <span class="mono">${counts.near} / ${counts.mid} / ${counts.far}</span>
        <button class="link-button timeline-toggle ${isOpen ? "is-open" : ""}" data-open-day="${issue.issue_date}"><span aria-hidden="true">⌄</span>${isOpen ? "свернуть" : "состав дня"}</button>
      </div>
    </article>`;
  }).join("");
}

function countPerimeters(materials) {
  return materials.reduce((acc, item) => {
    acc[item.perimeter] = (acc[item.perimeter] || 0) + 1;
    return acc;
  }, { near: 0, mid: 0, far: 0 });
}

function renderRubricator() {
  const byId = new Map(rubrics.map(row => [row.id, row]));
  const cells = [];
  rubricGroups.forEach(group => {
    cells.push(`<div class="rubric-group-title">${escapeHtml(group.title)}</div>`);
    group.ids.forEach(id => {
      const row = byId.get(id);
      if (!row) return;
      const isActive = state.rubrics.includes(row.id);
      cells.push(`<button class="rubric-cell ${isActive ? "is-active" : ""}" data-rubric="${row.id}" aria-pressed="${isActive ? "true" : "false"}">
      <strong>${escapeHtml(row.title)}</strong>
      <span><i>${trendArrow(row)}</i><b class="mono">${row.count || 0}</b></span>
    </button>`);
    });
  });
  rubrics.filter(row => !rubricGroups.some(group => group.ids.includes(row.id))).forEach(row => {
    const isActive = state.rubrics.includes(row.id);
    cells.push(`<button class="rubric-cell ${isActive ? "is-active" : ""}" data-rubric="${row.id}" aria-pressed="${isActive ? "true" : "false"}">
    <strong>${escapeHtml(row.title)}</strong>
    <span><i>${trendArrow(row)}</i><b class="mono">${row.count || 0}</b></span>
  </button>`);
  });
  cells.push(`<div class="rubric-cell rubric-help"><strong>Множественный выбор</strong><span>Рубрики сужают выдачу по принципу «хотя бы одна из выбранных».</span></div>`);
  document.getElementById("rubricator").innerHTML = cells.join("");
}

function trendArrow(row) {
  const count = Number(row.count) || 0;
  if (count > 16) return "↗";
  if (count < 6) return "↘";
  return "→";
}

function renderFooterSources() {
  document.getElementById("footerSources").innerHTML = sources.slice(0, 6).map(row => `<span><b title="${escapeHtml(row.name || "")}">${escapeHtml(sourceLabel(row.name))}</b><i class="mono">${row.included || 0}</i></span>`).join("");
}

function toggleRubric(id) {
  if (!id) return;
  state.rubrics = state.rubrics.includes(id)
    ? state.rubrics.filter(item => item !== id)
    : [...state.rubrics, id];
  reload();
}

async function loadIssueMaterials(request) {
  if (["issue", "yesterday"].includes(request.period)) {
    const issueDate = request.issueDate;
    if (!issueDate || issueDate === latest?.issue?.issue_date) return latest.materials;
    const payload = await loadIssuePayload(issueDate);
    return payload.materials || [];
  }
  const params = { period: request.period, limit: 100 };
  let path = "/api/materials";
  if (request.q) {
    params.q = request.q;
    path = "/api/search";
  } else {
    params.perimeter = request.perimeter;
  }
  const materials = [];
  let cursor = null;
  do {
    const pageParams = { ...params };
    if (cursor) pageParams.cursor = cursor;
    const response = await fetch(`${API}${path}?${qs(pageParams)}`);
    if (!response.ok) throw new Error(`${response.status} ${path}`);
    const page = await response.json();
    materials.push(...(page.items || []).map(legacyMaterial));
    cursor = page.nextCursor || null;
  } while (cursor);
  return materials;
}

async function loadPeriodStats(period) {
  if (!["7d", "30d"].includes(period)) return;
  periodStats.set(period, await getJson(`/api/stats?period=${period}`));
}

async function loadIssuePayload(issueDate) {
  if (issueCache.has(issueDate)) return issueCache.get(issueDate);
  const response = await fetch(`${API}/api/issues/${issueDate}`);
  if (response.status === 404) {
    const empty = { issue: { issue_date: issueDate, theses: [] }, materials: [] };
    issueCache.set(issueDate, empty);
    return empty;
  }
  if (!response.ok) throw new Error(`${response.status} /api/issue/${issueDate}`);
  const payload = legacyIssue(await response.json());
  issueCache.set(issueDate, payload);
  return payload;
}

async function reload() {
  const generation = ++reloadGeneration;
  const request = {
    period: state.period,
    perimeter: state.perimeter,
    q: state.q,
    issueDate: activeIssueDate(),
  };
  state.loading = true;
  renderColumns(state.materials);
  let materials;
  try {
    [materials] = await Promise.all([
      loadIssueMaterials(request),
      loadPeriodStats(request.period),
    ]);
  } catch (error) {
    if (generation !== reloadGeneration) return;
    state.loading = false;
    throw error;
  }
  if (generation !== reloadGeneration) return;
  state.materials = materials;
  state.loading = false;
  const issue = ["issue", "yesterday"].includes(state.period) ? issueCache.get(activeIssueDate())?.issue : null;
  if (["issue", "yesterday"].includes(state.period)) {
    setText("issueDate", issueLabel(activeIssueDate(), issue?.issue_number));
  } else {
    setText("issueDate", issueLabel(latest.issue?.issue_date));
  }
  updateSummary(currentStats(materials), materials);
  renderTrendPanels();
  renderTheses(materials);
  renderColumns(materials);
  renderBars("rubrics", rubrics, "title", "count");
  renderRubricator();
}

async function init() {
  latest = await getJson("/api/issue/latest");
  issueCache.set(latest.issue.issue_date, {
    issue: latest.issue,
    daily_analysis: latest.daily_analysis,
    issue_llm_theses: latest.issue_llm_theses,
    materials: latest.materials || [],
  });
  setText("issueDate", issueLabel(latest.issue?.issue_date));
  await reload();
  loadTimeseriesData().catch(error => {
    console.warn("Radar timeseries unavailable after retries", error);
  });
  loadSecondaryData().catch(error => {
    console.warn("Secondary Radar data unavailable", error);
  });
}

function delay(ms) {
  return new Promise(resolve => window.setTimeout(resolve, ms));
}

async function getJsonWithRetry(path, delays = TIMESERIES_RETRY_DELAYS_MS) {
  let lastError = null;
  for (const waitMs of delays) {
    if (waitMs) await delay(waitMs);
    try {
      return await getJson(path);
    } catch (error) {
      lastError = error;
    }
  }
  throw lastError || new Error(`Не удалось загрузить ${path}`);
}

function fallbackIssueTimeseries() {
  if (timeseries.length || !latest?.issue?.issue_date) return;
  const stats = latest.issue_stats || latest.stats?.day;
  if (!stats) return;
  timeseries = [{ stat_date: latest.issue.issue_date, ...stats }];
}

function renderTimeseriesPanels() {
  updateSummary(currentStats(state.materials), state.materials);
  renderTrendPanels();
}

async function loadTimeseriesData() {
  try {
    const payload = await getJsonWithRetry("/api/timeseries?days=30");
    if (Array.isArray(payload.timeseries) && payload.timeseries.length) {
      timeseries = payload.timeseries;
    } else {
      fallbackIssueTimeseries();
    }
  } catch (error) {
    fallbackIssueTimeseries();
    renderTimeseriesPanels();
    throw error;
  }
  renderTimeseriesPanels();
}

async function loadSecondaryData() {
  const [rubricsResult, sourcesResult, publicationTimeseriesResult, issuesResult] = await Promise.allSettled([
    getJson("/api/rubrics?period=30d"),
    getJson("/api/sources?period=30d"),
    getJson("/api/timeseries?days=30&basis=publication"),
    getJson("/api/issues?limit=5"),
  ]);
  if (rubricsResult.status === "fulfilled") rubrics = rubricsResult.value.rubrics || [];
  if (sourcesResult.status === "fulfilled") sources = sourcesResult.value.sources || [];
  if (publicationTimeseriesResult.status === "fulfilled") publicationTimeseries = publicationTimeseriesResult.value.timeseries || [];
  const issueList = issuesResult.status === "fulfilled" ? issuesResult.value.issues || [] : [];
  const issueResults = await Promise.allSettled(issueList.map(async issue => {
    if (issue.issue_date === latest?.issue?.issue_date) {
      return { ...issue, materials: latest.materials || [] };
    }
    const payload = await getJson(`/api/issue/${issue.issue_date}`);
    issueCache.set(issue.issue_date, payload);
    return { ...issue, materials: payload.materials || [] };
  }));
  issues = issueResults.filter(result => result.status === "fulfilled").map(result => result.value);
  renderBars("rubrics", rubrics, "title", "count");
  renderSourcesPanel();
  renderTrendPanels();
  renderTimeline();
  renderRubricator();
  renderFooterSources();
  renderTheses(state.materials);
}

document.addEventListener("click", event => {
  // Read the attribute rather than trust the match: this runs before every other
  // branch, and a throw here would take the page's whole click handling with it.
  const named = event.target.closest?.("[data-graph-node]")?.dataset?.graphNode;
  if (named) {
    // Three kinds of node, three route parameters: an entity node used to land
    // on `claim=` and drew an empty picture for a name that is not a claim.
    const [kind, ...rest] = named.split(":");
    const key = rest.join(":");
    const parameter = kind === "topic" ? "topic" : kind === "entity" ? "entity" : "claim";
    agentLoadGraph(`${parameter}=${encodeURIComponent(key)}`);
    return;
  }
  const button = event.target.closest("button");
  if (!button) return;
  if (button.dataset.forceGraph) {
    // A subject's list is the honest default; its graph is a choice, and the
    // choice is the reader's, made knowingly (the meta has said how much of
    // the neighbourhood these thirty are).
    agentLoadGraph(button.dataset.forceGraph, { forceCanvas: true });
    return;
  }
  if (button.id === "printGazette") {
    printGazette();
    return;
  }
  if (button.dataset.agentTab) {
    setAgentTab(button.dataset.agentTab);
    return;
  }
  if (button.dataset.agentGraph) {
    setAgentTab("graph");
    agentLoadGraph(`claim=${encodeURIComponent(button.dataset.agentGraph)}`);
    return;
  }
  if (button.dataset.agentLinks) {
    agentToggleLinks(button.dataset.agentLinks);
    return;
  }
  if (button.dataset.agentTopic) {
    agentOpenTopic(button.dataset.agentTopic);
    return;
  }
  if (button.dataset.agentPage) {
    agentOpenPage(button.dataset.agentPage);
    return;
  }
  if (button.dataset.agentAdmission) {
    agentState.admission = button.dataset.agentAdmission;
    document.querySelectorAll("[data-agent-admission]").forEach(chip => {
      chip.classList.toggle("is-active", chip.dataset.agentAdmission === agentState.admission);
    });
    return;
  }
  if (button.dataset.viewMode) {
    setViewMode(button.dataset.viewMode);
  }
  if (button.dataset.period) {
    state.period = button.dataset.period;
    state.issueDate = button.dataset.period === "yesterday" ? yesterdayIssueDate() : null;
    document.querySelectorAll("[data-period]").forEach(btn => btn.classList.toggle("is-active", btn === button));
    reload();
  }
  if (button.dataset.perimeter) {
    state.perimeter = button.dataset.perimeter;
    document.querySelectorAll("[data-perimeter]").forEach(btn => btn.classList.toggle("is-active", btn === button));
    reload();
  }
  if (button.dataset.rubric !== undefined) {
    toggleRubric(button.dataset.rubric);
  }
  if (button.dataset.rubricBar !== undefined) {
    toggleRubric(button.dataset.rubricBar);
  }
  if (button.dataset.openDay) {
    state.openDay = state.openDay === button.dataset.openDay ? null : button.dataset.openDay;
    renderTimeline();
  }
  if (button.dataset.issueDay) {
    state.period = "issue";
    state.issueDate = button.dataset.issueDay;
    document.querySelectorAll("[data-period]").forEach(btn => btn.classList.toggle("is-active", btn.dataset.period === "issue"));
    reload();
    document.getElementById("columns")?.scrollIntoView({ behavior: "smooth", block: "start" });
  }
  if (button.id === "resetFilters") {
    state.perimeter = "all";
    state.rubrics = [];
    state.q = "";
    document.getElementById("search").value = "";
    document.querySelectorAll("[data-perimeter]").forEach(btn => btn.classList.toggle("is-active", btn.dataset.perimeter === "all"));
    reload();
  }
});

let searchTimer = null;
document.getElementById("search").addEventListener("input", event => {
  clearTimeout(searchTimer);
  state.q = event.target.value.trim();
  searchTimer = setTimeout(reload, 180);
});

initViewMode();

init().catch(error => {
  document.body.insertAdjacentHTML("afterbegin", `<div class="api-error">API недоступен: ${escapeHtml(error.message)}</div>`);
});

/* ---------------------------------------------------------------------------
 * Agent mode (stage 3). The third position of the switcher.
 *
 * Everything here reads /kb/*, a read-only service over the knowledge base. It
 * shares the radar's markup vocabulary - cards, chips, mono numbers - because it
 * is the same site, not a second product bolted on.
 *
 * The four levels of disclosure arrive in one response, so the answer and the
 * quotation under it cannot get out of step: what renders below is a view of one
 * object, never four fetches that could half-fail.
 * ------------------------------------------------------------------------- */

const KB = "/kb";

const AGENT_KIND_LABEL = {
  fact: "факт",
  opinion: "мнение",
  case: "кейс",
  forecast: "прогноз",
  product_release: "релиз",
  incident: "инцидент"
};

const AGENT_STATUS_LABEL = {
  canon: "канон",
  canon_adjacent: "рядом с каноном",
  operationalization: "операционализация",
  external_reference: "внешняя ссылка",
  observed_signal: "наблюдаемый сигнал",
  hypothesis: "гипотеза"
};

const AGENT_STATUS_CLASS = {
  canon: "agent-label--canon",
  canon_adjacent: "agent-label--canon",
  operationalization: "agent-label--near",
  external_reference: "agent-label--near",
  observed_signal: "agent-label--far",
  hypothesis: "agent-label--far"
};

const agentState = { tab: "find", admission: "knowledge", busy: false };

async function kbFetch(path, options) {
  const response = await fetch(`${KB}${path}`, options);
  if (!response.ok && response.status !== 404) {
    throw new Error(`служба базы знаний ответила ${response.status}`);
  }
  return response.json();
}

/** One shape, whichever end it came from.
 *
 * The panels return database rows in snake_case; `/kb/ask` returns numbered
 * evidence serialised by the answer layer in camelCase. The renderer used to read
 * snake_case only, so every answer in the Ask tab quietly lost its labels, its
 * date and its character range - the three levels of disclosure below the answer
 * itself. Normalising once here is the fix; a fallback at each use site is how it
 * broke, because three of the fields got one and the rest did not.
 */
function agentRow(row) {
  const pick = (...names) => {
    for (const name of names) {
      if (row[name] !== undefined && row[name] !== null) return row[name];
    }
    return undefined;
  };
  return {
    claimId: pick("claim_id", "claimId"),
    statement: pick("statement"),
    quote: pick("quote_text", "quote"),
    charStart: pick("char_start", "charStart"),
    charEnd: pick("char_end", "charEnd"),
    sourceUrl: pick("source_url", "sourceUrl"),
    sourceTitle: pick("source_title", "sourceTitle"),
    materialKind: pick("material_kind", "materialKind"),
    status: pick("status"),
    primarySource: pick("primary_source", "primarySource") || "",
    isRetelling: Boolean(pick("is_retelling", "isRetelling")),
    shownOn: pick("shown_on", "shownOn"),
    shownKind: pick("shown_kind", "shownKind"),
    validUntil: pick("valid_until", "validUntil"),
    matchedBy: pick("matched_by", "matchedBy") || []
  };
}

const AGENT_LINK_LABEL = {
  supports: "подтверждает",
  contradicts: "противоречит",
  qualifies: "уточняет",
  related_to: "связанное"
};

/** Decision 11 made visible.
 *
 * An expired statement is not a false one - the owner's rule says its review is
 * due. So the label says the date and nothing stronger, and the reader decides.
 */
function agentStale(row) {
  if (!row.validUntil) return "";
  const until = String(row.validUntil).slice(0, 10);
  if (until >= new Date().toISOString().slice(0, 10)) return "";
  return `<span class="agent-label agent-label--stale">⧖ срок истёк ${escapeHtml(until)}</span>`;
}

function agentDate(row) {
  if (!row.shownOn) return "";
  const kind = row.shownKind === "published" ? "дата публикации" : "дата обнаружения радаром";
  return `${row.shownOn} · ${kind}`;
}

/** The labels the owner requires beside every unit: kind, status, whose claim. */
function agentLabels(row) {
  const parts = [];
  if (row.status) {
    const cls = AGENT_STATUS_CLASS[row.status] || "agent-label--far";
    parts.push(`<span class="agent-label ${cls}"><span class="dot"></span>${escapeHtml(AGENT_STATUS_LABEL[row.status] || row.status)}</span>`);
  }
  if (row.materialKind) {
    parts.push(`<span class="agent-label">${escapeHtml(AGENT_KIND_LABEL[row.materialKind] || row.materialKind)}</span>`);
  }
  if (row.isRetelling && row.primarySource) {
    parts.push(`<span class="agent-label agent-label--retelling">пересказ → ${escapeHtml(row.primarySource)}</span>`);
  }
  parts.push(agentStale(row));
  return parts.join("");
}

function agentMatchedBy(matched) {
  if (!Array.isArray(matched) || !matched.length) return "";
  return matched
    .map(arm => `<span class="agent-arm agent-arm--${arm === "смысл" ? "meaning" : "words"}">по ${escapeHtml(arm === "смысл" ? "смыслу" : "словам")}</span>`)
    .join("");
}

/** One statement at levels 2, 3 and 4 - labels, quotation with its range, source. */
function agentStatementCard(raw, ordinal) {
  const row = agentRow(raw);
  const number = ordinal ? `<span class="mono agent-ordinal">[${ordinal}]</span>` : "";
  const range = Number.isInteger(row.charStart) && Number.isInteger(row.charEnd)
    ? `<span class="mono agent-range">знаки ${row.charStart}–${row.charEnd}</span>`
    : "";
  return `
    <article class="card agent-statement" data-claim="${escapeHtml(row.claimId || "")}">
      <div class="agent-statement__labels">
        ${number}${agentLabels(row)}
        <span class="agent-spacer"></span>
        <span class="mono agent-when">${escapeHtml(agentDate(row))}</span>
      </div>
      ${row.statement ? `<p class="agent-statement__text">${escapeHtml(row.statement)}</p>` : ""}
      <blockquote class="agent-quote">
        <p>${escapeHtml(row.quote || "")}</p>
        <div class="agent-quote__meta">${range}${agentMatchedBy(row.matchedBy)}</div>
      </blockquote>
      <div class="agent-statement__source">
        <span class="agent-statement__sourcelabel">Источник:</span>
        <a href="${escapeHtml(safeExternalUrl(row.sourceUrl))}" target="_blank" rel="noopener noreferrer">
          ${escapeHtml(row.sourceTitle || row.sourceUrl || "источник")}
        </a>
      </div>
      ${row.claimId ? `<button class="agent-links__toggle" type="button" data-agent-links="${escapeHtml(row.claimId)}">
        Что база связала с этим
      </button>
      <button class="agent-links__toggle" type="button" data-agent-graph="${escapeHtml(row.claimId)}">
        Показать в графе
      </button>
      <div class="agent-links" data-agent-links-for="${escapeHtml(row.claimId)}" hidden></div>` : ""}
    </article>`;
}

/** Level five, on demand: what else the base says about the same thing.
 *
 * `agent.link` stores a pair once in uuid order, so the direction on the row is
 * about which side the judge was shown first. For `qualifies` that matters and
 * the phrasing follows it; the other three read the same from either end.
 */
async function agentToggleLinks(claimId) {
  const box = document.querySelector(`[data-agent-links-for="${CSS.escape(claimId)}"]`);
  if (!box) return;
  if (!box.hidden) { box.hidden = true; return; }
  box.hidden = false;
  if (box.dataset.loaded) return;
  box.innerHTML = `<p class="agent-waiting">Читаю связи…</p>`;
  try {
    const data = await kbFetch(`/statement/${encodeURIComponent(claimId)}`);
    const links = Array.isArray(data.links) ? data.links : [];
    if (!links.length) {
      box.innerHTML = `<p class="agent-links__empty">База не связала это утверждение ни с чем.</p>`;
      box.dataset.loaded = "1";
      return;
    }
    const contradictions = links.filter(link => link.link_type === "contradicts").length;
    box.innerHTML = `
      <p class="agent-links__head">
        ${links.length} ${plural(links.length, "связь", "связи", "связей")}${
          contradictions ? ` · ${contradictions} ${plural(contradictions, "противоречие", "противоречия", "противоречий")}` : ""
        }
      </p>
      <ul class="agent-links__list">
        ${links.map(link => {
          const type = AGENT_LINK_LABEL[link.link_type] || link.link_type;
          // "второе уточняет первое": on an outgoing link the other statement
          // qualifies this one, on an incoming one this qualifies the other.
          const arrow = link.link_type === "qualifies"
            ? (link.direction === "incoming" ? "→ уточняет" : "← уточняется")
            : type;
          return `<li class="agent-links__item agent-links__item--${escapeHtml(link.link_type)}">
            <span class="agent-links__type">${escapeHtml(link.link_type === "qualifies" ? arrow : type)}</span>
            <span class="agent-links__text">${escapeHtml(link.statement || "")}</span>
          </li>`;
        }).join("")}
      </ul>`;
    box.dataset.loaded = "1";
  } catch (error) {
    box.innerHTML = `<p class="agent-waiting">${escapeHtml(error.message)}</p>`;
  }
}

/* ── The conversation ─────────────────────────────────────────────────────
 *
 * The Ask tab is a dialogue, not a form: a welcome screen with prompts sampled
 * by the service, a thread that survives a reload, and a conveyor that shows
 * the verification as it happens. Every turn is still the verified pipeline -
 * the conveyor stages arrive as facts the code established, and the answer is
 * rendered only from what survived the span check.
 */

const CHAT_STEPS = [
  ["search", "поиск по базе"],
  ["draft", "черновик пунктов"],
  ["verify", "проверка по цитатам"]
];
const CHAT_STORE = "radarAgentChat.v1";
let chatSession = "";
let chatTurns = [];
let chatReady = false;

/** The session lives on the reader's device: the owner's decision is that
 *  questions and answers are analysis material without addresses, so the
 *  client is the only place a dialogue is stitched back together. */
function chatPersist() {
  try {
    localStorage.setItem(CHAT_STORE, JSON.stringify({ session: chatSession, turns: chatTurns }));
  } catch (error) {
    /* a private window keeps its conversation to itself */
  }
}

function chatShowWelcome(show) {
  document.getElementById("agentWelcome")?.toggleAttribute("hidden", !show);
  document.getElementById("agentThread")?.toggleAttribute("hidden", show);
}

function agentChatInit() {
  if (chatReady) return;
  chatReady = true;
  if (!chatSession) {
    try { chatSession = crypto.randomUUID(); } catch (error) { chatSession = "s-" + Date.now(); }
  }
  let stored = null;
  try { stored = JSON.parse(localStorage.getItem(CHAT_STORE) || "null"); } catch (error) { stored = null; }
  if (stored && Array.isArray(stored.turns) && stored.turns.length) {
    chatSession = stored.session || chatSession;
    chatTurns = stored.turns.slice(-20);
    const thread = document.getElementById("agentThread");
    if (thread) {
      thread.innerHTML = "";
      chatTurns.forEach((turn, index) => thread.insertAdjacentHTML("beforeend", chatTurnHtml(turn, index)));
    }
    chatShowWelcome(false);
    return;
  }
  agentLoadPrompts();
}

async function agentLoadPrompts() {
  const grid = document.getElementById("promptGrid");
  if (!grid) return;
  grid.innerHTML = `<p class="agent-waiting">Собираю примеры из базы…</p>`;
  try {
    const data = await kbFetch("/prompts");
    const prompts = Array.isArray(data.prompts) ? data.prompts : [];
    grid.innerHTML = prompts.map(prompt => `
      <button class="agent-prompt" type="button">
        <span class="agent-prompt__cat agent-prompt__cat--${escapeHtml(prompt.category || "find")}">${escapeHtml(prompt.hint || "")}</span>
        <p class="agent-prompt__text">${escapeHtml(prompt.text || "")}</p>
      </button>`).join("");
    const note = document.getElementById("poolNote");
    if (note) {
      note.textContent = data.pool
        ? `примеры собраны из пула ${data.pool} запросов · обновляются каждую сессию`
        : "";
    }
    grid.querySelectorAll(".agent-prompt").forEach((card, index) => {
      card.addEventListener("click", () => {
        const input = document.getElementById("agentQuestion");
        if (input) input.value = prompts[index] ? prompts[index].text : "";
        agentAsk(input ? input.value.trim() : "");
      });
    });
  } catch (error) {
    grid.innerHTML = `<p class="agent-waiting">${escapeHtml(error.message)}</p>`;
  }
}

function chatRefusalText(answered, evidence) {
  if (answered.refusalReason === "rate_limited_client") {
    return "Слишком много вопросов подряд. Попробуйте через несколько минут.";
  }
  if (answered.refusalReason === "rate_limited_today") {
    return "На сегодня лимит ответов исчерпан. Поиск и разделы работают.";
  }
  return evidence.length
    ? "В базе нет подтверждений, на которых можно построить ответ. Ниже — то, что нашлось рядом."
    : "В базе нет подтверждений, на которых можно построить ответ.";
}

/** The answer card: clauses with their citation chips, the disclosure button,
 *  and the evidence under it - each statement the base actually holds, with
 *  its quote and its source, the same card the Find tab renders. */
function chatTurnHtml(turn, index) {
  const answered = turn.answered || {};
  const evidence = Array.isArray(answered.evidence) ? answered.evidence : [];
  const clauses = Array.isArray(answered.clauses) ? answered.clauses : [];
  const chip = number =>
    `<a class="agent-cite" href="#ev-${index}-${number}">${Number(number) || "?"}</a>`;
  const body = clauses.length
    ? clauses.map(clause => `
        <p class="agent-answer__text">${escapeHtml(clause.text || "")}${
          Array.isArray(clause.evidence) ? clause.evidence.map(chip).join("") : ""
        }</p>`).join("")
    : answered.answer
      ? `<p class="agent-answer__text">${escapeHtml(answered.answer)}</p>`
      : `<p class="agent-answer__text agent-answer__text--refused">${escapeHtml(chatRefusalText(answered, evidence))}</p>`;
  const evidenceId = `ev-${index}`;
  return `
    <div class="agent-q">${escapeHtml(turn.question || "")}
      <span class="agent-q__meta">${escapeHtml(turn.at || "")}</span>
    </div>
    <div class="agent-answer__card">
      <div class="agent-answer__notice">
        <span class="mono">${escapeHtml(answered.machineNotice || "")}</span>
        <span class="agent-spacer"></span>
        <span class="mono agent-when">${evidence.length} ${plural(evidence.length, "утверждение", "утверждения", "утверждений")}</span>
      </div>
      ${body}
      ${chatToolCardsHtml(answered)}
      ${evidence.length ? `
        <div class="agent-answer__levels">
          <button class="agent-level" type="button" data-toggle="${evidenceId}">Доказательства
            <span class="agent-level__count">${evidence.length}</span>
          </button>
        </div>
        <div class="agent-evidence" id="${evidenceId}" hidden>
          ${evidence.map((row, ordinal) =>
            `<div id="ev-${index}-${ordinal + 1}">${agentStatementCard(row, ordinal + 1)}</div>`
          ).join("")}
        </div>` : ""}
    </div>`;
}

/** A tool card is data the base holds, not a model claim, so it carries no
 *  verification and needs none: it is the evidence, shown directly. */
function chatToolCardsHtml(answered) {
  const cards = Array.isArray(answered.toolCards) ? answered.toolCards : [];
  return cards.map(card => chatToolCardHtml(card)).join("");
}

function chatToolCardHtml(card) {
  const type = card && card.type ? card.type : "";
  const data = (card && card.data) || {};
  if (type === "concept") {
    const statements = Array.isArray(data.statements) ? data.statements.length : 0;
    const title = data.title || data.topicKey || "понятие";
    return `
      <details class="agent-tool" open>
        <summary class="agent-tool__head">Карточка понятия: ${escapeHtml(String(title))}
          <span class="agent-tool__count">${statements} ${plural(statements, "утверждение", "утверждения", "утверждений")}</span>
        </summary>
        <div class="agent-tool__body">
          ${Array.isArray(data.statements)
            ? data.statements.slice(0, 5).map(statement => agentStatementCard(statement)).join("")
            : `<pre class="agent-tool__raw">${escapeHtml(JSON.stringify(data, null, 2))}</pre>`}
        </div>
      </details>`;
  }
  if (type === "contradictions") {
    const pairs = Array.isArray(data.pairs) ? data.pairs : [];
    return `
      <details class="agent-tool" open>
        <summary class="agent-tool__head">Противоречия по теме запроса
          <span class="agent-tool__count">${Number(data.total) || pairs.length} пар</span>
        </summary>
        <div class="agent-tool__body">${pairs.slice(0, 5).map(pair => `
          <div class="agent-versus">
            <div class="agent-versus__side"><p class="agent-versus__claim">${escapeHtml(pair.first_statement || "")}</p></div>
            <div class="agent-versus__side agent-versus__side--contra"><p class="agent-versus__claim">${escapeHtml(pair.second_statement || "")}</p></div>
          </div>`).join("") || `<p class="agent-waiting">Пар не нашлось.</p>`}
        </div>
      </details>`;
  }
  if (type === "gaps" || type === "observatory") {
    const label = type === "gaps" ? "Карта пробелов" : "Обсерватория";
    const rows = Array.isArray(data[type]) || Array.isArray(data.gaps) || Array.isArray(data.observatory)
      ? (data[type] || data.gaps || data.observatory)
      : [];
    return `
      <details class="agent-tool" open>
        <summary class="agent-tool__head">${label}<span class="agent-tool__count">${rows.length}</span></summary>
        <div class="agent-tool__body">
          ${rows.slice(0, 5).map(row =>
            `<p class="agent-hit__text">${escapeHtml(typeof row === "string" ? row : row.statement || row.title || JSON.stringify(row))}</p>`
          ).join("") || `<p class="agent-waiting">Пусто.</p>`}
        </div>
      </details>`;
  }
  return "";
}

function chatWorkHtml() {
  return `
    <div class="agent-work">
      ${CHAT_STEPS.map(([step, label], index) => `
        ${index ? '<span class="agent-work__arrow">→</span>' : ""}
        <span class="agent-work__step" data-step="${step}"><span class="dot"></span>${label}</span>
      `).join("")}
    </div>`;
}

function chatWorkAdvance(work, stage) {
  const step = work.querySelector(`[data-step="${stage.step}"]`);
  if (!step || !stage.done) return;
  step.classList.remove("is-now");
  step.classList.add("is-done");
  // An arrow sits between two steps, so the neighbour to light up is the next
  // node carrying `data-step`, not the next sibling - which is the arrow.
  const steps = Array.from(work.querySelectorAll("[data-step]"));
  const next = steps[steps.indexOf(step) + 1];
  if (next) next.classList.add("is-now");
}

/** Read the SSE frames the service sends: `event: name` + `data: json`, one
 *  blank line between frames. The stream answers or errors; both are frames. */
async function chatStreamTurn(question, work) {
  const response = await fetch(`${KB}/chat/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question, admission: agentState.admission, session: chatSession })
  });
  if (!response.ok || !response.body) throw new Error(`служба базы знаний ответила ${response.status}`);
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let result = null;
  let failed = null;
  for (;;) {
    const chunk = await reader.read();
    if (chunk.done) break;
    buffer += decoder.decode(chunk.value, { stream: true });
    let boundary = buffer.indexOf("\n\n");
    while (boundary !== -1) {
      const frame = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      const eventLine = frame.split("\n").find(line => line.startsWith("event: "));
      const dataLine = frame.split("\n").find(line => line.startsWith("data: "));
      if (!eventLine || !dataLine) { boundary = buffer.indexOf("\n\n"); continue; }
      const event = eventLine.slice(7).trim();
      const payload = JSON.parse(dataLine.slice(6));
      if (event === "stage") chatWorkAdvance(work, payload);
      if (event === "result") result = payload;
      if (event === "error") failed = payload;
      boundary = buffer.indexOf("\n\n");
    }
  }
  if (failed) throw new Error(failed.error || "поток оборвался");
  if (!result) throw new Error("поток оборвался без ответа");
  return result;
}

/** The plain JSON endpoint with the stages replayed: the same turn when the
 *  stream cannot be read (an old proxy, a strict corporate network). */
async function chatJsonTurn(question, work) {
  const answered = await kbFetch("/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question, admission: agentState.admission, session: chatSession })
  });
  (answered.stages || []).forEach(stage => chatWorkAdvance(work, stage));
  return answered;
}

async function agentAsk(question) {
  const thread = document.getElementById("agentThread");
  if (!thread || agentState.busy || !question) return;
  agentState.busy = true;
  chatShowWelcome(false);
  const now = new Date();
  const at = `${String(now.getHours()).padStart(2, "0")}:${String(now.getMinutes()).padStart(2, "0")}`;
  thread.insertAdjacentHTML("beforeend", `
    <div class="agent-q">${escapeHtml(question)}<span class="agent-q__meta">${at}</span></div>`);
  const work = document.createElement("div");
  work.innerHTML = chatWorkHtml();
  const workNode = work.firstElementChild;
  thread.appendChild(workNode);
  workNode.querySelector('[data-step="search"]')?.classList.add("is-now");
  workNode.scrollIntoView({ behavior: "smooth", block: "center" });
  const input = document.getElementById("agentQuestion");
  if (input) input.value = "";
  let answered = null;
  try {
    answered = await chatStreamTurn(question, workNode);
  } catch (error) {
    try {
      answered = await chatJsonTurn(question, workNode);
    } catch (fallback) {
      workNode.remove();
      thread.insertAdjacentHTML("beforeend",
        `<p class="agent-waiting">${escapeHtml(fallback.message || error.message)}</p>`);
      agentState.busy = false;
      return;
    }
  }
  workNode.remove();
  const turn = { question, at, answered };
  chatTurns.push(turn);
  chatPersist();
  thread.insertAdjacentHTML("beforeend", chatTurnHtml(turn, chatTurns.length - 1));
  thread.lastElementChild?.scrollIntoView({ behavior: "smooth", block: "center" });
  agentState.busy = false;
}

/** Levels are free: the disclosure button only unhides evidence that arrived
 *  with the answer, so the thread is delegated here once, not per turn. */
document.getElementById("agentThread")?.addEventListener("click", event => {
  // A footnote whose target sits in a hidden block goes nowhere: open the
  // evidence first, and let the anchor do its own scrolling afterwards.
  const cite = event.target.closest(".agent-cite");
  if (cite) {
    const card = cite.closest(".agent-answer__card");
    const block = card?.querySelector(".agent-evidence");
    if (block && block.hidden) {
      block.hidden = false;
      card.querySelector(".agent-level[data-toggle]")?.classList.add("is-open");
    }
    return;
  }
  const button = event.target.closest(".agent-level[data-toggle]");
  if (!button) return;
  const block = document.getElementById(button.dataset.toggle);
  if (block) block.hidden = !block.hidden;
  button.classList.toggle("is-open", !block?.hidden);
});

document.getElementById("morePrompts")?.addEventListener("click", agentLoadPrompts);

document.getElementById("newDialog")?.addEventListener("click", () => {
  chatTurns = [];
  chatPersist();
  const thread = document.getElementById("agentThread");
  if (thread) thread.innerHTML = "";
  chatShowWelcome(true);
  agentLoadPrompts();
  window.scrollTo({ top: 0, behavior: "smooth" });
});

/* The microphone is the browser's own, when it has one: the Web Speech API
 *  needs no dependency and no server, and where it is absent the button stays
 *  a button rather than becoming an error. */
document.getElementById("agentMic")?.addEventListener("click", () => {
  const recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  const mic = document.getElementById("agentMic");
  if (!recognition || !mic) return;
  if (mic.classList.contains("is-listening")) { mic.classList.remove("is-listening"); return; }
  const listener = new recognition();
  listener.lang = "ru-RU";
  listener.interimResults = false;
  listener.onresult = event => {
    const input = document.getElementById("agentQuestion");
    if (input && event.results && event.results[0]) input.value = event.results[0][0].transcript;
  };
  listener.onend = () => mic.classList.remove("is-listening");
  listener.onerror = () => mic.classList.remove("is-listening");
  mic.classList.add("is-listening");
  listener.start();
});

function plural(n, one, few, many) {
  const mod10 = n % 10;
  const mod100 = n % 100;
  if (mod10 === 1 && mod100 !== 11) return one;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return few;
  return many;
}

function agentFindFilters() {
  const chosen = new URLSearchParams();
  document.querySelectorAll("[data-find-filter]").forEach(control => {
    const value = control.type === "checkbox" ? (control.checked ? control.value : "") : control.value;
    if (value) chosen.set(control.dataset.findFilter, value);
  });
  return chosen;
}

/** Populate the subject filter from the backbone rather than from a hard list.
 *
 * 229 accepted subjects, and they are the owner's to change - a copy in the
 * client would be a second list to keep in step with the first.
 */
async function agentLoadTopicOptions() {
  const select = document.getElementById("agentFindTopic");
  if (!select || select.dataset.loaded) return;
  try {
    const data = await kbFetch("/topics");
    (data.topics || []).forEach(topic => {
      const option = document.createElement("option");
      option.value = topic.topic_key;
      option.textContent = `${topic.title} (${topic.statements})`;
      select.append(option);
    });
    select.dataset.loaded = "1";
  } catch {
    // A subject list that would not load is not a reason to break the search:
    // every other filter still narrows, and the field itself still works.
  }
}

/** UC-01: what was found, and beside each hit which arm found it.
 *
 * Separate from `/ask` on purpose. Asking reaches a paid model behind a limit of
 * ten questions per five minutes; finding a quotation is free and unmetered, and
 * making the reader spend the first to do the second was the gap.
 */
async function agentFind() {
  const box = document.getElementById("agentFindResults");
  const query = document.getElementById("agentFindQuery")?.value.trim();
  if (!box || !query) return;
  box.innerHTML = `<p class="agent-waiting">Ищу по словам и по смыслу…</p>`;
  const parameters = agentFindFilters();
  parameters.set("q", query);
  parameters.set("limit", "25");
  try {
    const data = await kbFetch(`/search?${parameters.toString()}`);
    if (data.error) {
      box.innerHTML = `<p class="agent-waiting">${escapeHtml(data.error)}</p>`;
      return;
    }
    const hits = Array.isArray(data.hits) ? data.hits : [];
    if (!hits.length) {
      box.innerHTML = `<p class="agent-links__empty">Ничего не нашлось. Попробуйте снять фильтры или переформулировать.</p>`;
      return;
    }
    box.innerHTML = `
      <p class="agent-intro">${hits.length} ${plural(hits.length, "утверждение", "утверждения", "утверждений")}.
      Рядом с каждым видно, какая рука его нашла: по словам, по смыслу или обеими.</p>
      ${hits.map((row, index) => agentStatementCard(row, index + 1)).join("")}`;
  } catch (error) {
    box.innerHTML = `<p class="agent-waiting">${escapeHtml(error.message)}</p>`;
  }
}

const AGENT_RELATION_COLOUR = {
  supports: "#1e6e62",
  contradicts: "#a93d22",
  qualifies: "#a96b12",
  related_to: "#6b6880",
  about: "#2b4a75",
  mentions: "#a96b12"
};

const AGENT_ENTITY_LABEL = {
  organisation: "организация",
  person: "человек",
  platform: "платформа",
  standard: "стандарт",
  industry: "отрасль",
  role: "роль",
  risk: "риск",
  control: "контроль",
  practice: "практика"
};

/** UC-05, the two modes this base can actually draw: a subject and what
 *  stands under it, a statement and what the base linked it to. The other
 *  four modes need relations the schema does not hold yet.
 */
/* ── «Связи»: one neighbourhood, two equal representations ────────────────
 *
 * A drawing is one way to see a neighbourhood, a list is another. The default
 * follows the centre: a subject's statements read as a grouped list, a
 * statement's ties read as a picture. The canvas needs the vendored Cytoscape;
 * where it is absent - an old browser, the console smoke - or the screen is a
 * phone, the list is not a fallback but the whole interface, and it is the
 * accessible one: real buttons, real focus, changes announced.
 */

let linksCanvas = null;

function agentGraphDestroy() {
  if (linksCanvas) {
    try {
      linksCanvas.destroy();
    } catch (error) {
      /* a tab being torn down owes nothing to the canvas */
    }
    linksCanvas = null;
  }
}

const AGENT_NODE_SHAPE = { topic: "rectangle", entity: "diamond", statement: "ellipse" };

function agentGraphRoute(nodeId) {
  const [kind, ...rest] = String(nodeId).split(":");
  const key = rest.join(":");
  const parameter = kind === "topic" ? "topic" : kind === "entity" ? "entity" : "claim";
  return `${parameter}=${encodeURIComponent(key)}`;
}

function agentLinksGraphRender(data, host) {
  if (typeof cytoscape !== "function") return false;
  agentGraphDestroy();
  const nodes = data.nodes || [];
  const edges = data.edges || [];
  linksCanvas = cytoscape({
    container: host,
    elements: [
      ...nodes.map(node => ({
        data: { id: node.id, label: node.label, kind: node.kind, centre: node.id === data.centre }
      })),
      ...edges.map((edge, index) => ({
        data: {
          id: `e${index}`,
          source: edge.from,
          target: edge.to,
          relation: edge.relation,
          layer: edge.layer || "structural",
          explanation: edge.explanation || ""
        }
      }))
    ],
    style: [
      {
        selector: "node",
        style: {
          // Three kinds of thing, three shapes and colours, as before: colour
          // alone would leave the picture unreadable to anybody who does not
          // separate blue from amber.
          shape: ele => AGENT_NODE_SHAPE[ele.data("kind")] || "ellipse",
          "background-color": ele => (ele.data("centre") ? "#1f242a"
            : ele.data("kind") === "topic" ? "#2b4a75"
            : ele.data("kind") === "entity" ? "#a96b12" : "#ffffff"),
          "border-width": 2,
          "border-color": ele => (ele.data("kind") === "statement" && !ele.data("centre")
            ? "#1f242a" : "#e6e8e7"),
          width: 30,
          height: 30,
          label: "data(label)",
          "text-wrap": "wrap",
          "text-max-width": 130,
          "font-size": 10,
          color: "#1f242a",
          "text-valign": "bottom",
          "text-margin-y": 8
        }
      },
      {
        selector: "edge",
        style: {
          // Structural ties are the base's own plumbing - grey, quiet. Authorial
          // ties are the model's suggestions - coloured, and never to be read
          // as an established part of the canon.
          width: ele => (ele.data("relation") === "contradicts" ? 2 : 1.4),
          "line-color": ele => (ele.data("layer") === "authorial"
            ? AGENT_RELATION_COLOUR[ele.data("relation")] || "#a96b12"
            : "#c9cdcf"),
          "curve-style": "haystack",
          "haystack-radius": 0.4,
          opacity: 0.7
        }
      },
      { selector: "edge:selected", style: { width: 3, opacity: 1 } }
    ],
    layout: {
      // A subject's neighbourhood is a bundle of same-shaped statements: a grid
      // keeps them countable. Ties around a statement have a shape of their own.
      name: String(data.centre || "").startsWith("topic:") ? "grid" : "cose",
      nodeDimensionsIncludeLabels: true,
      animate: false,
      padding: 30
    }
  });
  linksCanvas.on("tap", "node", event => {
    agentLoadGraph(agentGraphRoute(event.target.id()));
  });
  linksCanvas.on("tap", "edge", event => {
    const line = document.getElementById("agentGraphEdge");
    const spoken = event.target.data("explanation") || event.target.data("relation") || "";
    if (line && spoken) line.textContent = spoken;
  });
  return true;
}

/** The list is the neighbourhood as real HTML: focusable rows, countable
 *  groups, and the "+N" that says how much more a drawn neighbour itself
 *  holds. It renders for every centre; the canvas is the addition. */
function agentLinksListHtml(data) {
  const nodes = (data.nodes || []).filter(node => node.id !== data.centre);
  const centre = (data.nodes || []).find(node => node.id === data.centre);
  const titles = { topic: "Темы", entity: "Имена", statement: "Утверждения" };
  const groups = ["topic", "entity", "statement"].map(kind => {
    const rows = nodes.filter(node => node.kind === kind);
    if (!rows.length) return "";
    return `<div class="agent-links__group">
      <h4 class="agent-section">${titles[kind]}</h4>
      <ul class="agent-links__rows">${rows.map(node => `
        <li><button class="agent-links__row" type="button"
            data-graph-node="${escapeHtml(node.kind)}:${escapeHtml(node.key)}">
          <span class="agent-links__rowtext">${escapeHtml(node.label)}</span>${
            node.hiddenNeighborCount
              ? `<span class="agent-links__more mono" title="всего под этим узлом, вместе с хроникой рынка">+${Number(node.hiddenNeighborCount)}</span>`
              : ""
          }</button></li>`).join("")}
      </ul></div>`;
  }).join("");
  return `
    <div class="agent-links__head">${centre ? escapeHtml(centre.label) : ""}</div>
    ${groups || `<p class="agent-links__empty">Соседей у этого узла нет.</p>`}`;
}

function agentGraphLegend(edges) {
  const name = { supports: "подтверждает", contradicts: "противоречит", qualifies: "уточняет", related_to: "связанное", about: "тема", mentions: "называет" };
  const seen = [...new Set((edges || []).map(edge => edge.relation))];
  return seen.map(relation => {
    // The legend is built only from relations actually present, and says which
    // of them are the model's suggestions: a grey line is the base's plumbing,
    // a coloured one is a claim about knowledge nobody has confirmed.
    const present = (edges || []).find(edge => edge.relation === relation) || {};
    const structural = present.layer !== "authorial";
    return `<span class="agent-graph__key${structural ? " agent-graph__key--structural" : ""}">
      <i style="background:${structural ? "#c9cdcf" : AGENT_RELATION_COLOUR[relation] || "#a96b12"}"></i>${escapeHtml(name[relation] || relation)}
    </span>`;
  }).join("");
}

async function agentLoadGraph(target, options = {}) {
  const box = document.getElementById("agentGraphBody");
  if (!box) return;
  const select = document.getElementById("agentGraphTopic");
  if (select && !select.dataset.loaded) {
    try {
      const data = await kbFetch("/topics");
      (data.topics || []).forEach(topic => {
        const option = document.createElement("option");
        option.value = topic.topic_key;
        option.textContent = `${topic.title} (${topic.statements})`;
        select.append(option);
      });
      select.dataset.loaded = "1";
    } catch {
      // The picker is a convenience; a statement can still be opened from a card.
    }
  }
  const picked = document.getElementById("agentGraphEntity");
  if (picked && !picked.dataset.loaded) {
    try {
      const data = await kbFetch("/entities?limit=80");
      (data.entities || []).forEach(entity => {
        const option = document.createElement("option");
        option.value = entity.entity_id;
        option.textContent = `${entity.canonical_name} — ${AGENT_ENTITY_LABEL[entity.entity_type] || entity.entity_type} (${entity.statements})`;
        picked.append(option);
      });
      picked.dataset.loaded = "1";
    } catch {
      // A name list that would not load still leaves the subject picker working.
    }
  }
  const query = target
    || (picked?.value ? `entity=${encodeURIComponent(picked.value)}` : "")
    || (select?.value ? `topic=${encodeURIComponent(select.value)}` : "");
  if (!query) {
    box.innerHTML = `<p class="agent-intro">Выберите тему — и база покажет, что под ней стоит
    и чем эти утверждения связаны между собой. Из карточки любого утверждения сюда же
    ведёт кнопка «Что база связала с этим».</p>`;
    return;
  }
  box.innerHTML = `<p class="agent-waiting">Собираю связи…</p>`;
  try {
    const data = await kbFetch(`/graph?${query}&limit=40`);
    if (!data.centre) {
      box.innerHTML = `<p class="agent-links__empty">Такого узла в базе нет.</p>`;
      return;
    }
    agentGraphDestroy();
    const legend = document.getElementById("agentGraphLegend");
    if (legend) legend.innerHTML = agentGraphLegend(data.edges);
    // How much of the neighbourhood this is, said out loud: a thousand-statement
    // subject drawn as thirty nodes must never read as the whole subject.
    const meta = data.meta || {};
    const policy = {
      "most-recent": "показаны самые свежие",
      "most-recent-knowledge": "показаны самые свежие из знания; хроника рынка сюда не входит",
      "link-limit": "показана часть связей",
      "all-neighbours": "показаны все соседи"
    }[meta.selectionPolicy] || "";
    const edgeLine = document.getElementById("agentGraphEdge");
    if (edgeLine) edgeLine.textContent = "";
    const metaLine = document.getElementById("agentGraphMeta");
    if (metaLine) {
      metaLine.textContent = meta.truncated
        ? `Показано ${meta.returnedNeighborCount} из ${meta.totalNeighborCount} соседей — ${policy}.`
        : `Соседей: ${meta.returnedNeighborCount || (data.nodes || []).length - 1}. ${policy}.`;
    }
    const machineProposed = (data.edges || []).some(edge => edge.layer === "authorial");
    box.innerHTML = `
      ${machineProposed
        ? `<p class="agent-links__notice">Цветные связи предложила машина; владелец базы их не подтверждал. Серые линии — устройство базы, а не утверждения о знании.</p>`
        : ""}
      ${agentLinksListHtml(data)}`;
    // The canvas is the addition, not the requirement: a phone, an old browser
    // and the console smoke all get the list, and the list is complete.
    const host = document.getElementById("agentGraphCanvas");
    const width = document.documentElement?.clientWidth || 1200;
    const isSubject = String(data.centre).startsWith("topic:");
    const wantCanvas = Boolean(host)
      && (options.forceCanvas || (!isSubject && width >= 640))
      && (data.nodes || []).length <= 41;
    const drawn = wantCanvas && agentLinksGraphRender(data, host);
    if (host) host.hidden = !drawn;
    if (host && isSubject && !options.forceCanvas) {
      box.insertAdjacentHTML("afterbegin",
        `<button class="agent-links__graphbtn" type="button" data-force-graph="${escapeHtml(query)}">показать эти ${meta.returnedNeighborCount || (data.nodes || []).length - 1} связей графом</button>`);
    }
  } catch (error) {
    box.innerHTML = `<p class="agent-waiting">${escapeHtml(error.message)}</p>`;
  }
}

/** UC-11: where the base disagrees with itself, both sides at once. */
async function agentLoadContradictions() {
  const box = document.getElementById("agentContradictions");
  if (!box || box.dataset.loaded) return;
  box.innerHTML = `<p class="agent-waiting">Загрузка разногласий…</p>`;
  try {
    const data = await kbFetch("/contradictions?limit=40");
    const pairs = Array.isArray(data.pairs) ? data.pairs : [];
    box.innerHTML = `
      <p class="agent-intro">Пары, которые база считает несовместимыми: об одном предмете
      сказано разное. Это находка, а не поломка — и решает её читатель, а не машина.
      Всего таких пар ${data.total}, ниже ${pairs.length} самых свежих.</p>
      ${pairs.map(pair => `
        <div class="agent-clash">
          ${agentStatementCard(agentSide(pair, "first"))}
          <div class="agent-clash__mark" aria-hidden="true">противоречит</div>
          ${agentStatementCard(agentSide(pair, "second"))}
        </div>`).join("")}`;
    box.dataset.loaded = "1";
  } catch (error) {
    box.innerHTML = `<p class="agent-waiting">${escapeHtml(error.message)}</p>`;
  }
}

/** One half of a contradicting pair, in the shape every card already reads. */
function agentSide(pair, side) {
  return {
    claim_id: pair[`${side === "first" ? "from" : "to"}_id`],
    statement: pair[`${side}_statement`],
    quote_text: pair[`${side}_quote`],
    char_start: pair[`${side}_char_start`],
    char_end: pair[`${side}_char_end`],
    source_url: pair[`${side}_source_url`],
    source_title: pair[`${side}_source_title`],
    material_kind: pair[`${side}_material_kind`],
    status: pair[`${side}_status`],
    shown_on: pair[`${side}_shown_on`],
    shown_kind: pair[`${side}_shown_kind`],
    primary_source: pair[`${side}_primary_source`],
    is_retelling: pair[`${side}_is_retelling`],
    valid_until: pair[`${side}_valid_until`]
  };
}

function agentObservatoryQuery() {
  const chosen = new URLSearchParams();
  const since = document.querySelector('[data-observatory="since"]')?.value;
  const kind = document.querySelector('[data-observatory="kind"]')?.value;
  const fresh = document.querySelector('[data-observatory="fresh"]')?.checked;
  if (since) {
    const days = { month: 30, quarter: 92, year: 365 }[since];
    const from = new Date(Date.now() - days * 86400000);
    chosen.set("since", from.toISOString().slice(0, 10));
  }
  if (kind) chosen.set("kind", kind);
  if (fresh) chosen.set("fresh", "1");
  return chosen;
}

async function agentLoadObservatory(options) {
  const box = document.getElementById("agentObservatoryBody");
  if (!box || (box.dataset.loaded && !options?.refresh)) return;
  box.innerHTML = `<p class="agent-waiting">Загрузка хроники…</p>`;
  try {
    const parameters = agentObservatoryQuery().toString();
    const data = await kbFetch(parameters ? `/observatory?${parameters}` : "/observatory");
    const rows = data.observatory || [];
    const byKind = new Map();
    rows.forEach(row => {
      const key = row.material_kind || "прочее";
      if (!byKind.has(key)) byKind.set(key, []);
      byKind.get(key).push(row);
    });
    if (!rows.length) {
      box.innerHTML = `<p class="agent-links__empty">За выбранный период в этих классах ничего нет.</p>`;
      box.dataset.loaded = "1";
      return;
    }
    box.innerHTML = `
      <p class="agent-intro">Срез по классам событий, а не лента: что случилось на рынке — отдельно
      от знания, которое база держит о классе явлений.</p>
      <div class="agent-columns">
        ${[...byKind.entries()].map(([kind, items]) => `
          <section class="agent-column">
            <h3 class="agent-column__head">
              ${escapeHtml(AGENT_KIND_LABEL[kind] || kind)}
              <span class="mono">${items.length}</span>
            </h3>
            ${items.map(row => agentStatementCard(row)).join("")}
          </section>`).join("")}
      </div>`;
    box.dataset.loaded = "1";
  } catch (error) {
    box.innerHTML = `<p class="agent-waiting">${escapeHtml(error.message)}</p>`;
  }
}

async function agentLoadTopics() {
  const box = document.getElementById("agentTopics");
  if (!box || box.dataset.loaded) return;
  box.innerHTML = `<p class="agent-waiting">Загрузка скелета тем…</p>`;
  try {
    const data = await kbFetch("/topics");
    const topics = (data.topics || []).filter(topic => topic.statements > 0);
    box.innerHTML = `
      <p class="agent-intro">Понятие — это элемент скелета тем, а не страница, где оно описано.
      Карточка собирается из утверждений базы.</p>
      <div class="agent-topics">
        ${topics.map(topic => `
          <button class="agent-topic" type="button" data-agent-topic="${escapeHtml(topic.topic_key)}">
            <span class="agent-topic__title">${escapeHtml(topic.title)}</span>
            <span class="mono agent-topic__count">${topic.statements}</span>
            <span class="agent-topic__path">${escapeHtml(topic.path || "")}</span>
          </button>`).join("")}
      </div>
      <div id="agentTopicCard"></div>`;
    box.dataset.loaded = "1";
  } catch (error) {
    box.innerHTML = `<p class="agent-waiting">${escapeHtml(error.message)}</p>`;
  }
}

async function agentOpenTopic(key) {
  const card = document.getElementById("agentTopicCard");
  if (!card) return;
  card.innerHTML = `<p class="agent-waiting">Загрузка карточки…</p>`;
  try {
    const data = await kbFetch(`/topics/${encodeURIComponent(key)}`);
    if (data.error) {
      card.innerHTML = `<p class="agent-waiting">${escapeHtml(data.error)}</p>`;
      return;
    }
    const statements = data.statements || [];
    card.innerHTML = `
      <h3 class="agent-section">${escapeHtml(data.title || key)}</h3>
      <p class="agent-intro mono">${escapeHtml(data.path || "")} · ${statements.length} ${plural(statements.length, "утверждение", "утверждения", "утверждений")}</p>
      ${statements.map(row => agentStatementCard(row)).join("")}`;
    card.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    card.innerHTML = `<p class="agent-waiting">${escapeHtml(error.message)}</p>`;
  }
}

async function agentLoadWiki() {
  const box = document.getElementById("agentWiki");
  if (!box || box.dataset.loaded) return;
  box.innerHTML = `<p class="agent-waiting">Загрузка страниц…</p>`;
  try {
    const data = await kbFetch("/pages");
    const pages = data.pages || [];
    box.innerHTML = `
      <p class="agent-intro">Авторские страницы методики — как есть. Жанр по Diátaxis —
      свойство раздела, а не страницы: страницы не режутся, навигация работает по разделам.</p>
      <div class="agent-topics">
        ${pages.map(page => `
          <button class="agent-topic" type="button" data-agent-page="${escapeHtml(page.relative_path)}">
            <span class="agent-topic__title">${escapeHtml(page.title || page.relative_path)}</span>
            <span class="agent-topic__path">${escapeHtml(page.relative_path)}</span>
            <span class="mono agent-topic__count">${page.chars} знаков</span>
          </button>`).join("")}
      </div>
      <div id="agentPage"></div>`;
    box.dataset.loaded = "1";
  } catch (error) {
    box.innerHTML = `<p class="agent-waiting">${escapeHtml(error.message)}</p>`;
  }
}

async function agentOpenPage(path) {
  const card = document.getElementById("agentPage");
  if (!card) return;
  card.innerHTML = `<p class="agent-waiting">Загрузка страницы…</p>`;
  try {
    const data = await kbFetch(`/pages/${encodeURIComponent(path)}`);
    if (data.error) {
      card.innerHTML = `<p class="agent-waiting">${escapeHtml(data.error)}</p>`;
      return;
    }
    card.innerHTML = `
      <h3 class="agent-section">${escapeHtml(data.title || path)}</h3>
      <p class="agent-intro mono">${escapeHtml(data.signature || "")}</p>
      <article class="card agent-page">${escapeHtml(data.body || "")}</article>`;
    card.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    card.innerHTML = `<p class="agent-waiting">${escapeHtml(error.message)}</p>`;
  }
}

async function agentLoadGaps() {
  const box = document.getElementById("agentGaps");
  if (!box || box.dataset.loaded) return;
  box.innerHTML = `<p class="agent-waiting">Загрузка карты пробелов…</p>`;
  try {
    const data = await kbFetch("/gaps?limit=60");
    const gaps = data.gaps || [];
    box.innerHTML = `
      <p class="agent-intro">Утверждения, которым не нашлось места в скелете тем. Ничего не
      отсеивается и ничего не предлагается автоматически — строка отличает «посмотрели, места нет»
      от «ещё не смотрели».</p>
      ${gaps.map(gap => `
        <article class="card agent-gap">
          <p class="agent-gap__missing">${escapeHtml(gap.missing)}</p>
          <p class="agent-gap__statement">${escapeHtml(gap.statement)}</p>
        </article>`).join("")}`;
    box.dataset.loaded = "1";
  } catch (error) {
    box.innerHTML = `<p class="agent-waiting">${escapeHtml(error.message)}</p>`;
  }
}

function setAgentTab(tab) {
  agentState.tab = tab;
  const panels = {
    ask: "agentAsk",
    find: "agentFind",
    observatory: "agentObservatory",
    graph: "agentGraph",
    contradictions: "agentContradictions",
    topics: "agentTopics",
    wiki: "agentWiki",
    gaps: "agentGaps"
  };
  Object.entries(panels).forEach(([name, id]) => {
    document.getElementById(id)?.toggleAttribute("hidden", name !== tab);
  });
  document.querySelectorAll("[data-agent-tab]").forEach(button => {
    const active = button.dataset.agentTab === tab;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-selected", active ? "true" : "false");
  });
  // Leaving the Links tab tears its canvas down: a hidden canvas keeps its
  // listeners and its memory for nothing.
  if (tab !== "graph") agentGraphDestroy();
  if (tab === "find") agentLoadTopicOptions();
  if (tab === "ask") agentChatInit();
  if (tab === "observatory") agentLoadObservatory();
  if (tab === "graph") agentLoadGraph();
  if (tab === "contradictions") agentLoadContradictions();
  if (tab === "topics") agentLoadTopics();
  if (tab === "wiki") agentLoadWiki();
  if (tab === "gaps") agentLoadGaps();
}

document.getElementById("agentForm")?.addEventListener("submit", event => {
  event.preventDefault();
  const question = document.getElementById("agentQuestion")?.value.trim();
  if (question) agentAsk(question);
});

document.getElementById("agentFindForm")?.addEventListener("submit", event => {
  event.preventDefault();
  agentFind();
});

/* A filter is a new search, not a new page: the reader changes "вид" and expects
 * the list under it to answer, without hunting for a button. */
document.getElementById("agentFindFilters")?.addEventListener("change", () => {
  if (document.getElementById("agentFindQuery")?.value.trim()) agentFind();
});

document.getElementById("agentObservatoryFilters")?.addEventListener("change", () => {
  agentLoadObservatory({ refresh: true });
});

document.getElementById("agentGraphTopic")?.addEventListener("change", () => {
  const entity = document.getElementById("agentGraphEntity");
  if (entity) entity.value = "";
  agentLoadGraph();
});

document.getElementById("agentGraphEntity")?.addEventListener("change", () => {
  const topic = document.getElementById("agentGraphTopic");
  if (topic) topic.value = "";
  agentLoadGraph();
});
