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
  const button = event.target.closest("button");
  if (!button) return;
  if (button.id === "printGazette") {
    printGazette();
    return;
  }
  if (button.dataset.agentTab) {
    setAgentTab(button.dataset.agentTab);
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

const agentState = { tab: "ask", admission: "knowledge", busy: false };

async function kbFetch(path, options) {
  const response = await fetch(`${KB}${path}`, options);
  if (!response.ok && response.status !== 404) {
    throw new Error(`служба базы знаний ответила ${response.status}`);
  }
  return response.json();
}

function agentDate(row) {
  if (!row.shown_on) return "";
  const kind = row.shown_kind === "published" ? "дата публикации" : "дата обнаружения радаром";
  return `${row.shown_on} · ${kind}`;
}

/** The labels the owner requires beside every unit: kind, status, whose claim. */
function agentLabels(row) {
  const parts = [];
  if (row.status) {
    const cls = AGENT_STATUS_CLASS[row.status] || "agent-label--far";
    parts.push(`<span class="agent-label ${cls}"><span class="dot"></span>${escapeHtml(AGENT_STATUS_LABEL[row.status] || row.status)}</span>`);
  }
  if (row.material_kind) {
    parts.push(`<span class="agent-label">${escapeHtml(AGENT_KIND_LABEL[row.material_kind] || row.material_kind)}</span>`);
  }
  if (row.is_retelling && row.primary_source) {
    parts.push(`<span class="agent-label agent-label--retelling">пересказ → ${escapeHtml(row.primary_source)}</span>`);
  }
  return parts.join("");
}

function agentMatchedBy(matched) {
  if (!Array.isArray(matched) || !matched.length) return "";
  return matched
    .map(arm => `<span class="agent-arm agent-arm--${arm === "смысл" ? "meaning" : "words"}">по ${escapeHtml(arm === "смысл" ? "смыслу" : "словам")}</span>`)
    .join("");
}

/** One statement at levels 2, 3 and 4 - labels, quotation with its range, source. */
function agentStatementCard(row, ordinal) {
  const number = ordinal ? `<span class="mono agent-ordinal">[${ordinal}]</span>` : "";
  const range = Number.isInteger(row.char_start) && Number.isInteger(row.char_end)
    ? `<span class="mono agent-range">знаки ${row.char_start}–${row.char_end}</span>`
    : "";
  return `
    <article class="card agent-statement" data-claim="${escapeHtml(row.claim_id)}">
      <div class="agent-statement__labels">
        ${number}${agentLabels(row)}
        <span class="agent-spacer"></span>
        <span class="mono agent-when">${escapeHtml(agentDate(row))}</span>
      </div>
      ${row.statement ? `<p class="agent-statement__text">${escapeHtml(row.statement)}</p>` : ""}
      <blockquote class="agent-quote">
        <p>${escapeHtml(row.quote_text || row.quote || "")}</p>
        <div class="agent-quote__meta">${range}${agentMatchedBy(row.matched_by || row.matchedBy)}</div>
      </blockquote>
      <div class="agent-statement__source">
        <span class="agent-statement__sourcelabel">Источник:</span>
        <a href="${escapeHtml(safeExternalUrl(row.source_url || row.sourceUrl))}" target="_blank" rel="noopener noreferrer">
          ${escapeHtml(row.source_title || row.source_url || row.sourceUrl || "источник")}
        </a>
      </div>
    </article>`;
}

async function agentAsk(question) {
  const box = document.getElementById("agentAnswer");
  if (!box || agentState.busy) return;
  agentState.busy = true;
  box.innerHTML = `<p class="agent-waiting">База читает свои утверждения…</p>`;
  try {
    const answered = await kbFetch("/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question })
    });
    if (answered.error) {
      box.innerHTML = `<p class="agent-waiting">${escapeHtml(answered.error)}</p>`;
      return;
    }
    const evidence = Array.isArray(answered.evidence) ? answered.evidence : [];
    const body = answered.answer
      ? `<p class="agent-answer__text">${escapeHtml(answered.answer)}</p>`
      : `<p class="agent-answer__text agent-answer__text--refused">В базе нет подтверждений, на которых можно построить ответ. Ниже — что нашлось рядом.</p>`;
    box.innerHTML = `
      <div class="agent-answer__card">
        <div class="agent-answer__notice">
          <span class="mono">${escapeHtml(answered.machineNotice || "")}</span>
          <span class="agent-spacer"></span>
          <span class="mono agent-when">${evidence.length} ${plural(evidence.length, "утверждение", "утверждения", "утверждений")}</span>
        </div>
        ${body}
      </div>
      ${evidence.length ? `<h3 class="agent-section">Утверждения под ответом</h3>` : ""}
      ${evidence.map((row, index) => agentStatementCard(row, index + 1)).join("")}`;
  } catch (error) {
    box.innerHTML = `<p class="agent-waiting">${escapeHtml(error.message)}</p>`;
  } finally {
    agentState.busy = false;
  }
}

function plural(n, one, few, many) {
  const mod10 = n % 10;
  const mod100 = n % 100;
  if (mod10 === 1 && mod100 !== 11) return one;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return few;
  return many;
}

async function agentLoadObservatory() {
  const box = document.getElementById("agentObservatory");
  if (!box || box.dataset.loaded) return;
  box.innerHTML = `<p class="agent-waiting">Загрузка хроники…</p>`;
  try {
    const data = await kbFetch("/observatory");
    const rows = data.observatory || [];
    const byKind = new Map();
    rows.forEach(row => {
      const key = row.material_kind || "прочее";
      if (!byKind.has(key)) byKind.set(key, []);
      byKind.get(key).push(row);
    });
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
            ${items.slice(0, 12).map(row => agentStatementCard(row)).join("")}
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
    observatory: "agentObservatory",
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
  if (tab === "observatory") agentLoadObservatory();
  if (tab === "topics") agentLoadTopics();
  if (tab === "wiki") agentLoadWiki();
  if (tab === "gaps") agentLoadGaps();
}

document.getElementById("agentForm")?.addEventListener("submit", event => {
  event.preventDefault();
  const question = document.getElementById("agentQuestion")?.value.trim();
  if (question) agentAsk(question);
});
