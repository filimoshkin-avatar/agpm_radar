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
  const periodTheses = {};
  for (const period of ["7d", "30d"]) {
    const prefix = `Период AgPM · ${period} · `;
    const theses = blocks
      .filter(item => item.title?.startsWith(prefix) && !item.title.endsWith("метаданные"))
      .sort((a, b) => a.title.localeCompare(b.title, "ru"))
      .map(item => {
        const [lead, ...rest] = String(item.text || "").split(/\n\n+/);
        return { lead: lead.trim(), rest: rest.join("\n\n").trim() };
      })
      .filter(item => item.lead && item.rest);
    const metadataBlock = blocks.find(item => item.title === `${prefix}метаданные`);
    let metadata = {};
    if (metadataBlock) {
      try { metadata = JSON.parse(metadataBlock.text); } catch (_error) { metadata = {}; }
    }
    if (theses.length) periodTheses[period] = { ...metadata, theses };
  }
  // The analysis headline is the analysis's own - the issue brief is a
  // different animal and used to stand in its place. `watch_next` is a
  // Legacy field name; on the V2 side that text lives in the `actions` block.
  const explicitTitles = Array.isArray(payload.analysis?.evidenceTitles)
    ? payload.analysis.evidenceTitles
    : null;
  const analysis = {
    headline: payload.analysis?.headline || payload.title || "",
    signal: block("overview"),
    why_agpm: block("signals"),
    watch_next: block("actions"),
    // The LLM chose these titles, in this order, for its analysis; the
    // key-material list stands in only for issues published before the
    // contract carried them.
    evidence_titles: explicitTitles && explicitTitles.length
      ? explicitTitles
      : (payload.materials || []).filter(item => item.keyMaterial).map(item => item.title),
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
    period_theses: periodTheses,
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

/* ── Адрес выпуска ──────────────────────────────────────────────────────
 *
 * Ссылка на выпуск — `/issues/<дата>`. `?date=<дата>` принимается ради
 * уведомлений, которые уже разосланы с ним, и на месте заменяется
 * каноническим адресом. До 05.09.2026 фронт не читал адрес вовсе: суточное
 * уведомление обещало выпуск, а открывался последний, и поделиться выпуском
 * было нечем — календарь менял экран, не адрес.
 *
 * Последний выпуск живёт по `/`, без собственного адреса: ссылка на «сегодня»
 * не должна завтра вести во вчера. Периоды 7 и 30 дней адреса тоже не имеют. */
const ISSUE_PATH = /^\/issues\/(\d{4}-\d{2}-\d{2})\/?$/;
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;
// Последний адрес, который эта страница показала сама. Хэша в нём нет: «#top»
// у логотипа — тоже запись в истории, и без сравнения «назад» с неё сбрасывало
// период на «Выпуск» и выкидывало читателя из «Газеты» на радар, хотя никакого
// выпуска в этом шаге не было.
let shownAddress = null;

function currentAddress() {
  return String(window.location.pathname || "") + String(window.location.search || "");
}

/** Календарная дата, а не только её форма: `2026-02-31` проходит регулярное
 *  выражение и получает от API 400, а 400 — это не «выпуска нет», это бросок,
 *  после которого баннер «API недоступен» переспрашивает тот же адрес каждые
 *  пятнадцать секунд и радар не рисуется никогда. */
function validIssueDate(value) {
  return ISO_DATE.test(value) && dateUtc(value)?.toISOString().slice(0, 10) === value;
}

function routeIssueDate() {
  try {
    const found = ISSUE_PATH.exec(String(window.location.pathname || ""));
    if (found) return validIssueDate(found[1]) ? found[1] : null;
    const asked = new URLSearchParams(String(window.location.search || "")).get("date");
    return asked && validIssueDate(asked) ? asked : null;
  } catch {
    return null; // адреса нет — открывается последний выпуск
  }
}

/** Адрес, за который отвечает переключение выпуска.
 *
 *  `/agent`, `/search` и `/gazettes` Caddy отдаёт намеренно, и синхронизация
 *  выпуска не имеет права стирать адрес, который читателю дали для другого
 *  экрана. Своими считаются только «/», «/?date=…» и «/issues/…». */
function ownedAddress() {
  const pathname = String(window.location.pathname || "");
  if (ISSUE_PATH.test(pathname)) return true;
  if (pathname !== "/") return false;
  const keys = [...new URLSearchParams(String(window.location.search || "")).keys()];
  return keys.length === 0 || (keys.length === 1 && keys[0] === "date");
}

// «Вчера» тоже называет точный выпуск, и адрес у неё выпускный: иначе на экране
// стоял один выпуск, а в строке — другой, и F5 показывал не то, что было видно.
function issuePath() {
  const date = ["issue", "yesterday"].includes(state.period) ? state.issueDate : null;
  return date && date !== latest?.issue?.issue_date ? `/issues/${date}` : "/";
}

function syncIssueAddress(replace = false) {
  try {
    if (!ownedAddress()) return;
    const target = issuePath();
    if (target === currentAddress()) {
      shownAddress = target;
      return;
    }
    window.history[replace ? "replaceState" : "pushState"]({ issueDate: state.issueDate }, "", target);
    shownAddress = target;
  } catch {
    /* адреса нет — экран меняется, строка адреса остаётся */
  }
}

// Кнопка «назад» возвращает выпуск, адрес которого только что ушёл из строки.
window.addEventListener?.("popstate", () => {
  if (!ownedAddress() || currentAddress() === shownAddress) return;
  shownAddress = currentAddress();
  const wanted = routeIssueDate();
  state.period = "issue";
  state.issueDate = wanted && wanted !== latest?.issue?.issue_date ? wanted : null;
  document.querySelectorAll("[data-period]").forEach(btn => btn.classList.toggle("is-active", btn.dataset.period === "issue"));
  if (state.viewMode !== "radar") setViewMode("radar");
  reload().catch(error => apiError(error.message));
});

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

// Declared here rather than beside the rest of the agent code, because
// `setViewMode` reads it and `initViewMode()` calls that during module
// evaluation. A reader whose stored mode is «agent» reached the read while this
// was still a lexical declaration further down the file: the throw aborted the
// module, so nothing after it ever ran and the page came up blank in every
// mode - not only the one that triggered it.
const agentState = { tab: "ask", admission: ["knowledge", "observatory"], busy: false };

// Declared at the top on purpose: a stored agent mode runs `setAgentTab` at
// import time, before the graph section below has initialised - a `let` down
// there is a landmine this page has stepped on before.
let linksCanvas = null;

/* ── Subscription access: a key is a capability, kept on the reader's device ──
 *
 * The dialogue is free; browsing the base is subscribed (owner's decision,
 * 2026-08-24). The interface hides nothing cleverly - without a key the agent
 * mode simply is the conversation, and the server remains the wall either way. */

// Объявлено здесь, а не рядом с остальным кодом базы знаний: `accessInit()`
// работает во время вычисления модуля и по ссылке вида `#key=…` сразу зовёт
// `accessEnter`, а тот строит адрес из этой константы. Ниже по файлу она
// оставалась в мёртвой зоне, ReferenceError глох в catch - и ключ по ссылке
// не срабатывал ни у кого, молча. Третий раз в этом файле, всё та же ловушка.
const KB = "/kb";

const ACCESS_STORE = "radarAccess.v1";
const agentAccess = { key: "", plan: null, expiresAt: null };

function accessApply(valid) {
  document.body.classList.toggle("is-subscribed", valid);
  const button = document.getElementById("agentSubButton");
  const label = document.getElementById("agentSubLabel");
  if (button && label) {
    const until = agentAccess.expiresAt ? String(agentAccess.expiresAt).slice(0, 10) : "";
    label.textContent = valid
      ? `подписка${until ? ` до ${until}` : ""}`
      : "Режим подписки";
  }
  const out = document.getElementById("agentSubOut");
  if (out) out.hidden = !valid;
}

function accessSave() {
  try {
    localStorage.setItem(ACCESS_STORE, JSON.stringify(agentAccess));
  } catch {
    /* a private window keeps its key to itself */
  }
}

function accessLoad() {
  try {
    const stored = JSON.parse(localStorage.getItem(ACCESS_STORE) || "null");
    if (stored && typeof stored.key === "string" && stored.key) {
      agentAccess.key = stored.key;
      agentAccess.plan = stored.plan || null;
      agentAccess.expiresAt = stored.expiresAt || null;
      return true;
    }
  } catch {
    /* nothing stored is nothing to load */
  }
  return false;
}

async function accessEnter(key, message) {
  const trimmed = String(key || "").trim();
  if (!trimmed) return;
  try {
    const response = await fetch(`${KB}/access/validate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key: trimmed })
    });
    const checked = await response.json();
    if (checked && checked.valid) {
      agentAccess.key = trimmed;
      agentAccess.plan = checked.plan || null;
      agentAccess.expiresAt = checked.expiresAt || null;
      accessSave();
      accessApply(true);
      if (message) {
        message.hidden = false;
        message.textContent = "Ключ принят. Вкладки агента — в вашем распоряжении.";
      }
      document.getElementById("agentSubPanel")?.toggleAttribute("hidden", true);
      return;
    }
    if (message) {
      message.hidden = false;
      message.textContent = "Ключ не подошёл — проверьте его или напишите владельцу.";
    }
  } catch (error) {
    if (message) {
      message.hidden = false;
      message.textContent = "Проверить ключ не вышло — попробуйте ещё раз.";
    }
  }
}

function accessInit() {
  accessApply(accessLoad());
  // A key may arrive by link: #key=radar-…  It applies once and leaves the
  // URL. Outside a browser (the console smoke) there is no location to read
  // and none to clean.
  try {
    const match = /^#key=(.+)$/.exec(location.hash || "");
    if (match && match[1]) {
      accessEnter(decodeURIComponent(match[1]));
      history.replaceState(null, "", location.pathname + location.search);
    }
  } catch {
    /* no location, no key-by-link */
  }
}

/* ── Слои, которые обязаны закрываться ────────────────────────────────────
 *
 * Панель подписки, архив газеты, история вопросов — три поповера, и до сих пор
 * каждый закрывался только повторным кликом по своей же кнопке. Читатель,
 * открывший панель случайным кликом по замку, не имел выхода: ни ✕, ни Escape,
 * ни клика мимо. У панели при этом стоит `role="dialog"`, а он обещает и
 * закрытие, и фокус.
 *
 * Один список на все три, а не три обработчика: правило одно — «слой
 * закрывается снаружи», и написанное трижды оно разойдётся. Открытие любого
 * слоя закрывает остальные: два поповера одновременно — это уже не поповеры.
 */
const LAYERS = [
  { node: "agentSubPanel", opener: "agentSubButton", close: () => accessPanel(false) },
  { node: "gazetteArchive", opener: "gazetteIssue", close: () => gazetteArchive(false) },
  { node: "chatHistoryList", opener: "chatHistoryToggle", close: () => chatHistoryToggleOpen(false) },
];

function layerOpen(node) {
  return Boolean(node) && !node.hidden;
}

/** Закрыть все слои, кроме названного. */
function closeLayers(except) {
  LAYERS.forEach(layer => {
    if (layer.node === except) return;
    if (layerOpen(document.getElementById(layer.node))) layer.close();
  });
}

document.addEventListener("keydown", event => {
  if (event.key !== "Escape") return;
  // Голосовой режим — тоже слой, и Escape его отменяет БЕЗ переноса текста:
  // отпускание кнопки означает «беру сказанное», Escape — «передумал».
  if (voice.mode) {
    voiceStop({ commit: false });
    return;
  }
  const open = LAYERS.filter(layer => layerOpen(document.getElementById(layer.node)));
  if (!open.length) return;
  event.preventDefault();
  open.forEach(layer => layer.close());
});

// Клик по газете уходит внутрь рамки, и `document` его не видит: iframe глотает
// событие целиком. Наружу пробивается только одно — окно теряет фокус.
window.addEventListener?.("blur", () => {
  if (document.activeElement?.tagName === "IFRAME") closeLayers();
});

document.addEventListener("pointerdown", event => {
  LAYERS.forEach(layer => {
    const node = document.getElementById(layer.node);
    if (!layerOpen(node)) return;
    const target = event.target;
    // Клик по самому слою или по кнопке, которая его открыла, — не «мимо».
    if (node.contains?.(target)) return;
    if (document.getElementById(layer.opener)?.contains?.(target)) return;
    layer.close();
  });
});

/* The button does not switch anything - it explains, and offers the two doors:
 * a request to the owner, or a key the reader already holds. */
function accessPanel(open) {
  const panel = document.getElementById("agentSubPanel");
  const button = document.getElementById("agentSubButton");
  if (!panel) return;
  const opening = open === undefined ? panel.hidden : open;
  if (opening) closeLayers("agentSubPanel");
  panel.toggleAttribute("hidden", !opening);
  button?.setAttribute("aria-expanded", opening ? "true" : "false");
  // `role="dialog"` обещает фокус: в открытую панель, обратно на кнопку при
  // закрытии. Без этого клавиатура остаётся там, где её застали.
  if (opening) document.getElementById("agentSubKey")?.focus?.();
  else if (document.activeElement && panel.contains?.(document.activeElement)) button?.focus?.();
  if (opening && agentAccess.key) {
    const message = document.getElementById("agentSubMessage");
    if (message) {
      message.hidden = false;
      message.textContent = `Подписка активна${
        agentAccess.expiresAt ? ` до ${String(agentAccess.expiresAt).slice(0, 10)}` : ""
      }.`;
    }
  }
}

document.getElementById("agentSubButton")?.addEventListener("click", () => accessPanel());
document.getElementById("agentSubClose")?.addEventListener("click", () => accessPanel(false));

/* Закрытый пункт рейки не уводит из диалога: макет показывает на нём замок и
 * подсказку, а клик открывает разговор о подписке. Слушатель стоит на самой
 * рейке в фазе перехвата, поэтому делегирование документа до него не доходит. */
document.getElementById("agentRail")?.addEventListener("click", event => {
  const item = event.target?.closest?.("[data-agent-tab]");
  if (!item || item.dataset.agentTab === "ask") return;
  if (document.body.classList.contains("is-subscribed")) return;
  event.stopPropagation();
  event.preventDefault?.();
  // Панель открывается сбоку, и связь с тем, куда человек ткнул, теряется.
  // Короткая вспышка замка связывает причину со следствием.
  item.classList.add("is-denied");
  setTimeout(() => item.classList.remove("is-denied"), 700);
  accessPanel(true);
}, true);

const RAIL_STORE = "radarAgentRail.v1";

function agentRailNarrow(narrow) {
  const rail = document.getElementById("agentRail");
  const grid = document.getElementById("agGrid");
  const toggle = document.getElementById("agentRailToggle");
  if (!rail) return;
  rail.classList.toggle("is-narrow", narrow);
  grid?.classList.toggle("is-narrow", narrow);
  if (toggle) {
    toggle.textContent = narrow ? "»" : "«";
    toggle.setAttribute("aria-expanded", narrow ? "false" : "true");
    toggle.setAttribute("aria-label", narrow ? "Развернуть меню" : "Свернуть меню");
  }
}

document.getElementById("agentRailToggle")?.addEventListener("click", () => {
  const narrow = !document.getElementById("agentRail")?.classList.contains("is-narrow");
  agentRailNarrow(narrow);
  // Ширина рейки — привычка читателя, а не состояние сессии.
  try { localStorage.setItem(RAIL_STORE, narrow ? "1" : "0"); } catch { /* приватное окно */ }
});

try {
  if (localStorage.getItem(RAIL_STORE) === "1") agentRailNarrow(true);
} catch {
  /* нет хранилища - рейка просто разложена */
}

/** Счётчик у пункта рейки. */
function agentRailCount(tab, value) {
  const node = document.querySelector(`[data-rail-count="${tab}"]`);
  if (node) node.textContent = Number.isFinite(Number(value)) ? String(value) : "";
}

/* ── Числа базы: один запрос без ключа ────────────────────────────────────
 *
 * Решение владельца, ADR-0011: счёт объектов базы публичен. Число — не
 * содержание, и без него закрытый пункт меню не говорит читателю ничего:
 * замок без числа не сообщает, чего именно он лишает.
 *
 * Числа из макета (13 876 цитат) не годятся: на 2026-08-25 в базе 11 759, и
 * печатать чужую цифру значит соврать о размере. Поэтому — только то, что
 * ответила служба. Не ответила — строка молчит.
 */
let kbCounts = null;

function ru(value) {
  return Number(value).toLocaleString("ru-RU").replace(/\u00a0/g, " ");
}

/** Одно число сведено из четырёх шагов, и по наведению их видно порознь:
 *  сведение не должно прятать шаг, который падает. */
function kbSyncTitle(chain) {
  const names = {
    perimeter: "материалы радара",
    ingest: "загрузка и разбор",
    knowledge: "чтение и связывание",
    embedding: "векторы",
  };
  return (Array.isArray(chain) ? chain : [])
    .map(step => {
      const when = step.succeeded_at ? new Date(step.succeeded_at) : null;
      const at = when && !Number.isNaN(when.getTime())
        ? when.toISOString().slice(0, 16).replace("T", " ") + " UTC"
        : "ни разу";
      const failed = Number(step.failures_since) || 0;
      return `${names[step.step] || step.step}: ${at}${failed ? ` · падений с тех пор: ${failed}` : ""}`;
    })
    .join("\n");
}

async function kbLoadCounts() {
  if (kbCounts) return kbCounts;
  try {
    const data = await kbFetch("/counts");
    if (!data || !Number.isFinite(Number(data.statements))) return null;
    kbCounts = data;
    kbApplyCounts(data);
    return data;
  } catch {
    // Служба недоступна — числа просто не появятся. Врать вместо них нечем.
    return null;
  }
}

/** «СИНХР. 06:07 UTC» — по самому отставшему шагу цепочки, и служба уже свела
 *  его в одно поле. Пусто, пока хотя бы один шаг ни разу не прошёл: тогда
 *  строка молчит, а не называет время, которого не было.
 *
 *  Дата появляется, когда проход не сегодняшний: «СИНХР. 06:07 UTC» и «СИНХР.
 *  24.08 06:07 UTC» — разные новости, и вторую нельзя выдавать за первую. */
function kbSyncLabel(syncedAt) {
  if (!syncedAt) return "";
  const when = new Date(syncedAt);
  if (Number.isNaN(when.getTime())) return "";
  const time = `${String(when.getUTCHours()).padStart(2, "0")}:${String(when.getUTCMinutes()).padStart(2, "0")}`;
  const today = new Date();
  const sameDay = when.getUTCFullYear() === today.getUTCFullYear()
    && when.getUTCMonth() === today.getUTCMonth()
    && when.getUTCDate() === today.getUTCDate();
  const day = `${String(when.getUTCDate()).padStart(2, "0")}.${String(when.getUTCMonth() + 1).padStart(2, "0")}`;
  return sameDay ? `СИНХР. ${time} UTC` : `СИНХР. ${day} ${time} UTC`;
}

function kbApplyCounts(counts) {
  const statements = Number(counts.statements) || 0;
  const topics = Number(counts.topics) || 0;
  const line = `${ru(statements)} ${pluralRu(statements, "УТВЕРЖДЕНИЕ", "УТВЕРЖДЕНИЯ", "УТВЕРЖДЕНИЙ")}`
    + ` · ${ru(topics)} ${pluralRu(topics, "ТЕМА", "ТЕМЫ", "ТЕМ")}`;
  const synced = kbSyncLabel(counts.syncedAt);

  const cell = document.getElementById("tickerBase");
  if (cell) {
    cell.hidden = false;
    cell.textContent = `БАЗА ${line}`;
  }
  const status = document.getElementById("agentBaseStatus");
  if (status) {
    status.innerHTML = `<i aria-hidden="true"></i>БАЗА: ${escapeHtml(line)}`
      + (synced ? ` · ${escapeHtml(synced)}` : "");
    if (synced) status.title = kbSyncTitle(counts.chain);
  }

  // Каждому пункту рейки — своё число, включая закрытые: замок должен
  // говорить, чего читатель лишён.
  agentRailCount("find", counts.statements);
  agentRailCount("observatory", counts.observatory);
  agentRailCount("graph", counts.links);
  agentRailCount("topics", counts.topics);
  agentRailCount("wiki", counts.pages);
  agentRailCount("contradictions", counts.contradictions);
  agentRailCount("gaps", counts.gaps);
}

/* Строка вопроса в шапке: Enter уводит в диалог и спрашивает там же. */
document.getElementById("headerAsk")?.addEventListener("keydown", event => {
  if (event.key !== "Enter") return;
  const question = String(event.target?.value || "").trim();
  if (!question) return;
  event.preventDefault?.();
  event.target.value = "";
  setAgentTab("ask");
  agentAsk(question);
});

/* Архив номеров газеты: один номер сегодня, поповер — на вырост. */
/* ── Газета приходит из базы, а не из разметки ──────────────────────────
 *
 * До 05.09.2026 номера были файлами внутри приложения: пять штук к сентябрю,
 * и каждый новый требовал правки в трёх списках — разметке, матчере Caddy и
 * `_BUNDLED_GAZETTE_ISSUES` — плюс выката всего приложения. Издательский путь
 * для этого и существует: номер публикуется кандидатом, его файлы лежат под
 * `/gazettes/<период>/` с проверкой хэша каждого по базе, а `/api/gazettes`
 * говорит, какие номера есть и какой из них последний.
 *
 * Номер грузится при первом входе в раздел, а не у каждого читателя на старте:
 * `src` в разметке означал, что триста килобайт газеты качает и тот, кто её не
 * открывает. */
const GAZETTE_MONTHS = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
  "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"];
const GAZETTE_MONTHS_SHORT = ["янв", "фев", "мар", "апр", "мая", "июн",
  "июл", "авг", "сен", "окт", "ноя", "дек"];
const GAZETTE_MONTHS_IN = ["январе", "феврале", "марте", "апреле", "мае", "июне",
  "июле", "августе", "сентябре", "октябре", "ноябре", "декабре"];
let gazetteIssues = null;
let gazetteLoading = null;

function gazetteMonthIndex(period) {
  const month = Number(String(period || "").slice(5, 7));
  return month >= 1 && month <= 12 ? month - 1 : -1;
}

function gazetteMonthLabel(period) {
  const index = gazetteMonthIndex(period);
  return index < 0 ? String(period || "") : `${GAZETTE_MONTHS[index]} ${String(period).slice(0, 4)}`;
}

function gazetteShortDate(publishedAt) {
  const when = dateUtc(publishedAt);
  if (!when) return "";
  return `${when.getUTCDate()} ${GAZETTE_MONTHS_SHORT[when.getUTCMonth()]}`;
}

/** Следующий номер — месяц после последнего, включая переход через год. */
function gazetteNextMonth(period) {
  const index = gazetteMonthIndex(period);
  return index < 0 ? "" : GAZETTE_MONTHS_IN[(index + 1) % 12];
}

function gazetteRowHtml(item, ordinal, current) {
  const month = gazetteMonthLabel(item.period);
  const short = gazetteShortDate(item.publishedAt);
  const sub = [short, "Том I", `№ ${ordinal}`].filter(Boolean).join(" · ");
  return `<button class="gazette-archive__row${current ? " is-current is-viewed" : ""}" type="button"
          aria-pressed="${current ? "true" : "false"}"
          data-gazette-issue="${escapeHtml(item.period)}"
          data-gazette-url="${escapeHtml(item.url)}"
          data-gazette-month="${escapeHtml(month)}"
          data-gazette-number="№ ${ordinal}"
          data-gazette-title="${escapeHtml(item.title || "")}">
      <span class="gazette-archive__names">
        <span class="gazette-archive__month">${escapeHtml(month)}</span>
        <span class="gazette-archive__sub mono">${escapeHtml(sub)}</span>
      </span>
      ${current ? `<span class="gazette-archive__tag mono">ТЕКУЩИЙ</span>` : ""}
    </button>`;
}

function renderGazetteArchive(items) {
  const rows = document.getElementById("gazetteArchiveRows");
  const foot = document.getElementById("gazetteArchiveFoot");
  if (!rows) return;
  if (!items.length) {
    rows.innerHTML = `<div class="gazette-archive__foot">Опубликованных номеров пока нет.</div>`;
    if (foot) foot.textContent = "";
    return;
  }
  // Список приходит от новых к старым, поэтому номер выпуска считается с конца.
  rows.innerHTML = items
    .map((item, index) => gazetteRowHtml(item, items.length - index, index === 0))
    .join("");
  if (foot) {
    const next = gazetteNextMonth(items[0].period);
    foot.textContent = next
      ? `Архив пополняется по мере выхода номеров — следующий выпуск в ${next}.`
      : "Архив пополняется по мере выхода номеров.";
  }
  const frame = document.querySelector(".gazette-frame");
  const first = items[0];
  if (frame && first.url && frame.getAttribute("src") !== first.url) {
    frame.setAttribute("src", first.url);
    if (first.title) frame.setAttribute("title", first.title);
  }
  syncGazetteTopStatus();
}

async function loadGazettes() {
  if (gazetteIssues) return gazetteIssues;
  if (gazetteLoading) return gazetteLoading;
  gazetteLoading = (async () => {
    const rows = document.getElementById("gazetteArchiveRows");
    if (rows) rows.innerHTML = `<div class="gazette-archive__foot">Загружаю архив…</div>`;
    try {
      const payload = await getJson("/api/gazettes?limit=100");
      gazetteIssues = Array.isArray(payload.items) ? payload.items : [];
      renderGazetteArchive(gazetteIssues);
      return gazetteIssues;
    } catch (error) {
      gazetteLoading = null;
      if (rows) {
        rows.innerHTML = `<div class="gazette-archive__foot">Архив недоступен: ${escapeHtml(error.message)}</div>`;
      }
      const status = document.getElementById("gazetteTopStatus");
      if (status) status.textContent = "АРХИВ НЕДОСТУПЕН";
      return [];
    }
  })();
  return gazetteLoading;
}

function gazetteArchive(open) {
  const archive = document.getElementById("gazetteArchive");
  const button = document.getElementById("gazetteIssue");
  if (!archive) return;
  const opening = open === undefined ? archive.hidden : open;
  if (opening) closeLayers("gazetteArchive");
  archive.toggleAttribute("hidden", !opening);
  button?.setAttribute("aria-expanded", opening ? "true" : "false");
}

document.getElementById("gazetteIssue")?.addEventListener("click", () => gazetteArchive());

/* Выбор выпуска из архива: рамка перезагружается, шапка называет выбранный номер.
 * «ТЕКУЩИЙ» — свойство последнего выпуска, оно не переезжает вместе с просмотром. */
function openGazetteIssue(row) {
  const url = row.dataset.gazetteUrl;
  const frame = document.querySelector(".gazette-frame");
  if (frame && url && frame.getAttribute("src") !== url) {
    frame.setAttribute("src", url);
    if (row.dataset.gazetteTitle) {
      frame.setAttribute("title", row.dataset.gazetteTitle);
    }
    const month = document.querySelector("#gazetteIssue b");
    if (month) month.textContent = row.dataset.gazetteMonth || "";
    document.querySelectorAll("[data-gazette-issue]").forEach(item => {
      item.classList.toggle("is-viewed", item === row);
      // Подсветка — единственный признак просматриваемого выпуска, а фон её
      // держит на 1.11:1: без aria-pressed выбор не виден ни скринридеру, ни
      // тому, кто не различает эти два бежевых.
      item.setAttribute("aria-pressed", item === row ? "true" : "false");
    });
  }
  gazetteArchive(false);
}

/* Статус в шапке газеты говорит о последнем выпуске — и берётся из архива,
 * а не дублируется строкой: обновился архив — обновился статус. */
function syncGazetteTopStatus() {
  const row = document.querySelector("[data-gazette-issue].is-current");
  const status = document.getElementById("gazetteTopStatus");
  if (!row || !status) return;
  const button = document.querySelector("#gazetteIssue b");
  if (button && !button.textContent) button.textContent = row.dataset.gazetteMonth || "";
  const next = gazetteNextMonth(row.dataset.gazetteIssue);
  const parts = [
    "ТЕКУЩИЙ НОМЕР:",
    row.dataset.gazetteNumber || "",
    "·",
    (row.dataset.gazetteMonth || "").toUpperCase(),
  ];
  if (next) parts.push("· СЛЕДУЮЩИЙ —", next.toUpperCase());
  status.textContent = parts.filter(Boolean).join(" ");
}

/* Лого — наверх текущего раздела: раздел не меняем, только прокрутку. */
document.querySelector(".brand")?.addEventListener("click", event => {
  event.preventDefault();
  window.scrollTo?.(0, 0);
});


document.getElementById("agentSubEnter")?.addEventListener("click", () => {
  accessEnter(
    document.getElementById("agentSubKey")?.value,
    document.getElementById("agentSubMessage")
  );
});

document.getElementById("agentSubKey")?.addEventListener("keydown", event => {
  if (event.key === "Enter") document.getElementById("agentSubEnter")?.click();
});

document.getElementById("agentSubOut")?.addEventListener("click", () => {
  agentAccess.key = "";
  agentAccess.plan = null;
  agentAccess.expiresAt = null;
  accessSave();
  accessApply(false);
  const message = document.getElementById("agentSubMessage");
  if (message) {
    message.hidden = false;
    message.textContent = "Вы вышли из подписки. Диалог с агентом продолжает работать.";
  }
  if (state.viewMode === "agent") setAgentTab("ask");
});

function setViewMode(mode) {
  state.viewMode = VIEW_MODES.includes(mode) ? mode : "radar";
  document.body.classList.toggle("is-gazette", state.viewMode === "gazette");
  document.body.classList.toggle("is-agent", state.viewMode === "agent");
  // Кольцо обходит дни только на «Радаре»: в других режимах его никто не
  // видит, а таймер продолжал идти.
  if (state.viewMode !== "radar" && ringTimer) {
    clearInterval(ringTimer);
    ringTimer = null;
  } else if (state.viewMode === "radar" && !ringTimer && state.period === "30d") {
    startRingTicker();
  }
  // Шапка макета v3 держит имя радара во всех режимах: имя агента стоит над
  // его собственной колонкой, а инструменты справа меняются по режиму.
  if (state.viewMode !== "agent") {
    document.getElementById("agentSubPanel")?.toggleAttribute("hidden", true);
  }
  // A free reader lands in the conversation - it is their whole interface.
  // Subscribers land there too; their difference is what else they can open.
  // Deferred a microtask: a stored agent mode runs this at import time, and
  // `setAgentTab` reaches declarations the module has not initialised yet.
  //
  // The opening tab is marked active in the markup, so nothing else would run
  // its loader: the conversation needs its own start, whoever the reader is.
  if (state.viewMode === "agent") {
    const opening = document.body.classList.contains("is-subscribed") ? agentState.tab : "ask";
    queueMicrotask(() => setAgentTab(opening));
  }
  document.getElementById("gazetteView")?.toggleAttribute("hidden", state.viewMode !== "gazette");
  document.getElementById("agentView")?.toggleAttribute("hidden", state.viewMode !== "agent");
  // Кадр газеты мог загрузиться, пока раздел был скрыт: событие `load` тогда
  // прошло до подписки, и высоту надо взять сейчас.
  if (state.viewMode === "gazette") {
    loadGazettes().catch(error => console.warn("Radar gazettes unavailable", error));
    fitGazetteFrame();
  }
  document.querySelectorAll("[data-view-mode]").forEach(button => {
    const active = button.dataset.viewMode === state.viewMode;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-pressed", active ? "true" : "false");
  });
}

function initViewMode() {
  accessInit();
  // Перезагрузка всегда открывает радар наверху: прошлый режим не тянется,
  // а браузеру запрещается восстанавливать старую позицию прокрутки.
  if (typeof history !== "undefined" && "scrollRestoration" in history) {
    history.scrollRestoration = "manual";
  }
  window.scrollTo?.(0, 0);
  setViewMode("radar");
}

/** Газета — один длинный лист, а не окно со своей полосой прокрутки: рамка
 *  растёт под содержимое, как бумага в макете. Кадр свой (`allow-same-origin`),
 *  поэтому высоту можно спросить; где нельзя - остаётся высота из таблицы. */
function fitGazetteFrame() {
  const frame = document.querySelector(".gazette-frame");
  if (!frame) return;
  try {
    const height = frame.contentDocument?.documentElement?.scrollHeight;
    if (height) frame.style.height = `${height + 8}px`;
  } catch {
    /* чужой origin высоту не отдаёт - пусть работает CSS */
  }
}

document.querySelector(".gazette-frame")?.addEventListener("load", fitGazetteFrame);

function printGazette() {
  const frame = document.querySelector(".gazette-frame");
  const fallback = () => {
    // Печатается открытый номер, а не тот, с которого страница начиналась:
    // архив меняет `src` рамки, и запасной путь обязан идти за ним.
    // Печатается открытый номер; если ни одного не открыто, печатать нечего.
    const source = frame?.getAttribute("src");
    const printWindow = source ? window.open(source, "_blank", "noopener") : null;
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

/** Нижняя кромка липких шапок — граница, ниже которой начинается видимое.
 *
 *  Шапка в «Радаре» одна, в «Агенте» их две: общая и шапка колонки, стоящая
 *  на полке под ней. Высота считается по факту, а не константой: константа
 *  была одна, и «в начало» уводило вопрос под вторую шапку. */
function stickyBottom() {
  let bottom = 0;
  for (const selector of [".topbar", ".agent-main__head"]) {
    const head = document.querySelector(selector);
    // Скрытый раздел не занимает места: `offsetParent` у него пуст.
    if (!head || head.offsetParent === null) continue;
    const shelf = parseFloat(window.getComputedStyle?.(head)?.top) || 0;
    bottom = Math.max(bottom, shelf + (head.getBoundingClientRect?.().height || 0));
  }
  return bottom;
}

/** Единственный способ подвинуть страницу: окном, а не узлом.
 *
 * Дизайн-система запрещает штатный метод прокрутки к элементу, и не из
 * вкусовщины: он тянет ближайшего прокручиваемого предка, а в режиме «Агент»
 * им оказывается колонка, не страница. Здесь считается координата и едет окно.
 * Где окна нет (консольный смоук) — не едет никуда. */
function scrollToNode(node, place = "start") {
  try {
    const box = node?.getBoundingClientRect?.();
    if (!box) return;
    const height = window.innerHeight || 800;
    // §5 гасит анимацию везде, и прокрутка не исключение: явный `behavior`
    // в JS сильнее, чем `scroll-behavior` в таблице стилей, поэтому спрашивать
    // приходится здесь.
    const still = window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches;
    // Композер прилипает к нижней кромке окна. «В конец» — это конец узла над
    // ним, а не под ним: иначе свежий ответ уезжает под строку вопроса ровно
    // настолько, насколько она высока.
    const composer = document.getElementById("agentComposer");
    const inset = place === "end" && composer && !composer.hidden
      ? (composer.getBoundingClientRect?.().height || 0)
      : 0;
    const top = box.top + (window.scrollY || 0);
    const target = place === "end" ? top + box.height - height + inset + 16
      : place === "center" ? top - height / 2 + box.height / 2
      : top - stickyBottom() - 19;
    window.scrollTo?.({ top: Math.max(0, target), behavior: still ? "auto" : "smooth" });
  } catch {
    /* нет окна — нечего листать */
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

/* Сводка выпуска живёт в тикере: те же числа, что и раньше стояли плиткой,
 * только строкой на графите — раскладка макета v3. Идентификаторы прежние,
 * менялось место, а не смысл. */
function updateSummary(stats, materials) {
  setText("viewed", stats.viewed || 0);
  setText("included", stats.included || 0);
  setText("cut", stats.cut || 0);
  document.getElementById("perimeters").innerHTML = `<span class="near-text">Б ${stats.near || 0}</span> · <span class="mid-text">С ${stats.mid || 0}</span> · <span class="far-text">Д ${stats.far || 0}</span>`;
  setText("nearShare", stats.included ? `${Math.round((stats.near || 0) / stats.included * 100)}%` : "0%");
  setText("includedShare", stats.viewed ? `${Math.round((stats.included || 0) / stats.viewed * 100)}%` : "0%");
  setText("allChip", stats.included || 0);
  setText("nearChip", stats.near || 0);
  setText("midChip", stats.mid || 0);
  setText("farChip", stats.far || 0);
  renderFooterMethod(stats);
  renderSparkline();
}

/** Методика в подвале — числами выпуска, как в макете: сколько посмотрели,
 *  сколько включили, сколько отсекли. Те же данные, что и в тикере. */
function renderFooterMethod(stats) {
  const issueNumber = latest?.issue?.issue_number;
  setText("footerMethodLabel", issueNumber ? `МЕТОДИКА ВЫПУСКА ${issueNumber}` : "МЕТОДИКА ОТБОРА");
  const node = document.getElementById("footerMethod");
  if (!node) return;
  node.innerHTML = [
    `<span>ПРОСМОТРЕНО ${stats.viewed || 0} → ВКЛЮЧЕНО ${stats.included || 0}</span>`,
    // Всё, что не вошло: редакционный отброс Legacy (дубли, реклама, шум),
    // материалы вне 30-дневного окна V2 и исключённые вручную. Число —
    // `viewed − included`, и подпись обязана называть все его причины.
    `<span>ОТСЕЧЕНО ${stats.cut || 0} · РЕДАКЦИЯ, ОКНО 30 ДНЕЙ, ВРУЧНУЮ</span>`,
    `<span>ПЕРИМЕТРЫ: Б ${stats.near || 0} · С ${stats.mid || 0} · Д ${stats.far || 0}</span>`,
  ].join("");
}

function renderSparkline() {
  const max = Math.max(1, ...timeseries.map(row => Number(row.included) || 0));
  const selected = activeIssueDate();
  document.getElementById("sparkline").innerHTML = timeseries.slice(-30).map((row, index, arr) => {
    const value = Number(row.included) || 0;
    const height = Math.max(2, Math.round(value / max * 12));
    const classes = [index === arr.length - 1 ? "is-last" : "", row.stat_date === selected ? "is-selected" : ""].filter(Boolean).join(" ");
    return `<span class="${classes}" style="height:${height}px" title="${fmtDate(row.stat_date, true)} · ${value}"></span>`;
  }).join("");
}

function renderTheses(materials) {
  const selectedPayload = state.issueDate ? issueCache.get(state.issueDate) : issueCache.get(latest?.issue?.issue_date);
  const selectedIssue = selectedPayload?.issue || latest?.issue;
  const selectedLlmTheses = selectedPayload?.issue_llm_theses;
  const periodResult = latest?.period_theses?.[state.period] || null;
  const periodTheses = periodResult?.theses || [];
  const issueLlmTheses = selectedLlmTheses?.status === "success" && selectedLlmTheses?.theses?.length ? selectedLlmTheses.theses : [];
  const generated = periodTheses.length ? periodTheses : issueLlmTheses.length ? issueLlmTheses : selectedIssue?.theses?.length ? selectedIssue.theses : [
    { lead: "Операционная агентность требует governance.", rest: "В материалах чаще всего появляются контроль доступа, workflow, трассировка и корпоративная эксплуатация." },
    { lead: "Близкий периметр остаётся управленческим фильтром.", rest: "PMO-сценарии важны там, где агент помогает статусу, риску, поручению или портфельной видимости." },
    { lead: "Дата публикации отделена от даты обнаружения.", rest: "Материал живёт в архиве по реальной дате первоисточника, а не по дате выпуска радара." },
    { lead: "Agent-washing остаётся шумом.", rest: "Публично показываем отобранный слой и агрегаты отсечения, полный список шума остаётся внутренним." },
  ];
  setText("thesesTitle", thesesTitle());
  const shown = generated.slice(0, 4);
  const issueNumber = selectedIssue?.issue_number;
  setText("thesesNote", [
    issueNumber ? `выпуск ${issueNumber}` : "",
    `${shown.length} ${pluralRu(shown.length, "тезис", "тезиса", "тезисов")}`,
    periodResult?.status === "fallback"
      ? "резервный текст: LLM недоступна"
      : periodResult?.model
        ? `LLM: ${periodResult.model}`
        : "",
  ].filter(Boolean).join(" · "));
  document.getElementById("theses").innerHTML = shown.map((item, index) => {
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
  // Заголовок называет день, который реально показан: сегодня, вчера — или дата выбранного выпуска.
  const sonarDay = state.period === "yesterday"
    ? "вчера"
    : state.issueDate && state.issueDate !== latest?.issue?.issue_date
      ? fmtDate(state.issueDate, true)
      : "сегодня";
  document.getElementById("radarTitle").textContent = `Сонар · ${sonarDay}`;
  root.innerHTML = renderSonarWidget(visible);
}

/* Сонар: три кольца, засечки по сторонам света, подписи периметров, луч 8 с и
 * блипы, вспышка которых догоняет луч отрицательной задержкой. Геометрия — та,
 * что работала до редизайна; из палитры дизайн-системы взяты только цвета. */
function renderSonarWidget(materials) {
  return `<div class="radar-widget">
    <svg viewBox="0 0 240 240" class="radar-svg radar-sonar" role="img" aria-label="Сонар: материалы на трёх кольцах">
      <circle cx="120" cy="120" r="34" fill="none" stroke="#e6e2d4" stroke-width="1"></circle>
      <circle cx="120" cy="120" r="64" fill="none" stroke="#e6e2d4" stroke-width="1"></circle>
      <circle cx="120" cy="120" r="94" fill="none" stroke="#d9d4c5" stroke-width="1"></circle>
      <line x1="120" y1="26" x2="120" y2="20" stroke="#c9c4b4" stroke-width="1"></line>
      <line x1="214" y1="120" x2="220" y2="120" stroke="#c9c4b4" stroke-width="1"></line>
      <line x1="120" y1="214" x2="120" y2="220" stroke="#c9c4b4" stroke-width="1"></line>
      <line x1="26" y1="120" x2="20" y2="120" stroke="#c9c4b4" stroke-width="1"></line>
      <text x="120" y="82" text-anchor="middle" font-size="9" paint-order="stroke" stroke="#fdfcf9" stroke-width="3">близкий</text>
      <text x="120" y="52" text-anchor="middle" font-size="9" paint-order="stroke" stroke="#fdfcf9" stroke-width="3">средний</text>
      <text x="120" y="22" text-anchor="middle" font-size="9" paint-order="stroke" stroke="#fdfcf9" stroke-width="3">дальний</text>
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
        <circle cx="150" cy="150" r="48" fill="none" stroke="#eeeadd" stroke-width="1"></circle>
        <g>${rows.map((row, index) => ringBar(row, index, rows.length, maxTotal)).join("")}</g>
        <g id="ringHl" class="ring-highlight" data-anim>
          <path d="M150 150 L134.9 10.8 A140 140 0 0 1 165.1 10.8 Z" fill="rgba(169,107,18,0.10)"></path>
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
    // Search looks at everything the card shows as text and nothing else, so a hit
    // is always visible on the card that produced it. The server-side search of the
    // period modes applies the same rule in apps/api/public_data.py.
    if (!cardSearchText(item).includes(state.q.toLowerCase())) return false;
  }
  return true;
}

// Everything a card shows as text, in one place: renderCard() draws it and the
// search reads it, so the two cannot drift apart. The description and takeaway
// are the model's when its analysis succeeded, the rule-based ones otherwise.
function cardView(item) {
  const llmSucceeded = item.llm_summary && item.llm_summary.status === "success";
  return {
    host: sourceHost(item.url) || item.source_name || "источник",
    date: materialDateLabel(item),
    signal: signalMeta(item),
    description: (llmSucceeded ? item.llm_summary.short_text : "") || item.brief || item.summary || "",
    takeaway: (llmSucceeded ? item.llm_summary.agpm_angle : "") || item.agpm_takeaway || "",
    tags: (item.rubrics || []).slice(0, 3).map(id => ({ id, name: rubricNames[id] || id })),
  };
}

function cardSearchText(item) {
  const view = cardView(item);
  return [view.signal.label, view.host, view.date, item.title, view.description, view.takeaway, ...view.tags.map(tag => tag.name)]
    .join(" ")
    .toLowerCase();
}

function renderColumns(materials) {
  const root = document.getElementById("columns");
  root.classList.toggle("loading", state.loading);
  // Виджет гаснет вместе с колонками: числа в нём те же самые.
  document.getElementById("radarViz")?.classList.toggle("is-loading", state.loading);
  if (state.loading) {
    root.innerHTML = Object.keys(perimeters).map(key => `<section class="column column-${key}"><div class="skeleton"></div><div class="skeleton small"></div><div class="skeleton card-skel"></div><div class="skeleton card-skel"></div></section>`).join("");
    return;
  }
  const visibleKeys = Object.keys(perimeters).filter(key => state.perimeter === "all" || key === state.perimeter);
  root.innerHTML = visibleKeys.map(key => {
    const meta = perimeters[key];
    const rows = materials.filter(item => item.perimeter === key && materialMatches(item));
    const keyCount = rows.filter(row => row.key_material).length;
    return `<section class="column column-${key}">
      <header class="column__head">
        <span class="column__title">${meta.title}</span>
        <span class="column__count">${rows.length}</span>
      </header>
      <div class="column__desc">${meta.desc}</div>
      <div class="cards">${rows.length ? rows.map(renderCard).join("") : '<div class="empty">Материалов по этому фильтру нет.</div>'}</div>
      <footer class="column__foot">${keyCount} ${pluralRu(keyCount, "ключевой", "ключевых", "ключевых")} из ${rows.length} ${pluralRu(rows.length, "включённого", "включённых", "включённых")}</footer>
    </section>`;
  }).join("");
  renderActiveFilter();
}

function renderCard(item) {
  const view = cardView(item);
  const { host, date, signal, description, takeaway } = view;
  const tags = view.tags.map(tag => {
    const tagClass = rubricTagClasses[tag.id] || "tag-default";
    return `<span class="tag ${tagClass}">${escapeHtml(tag.name)}</span>`;
  }).join("");
  return `<article class="card">
    <div class="card__meta">
      <span class="signal ${signal.className}" title="${escapeHtml(signal.title)}">${signal.mark} ${escapeHtml(signal.label)}</span>
      <span>${escapeHtml(host)}</span>
      <span>${escapeHtml(date)}</span>
    </div>
    <h3>${escapeHtml(item.title)}</h3>
    <p>${escapeHtml(description)}</p>
    ${takeaway ? `<div class="takeaway"><b class="takeaway__label">ВЫВОД ДЛЯ AgPM · </b>${escapeHtml(takeaway)}</div>` : ""}
    ${tags ? `<div class="tags">${tags}</div>` : ""}
    <a class="source-link" href="${escapeHtml(safeExternalUrl(item.url))}" target="_blank" rel="noopener">первоисточник →</a>
  </article>`;
}

function signalMeta(item) {
  const strength = item.signal_strength || (item.verdict === "core" ? "strong" : "context");
  const labels = {
    strong: "Сильный сигнал",
    context: "Контекст",
    watch: "Наблюдение",
  };
  const titles = {
    strong: "Материал можно использовать в методической или управленческой повестке почти сразу.",
    context: "Материал важен для понимания среды, но требует перевода в AgPM через интерпретацию.",
    watch: "Ранний или рыночный сигнал: стоит наблюдать, но не делать главным аргументом выпуска.",
  };
  // Знаки макета v3: заполненный треугольник — сильный сигнал, круг — контекст,
  // контур — наблюдение. Типографика, а не иконка: тот же ряд, что и стрелки.
  const marks = { strong: "▲", context: "●", watch: "○" };
  return {
    label: item.signal_label || labels[strength] || labels.strong,
    title: titles[strength] || titles.strong,
    mark: marks[strength] || marks.strong,
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
  const node = document.getElementById(id);
  if (!node) return;
  node.innerHTML = rows.map(row => {
    const value = Number(row[valueKey]) || 0;
    const blockClass = rubricBlockClasses[row.id] || "default";
    const isActive = state.rubrics.includes(row.id);
    const arrow = trendArrow(row);
    // Низкая надёжность гасит стрелку до цвета «ровно»: направление показано,
    // но не выдаёт себя за сигнал, которого в двух окнах ещё нет.
    const deltaClass = [
      arrow === "↘" ? "is-down" : arrow === "→" ? "is-flat" : "",
      row.confidence === "low" ? "is-weak" : "",
    ].filter(Boolean).join(" ");
    const details = rubricTrendDetails(row);
    return `<button class="bar-row bar-row-${blockClass} ${isActive ? "is-active" : ""}" data-rubric-bar="${row.id || ""}" aria-pressed="${isActive ? "true" : "false"}" title="${escapeHtml(details)}">
      <span>${escapeHtml(row[labelKey] || "")}</span>
      <span class="bar-track"><span class="bar-fill" style="width:${Math.max(3, value / max * 100)}%"></span></span>
      <span class="bar-count">${value}</span>
      <span class="bar-delta ${deltaClass}">${arrow}</span>
    </button>`;
  }).join("");
  // Подпись принадлежит той панели, в которой лежат столбики: #rubricatorNote —
  // заголовок «Рубрикатора», и период динамики там врал про соседнюю сетку счётов.
  const periodLabel = state.period === "7d"
    ? "7 дней к предыдущим 7"
    : state.period === "30d" ? "30 дней к предыдущим 30" : "выпуск к предыдущему";
  setText("trendRange", rows.length ? `${rows.length} ${pluralRu(rows.length, "рубрика", "рубрики", "рубрик")} · ${periodLabel}` : "");
}

/* «Материалы в разрезе по дням» и календарь — макет v3.
 *
 * Столбик дня — стопка Б/С/Д снизу вверх, выходные приглушены. Ось внизу
 * называет четыре даты и сегодняшний счёт; SVG-график с двумя шкалами уступил
 * место плотной стопке, потому что дизайн-система просит данные, а не рамку. */
function renderTrendPanels() {
  const rows = timeseries.slice(-30);
  renderTrendStack(rows);
  renderHeatmap(rows);
}

function renderTrendStack(rows) {
  const host = document.getElementById("trendBars");
  if (!host) return;
  const maxTotal = Math.max(1, ...rows.map(row => ringTotal(row)));
  // Самый высокий день занимает 64px из 74 — плотность макета. Потолок в
  // 4px на материал держит тихую неделю: три материала не должны выглядеть
  // как тридцать только потому, что больше не с чем сравнивать.
  const scale = Math.min(4, 64 / maxTotal);
  const bar = (value, name) => value
    ? `<i class="${name}" style="height:${(value * scale).toFixed(1)}px"></i>`
    : "";
  const columns = rows.map(row => {
    const total = ringTotal(row);
    const classes = ["trend-day", isWeekend(row.stat_date) ? "is-weekend" : "", total ? "" : "is-empty"]
      .filter(Boolean).join(" ");
    const stack = total
      ? bar(Number(row.near) || 0, "near") + bar(Number(row.mid) || 0, "mid") + bar(Number(row.far) || 0, "far")
      : `<i style="height:2px"></i>`;
    return `<span class="${classes}" title="${fmtDate(row.stat_date)} · ${row.near || 0}/${row.mid || 0}/${row.far || 0}">${stack}</span>`;
  }).join("");
  const marks = [0, Math.floor(rows.length / 4), Math.floor(rows.length / 2), Math.floor(rows.length * 3 / 4)]
    .filter((value, index, all) => all.indexOf(value) === index && rows[value])
    .map(index => `<span>${escapeHtml(fmtDate(rows[index].stat_date, true))}</span>`)
    .join("");
  const last = rows.at(-1);
  host.innerHTML = `<div class="trend-stack">${columns}</div>
    <div class="trend-axis">${marks}${last && latest?.issue?.issue_number ? `<span class="is-today">выпуск · ${latest.issue.issue_number}</span>` : ""}</div>`;
  setText("trendDaysNote", rows.length ? `${ringRangeLabel(rows)} · Б/С/Д` : "Б/С/Д");
}

function renderHeatmap(rows) {
  const host = document.getElementById("heatmap");
  if (!host) return;
  const max = Math.max(1, ...rows.map(row => Number(row.included) || 0));
  const selected = activeIssueDate();
  // Календарь встаёт по неделям: понедельник — первый столбец, пустые клетки
  // до первого дня держат сетку, иначе шапка ПН…ВС врёт.
  const lead = rows.length ? (weekdayIndex(rows[0].stat_date) + 6) % 7 : 0;
  const blanks = Array.from({ length: lead }, () =>
    `<span class="heatmap-day heatmap-day--blank" aria-hidden="true"></span>`).join("");
  host.innerHTML = blanks + rows.map(row => {
    const included = Number(row.included) || 0;
    const alpha = Math.min(0.85, 0.06 + included / max * 0.75);
    const isSelected = ["issue", "yesterday"].includes(state.period) && row.stat_date === selected;
    const light = !isSelected && alpha > 0.45 ? " is-light" : "";
    // Выбранный день — янтарный, и его заливку задаёт таблица стилей: инлайновый
    // фон перебил бы её и оставил выпуск неотличимым.
    const paint = isSelected ? "" : ` style="background:rgba(43,74,117,${alpha.toFixed(2)})"`;
    return `<button class="heatmap-day${isSelected ? " is-active" : ""}${light}" data-issue-day="${row.stat_date}"${paint} title="${fmtDate(row.stat_date)} · ${included}" aria-label="Показать выпуск за ${fmtDate(row.stat_date)}">${dayOfMonth(row.stat_date)}</button>`;
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
      cells.push(rubricCell(row));
    });
  });
  rubrics.filter(row => !rubricGroups.some(group => group.ids.includes(row.id))).forEach(row => {
    cells.push(rubricCell(row));
  });
  cells.push(`<div class="rubric-cell rubric-help"><strong>Множественный выбор</strong><span>Рубрики сужают выдачу по принципу «хотя бы одна из выбранных».</span></div>`);
  document.getElementById("rubricator").innerHTML = cells.join("");
  const countLabel = state.period === "7d"
    ? "счёт за 7 дней"
    : state.period === "30d" ? "счёт за 30 дней" : "счёт по выпуску";
  setText("rubricatorNote", rubrics.length ? `${rubrics.length} ${pluralRu(rubrics.length, "рубрика", "рубрики", "рубрик")} · ${countLabel}` : "");
}

function rubricCell(row) {
  const isActive = state.rubrics.includes(row.id);
  return `<button class="rubric-cell ${isActive ? "is-active" : ""}" type="button" data-rubric="${row.id}" aria-pressed="${isActive ? "true" : "false"}">
    <strong>${escapeHtml(row.title)}</strong>
    <b class="mono">${row.count || 0}</b>
  </button>`;
}

function trendArrow(row) {
  return row.direction === "up" ? "↗" : row.direction === "down" ? "↘" : "→";
}

function rubricTrendDetails(row) {
  const confidence = { low: "низкая", medium: "средняя", high: "высокая" }[row.confidence] || "не определена";
  if (row.previousShare === null || row.previousShare === undefined) {
    return `Материалов: ${row.currentCount || 0}. Сравнивать не с чем: более раннего выпуска нет.`;
  }
  const currentShare = Math.round((Number(row.currentShare) || 0) * 100);
  const previousShare = Math.round((Number(row.previousShare) || 0) * 100);
  return `Было ${row.previousCount || 0}, стало ${row.currentCount || 0}; доля ${previousShare}% → ${currentShare}%; индекс ${Number(row.index || 0).toFixed(1)}; надёжность: ${confidence}.`;
}

function renderFooterSources() {
  const shown = sources.slice(0, 12);
  const rest = Math.max(0, sources.length - shown.length);
  document.getElementById("footerSources").innerHTML = `${shown.map(row => `<span><b title="${escapeHtml(row.name || "")}">${escapeHtml(sourceLabel(row.name))}</b><i class="mono">${row.included || 0}</i></span>`).join("")}${rest ? `<span>+ ${rest} ${pluralRu(rest, "источник", "источника", "источников")}</span>` : ""}`;
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
    const empty = { absent: true, issue: { issue_date: issueDate, theses: [] }, materials: [] };
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
  // `rubrics` живёт на уровне модуля, поэтому пишется только после проверки
  // поколения: опоздавший reload иначе оставлял бы свой период в общем состоянии,
  // не перерисовав экран. Отказ рубрик — не отказ выпуска: панель просто
  // остаётся прежней, а страница рисуется.
  let nextRubrics = null;
  try {
    [materials, , nextRubrics] = await Promise.all([
      loadIssueMaterials(request),
      loadPeriodStats(request.period),
      loadRubrics(request.period, request.issueDate).catch(error => {
        console.warn("Radar rubrics unavailable", error);
        return null;
      }),
    ]);
  } catch (error) {
    if (generation !== reloadGeneration) return;
    state.loading = false;
    throw error;
  }
  if (generation !== reloadGeneration) return;
  if (nextRubrics) rubrics = nextRubrics;
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

async function loadRubrics(period, issueDate) {
  const apiPeriod = ["issue", "yesterday"].includes(period) ? "day" : period;
  const params = { period: apiPeriod, anchor: issueDate };
  const payload = await getJson(`/api/rubrics?${qs(params)}`);
  return payload.rubrics || [];
}

async function init() {
  latest = await getJson("/api/issue/latest");
  issueCache.set(latest.issue.issue_date, {
    issue: latest.issue,
    daily_analysis: latest.daily_analysis,
    issue_llm_theses: latest.issue_llm_theses,
    materials: latest.materials || [],
  });
  const wanted = routeIssueDate();
  if (wanted && wanted !== latest.issue?.issue_date) {
    state.period = "issue";
    state.issueDate = wanted;
  }
  setText("issueDate", issueLabel(activeIssueDate()));
  await reload();
  // Ссылка на выпуск, которого нет: показываем последний и приводим адрес к
  // нему. Пустой экран без единого слова о том, почему он пуст, — хуже.
  if (state.issueDate && issueCache.get(state.issueDate)?.absent) {
    state.issueDate = null;
    await reload();
  }
  syncIssueAddress(true);
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
  const [sourcesResult, publicationTimeseriesResult, issuesResult] = await Promise.allSettled([
    getJson("/api/sources?period=30d"),
    getJson("/api/timeseries?days=30&basis=publication"),
    getJson("/api/issues?limit=5"),
  ]);
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
  renderTrendPanels();
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
  if (button.id === "printGazetteTop") {
    printGazette();
    return;
  }
  if (button.dataset.agentTab) {
    setAgentTab(button.dataset.agentTab);
    return;
  }
  if (button.dataset.agentGraph) {
    const claim = `claim=${encodeURIComponent(button.dataset.agentGraph)}`;
    // Inside the conversation the neighbourhood opens in the thread - the chat
    // is a free reader's whole interface. Elsewhere (a subscriber's tab) it
    // walks the tab as before.
    if (button.closest("#agentThread")) chatGraphTurn(claim);
    else {
      setAgentTab("graph");
      agentLoadGraph(claim);
    }
    return;
  }
  if (button.dataset.agentLinks) {
    agentToggleLinks(button.dataset.agentLinks);
    return;
  }
  if (button.dataset.agentTrail) {
    agentToggleTrail(button.dataset.agentTrail);
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
  if (button.dataset.gazetteIssue) {
    openGazetteIssue(button);
    return;
  }
  if (button.dataset.agentAdmission) {
    const value = button.dataset.agentAdmission;
    const next = agentState.admission.includes(value)
      ? agentState.admission.filter(item => item !== value)
      : [...agentState.admission, value];
    // Хотя бы один источник всегда выбран: последний чип не снимается.
    if (next.length) agentState.admission = next;
    document.querySelectorAll("[data-agent-admission]").forEach(chip => {
      const on = agentState.admission.includes(chip.dataset.agentAdmission);
      chip.classList.toggle("is-active", on);
      // Подсветка — только для глаза; состояние переключателя говорят вслух.
      chip.setAttribute("aria-pressed", on ? "true" : "false");
    });
    return;
  }
  if (button.dataset.viewMode) {
    // Нажатие снизу страницы должно быть заметным: раздел меняется — и виден.
    setViewMode(button.dataset.viewMode);
    window.scrollTo?.(0, 0);
  }
  if (button.dataset.period) {
    state.period = button.dataset.period;
    state.issueDate = button.dataset.period === "yesterday" ? yesterdayIssueDate() : null;
    document.querySelectorAll("[data-period]").forEach(btn => btn.classList.toggle("is-active", btn === button));
    reload();
    syncIssueAddress();
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
  if (button.dataset.issueDay) {
    state.period = "issue";
    state.issueDate = button.dataset.issueDay;
    document.querySelectorAll("[data-period]").forEach(btn => btn.classList.toggle("is-active", btn.dataset.period === "issue"));
    // Ссылка на выпуск может быть нажата и вне радара: сначала показываем радар.
    if (state.viewMode !== "radar") setViewMode("radar");
    reload();
    syncIssueAddress();
    scrollToNode(document.getElementById("columns"));
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

// Тикер показывает размер базы в любом режиме, поэтому счёт спрашивается сразу,
// а не при открытии диалога. Запрос свободен от ключа (ADR-0011) и кэширован
// службой, так что цена ему — один раз за загрузку страницы.
kbLoadCounts();

/* Баннер отказа висел до перезагрузки страницы. Между тем API возвращается сам,
 * и читателю нечего было делать с этой строкой, кроме как перезагрузиться
 * вручную. Теперь он считает вслух и пробует снова, а закрыть его можно. */
const API_RETRY_SECONDS = 15;

function apiError(message) {
  document.querySelector(".api-error")?.remove();
  const banner = document.createElement("div");
  banner.className = "api-error";
  banner.innerHTML = `<span class="api-error__text">API недоступен: ${escapeHtml(message)}</span>
    <span class="api-error__wait mono"></span>
    <button class="api-error__retry" type="button" data-api-retry>повторить сейчас</button>
    <button class="api-error__close" type="button" data-api-close aria-label="Закрыть">✕</button>`;
  document.body.insertAdjacentElement?.("afterbegin", banner);
  let left = API_RETRY_SECONDS;
  const wait = banner.querySelector(".api-error__wait");
  const retry = () => {
    clearInterval(timer);
    banner.remove();
    init().catch(again => apiError(again.message));
  };
  const timer = setInterval(() => {
    left -= 1;
    if (wait) wait.textContent = `повтор через ${left} с`;
    if (left <= 0) retry();
  }, 1000);
  if (wait) wait.textContent = `повтор через ${left} с`;
  banner.addEventListener("click", event => {
    if (event.target?.closest?.("[data-api-retry]")) retry();
    if (event.target?.closest?.("[data-api-close]")) {
      clearInterval(timer);
      banner.remove();
    }
  });
}

init().catch(error => apiError(error.message));

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


async function kbFetch(path, options) {
  // A key, when the reader holds one, travels with every request: the server
  // decides what it opens, the client only asks.
  const headers = new Headers(options?.headers || {});
  if (agentAccess.key) headers.set("Authorization", `Bearer ${agentAccess.key}`);
  const response = await fetch(`${KB}${path}`, { ...options, headers });
  if (response.status === 403) {
    const wall = new Error("раздел доступен по подписке — войдите по ключу");
    wall.subscription = true;
    throw wall;
  }
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
    matchedBy: pick("matched_by", "matchedBy") || [],
    relevance: pick("relevance")
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

/** «Почему найдено» — строка эталона, собранная из того, что база прислала:
 *  какая рука нашла (по словам, по смыслу или обеими) и с каким рангом. Где
 *  ранга нет - его нет и в строке; выдумывать 0,92 этому экрану нельзя. */
function agentWhyFound(row) {
  const arms = Array.isArray(row.matchedBy) ? row.matchedBy : [];
  const how = arms.length ? agentMatchedBy(arms) : "";
  const rank = Number.isFinite(Number(row.relevance))
    ? `<span class="agent-rank">ранг ${Number(row.relevance).toFixed(2).replace(".", ",")}</span>`
    : "";
  if (!how && !rank) return "";
  return `<div class="agent-why"><span class="agent-why__label">почему найдено:</span>${how}${rank}</div>`;
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
        <div class="agent-quote__meta">${range}<span class="agent-verbatim">цитата дословна, проверяется при каждой записи</span></div>
      </blockquote>
      ${agentWhyFound(row)}
      <div class="agent-statement__source">
        <span class="agent-statement__sourcelabel">Источник:</span>
        <a href="${escapeHtml(safeExternalUrl(row.sourceUrl))}" target="_blank" rel="noopener noreferrer">
          ${escapeHtml(row.sourceTitle || row.sourceUrl || "источник")}
        </a>
      </div>
      ${row.claimId ? `<div class="agent-statement__actions">
        <button class="agent-links__toggle" type="button" data-agent-links="${escapeHtml(row.claimId)}">Что агент связал с этим</button>
        <button class="agent-links__toggle" type="button" data-agent-graph="${escapeHtml(row.claimId)}">Показать в графе</button>
        <button class="agent-links__toggle" type="button" data-agent-trail="${escapeHtml(row.claimId)}">Путь до выпуска →</button>
      </div>
      <div class="agent-links" data-agent-links-for="${escapeHtml(row.claimId)}" hidden></div>
      <div class="agent-trail" data-agent-trail-for="${escapeHtml(row.claimId)}" hidden></div>` : ""}
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
  box.innerHTML = agentLoadingHtml("Читаю связи…");
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

/** Последнее звено цепочки доверия: ОТВЕТ → УТВЕРЖДЕНИЕ → ЦИТАТА →
 *  ПЕРВОИСТОЧНИК → **ВЫПУСК**. Материал, который выбрал этот документ, и
 *  выпуск радара, который его показал.
 *
 *  Пусто — это ответ, а не пробел: 46 % утверждений пришли из канона, wiki и
 *  операторского импорта, и в выпуск радара их материал не входил. Так и
 *  сказано словами; пустое место сказало бы «данных нет», что неправда. */
async function agentToggleTrail(claimId) {
  const box = document.querySelector(`[data-agent-trail-for="${CSS.escape(claimId)}"]`);
  if (!box) return;
  if (!box.hidden) { box.hidden = true; return; }
  box.hidden = false;
  if (box.dataset.loaded) return;
  box.innerHTML = agentLoadingHtml("Ищу путь до выпуска…");
  try {
    const data = await kbFetch(`/statement/${encodeURIComponent(claimId)}`);
    const trail = Array.isArray(data.trail) ? data.trail : [];
    box.dataset.loaded = "1";
    if (!trail.length) {
      box.innerHTML = `<p class="agent-trail__none">Материал этого утверждения не входил
        в выпуск радара: он пришёл из канона, wiki или операторского импорта.
        Первоисточник — выше.</p>`;
      return;
    }
    const perimeters = { near: "близкий", mid: "средний", far: "дальний" };
    box.innerHTML = `
      <p class="agent-trail__head mono">путь до выпуска</p>
      ${trail.map(step => {
        const issue = step.issue_number ? `выпуск ${step.issue_number}` : "выпуск";
        const perimeter = perimeters[step.perimeter] || step.perimeter || "";
        return `<div class="agent-trail__step">
          <button class="agent-trail__issue mono" type="button" data-issue-day="${escapeHtml(step.issue_date)}" title="Открыть выпуск в радаре">${escapeHtml(issue)}</button>
          <span class="agent-trail__date mono">${escapeHtml(fmtDate(step.issue_date, true))}</span>
          <span class="agent-trail__material">${escapeHtml(step.material_title || "материал выпуска")}</span>
          <span class="agent-trail__perimeter mono">${escapeHtml(perimeter)}${step.key_material ? " · ключевой" : ""}</span>
        </div>`;
      }).join("")}`;
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
  ["search", "Поиск по базе"],
  ["draft", "Черновик пунктов"],
  ["verify", "Проверка по цитатам"],
  // Служба присылает три стадии; четвёртая — то, ради чего они шли. Она
  // гаснет вместе с конвейером, когда карточка ответа встаёт на его место.
  ["answer", "Ответ"]
];
const CHAT_STORE = "radarAgentChat.v1";
// The server cuts a question at 500 characters (MAX_QUESTION_CHARS); the
// counter says so while there is still something to do about it.
const CHAT_MAX_QUESTION = 500;
let chatSession = "";
let chatTurns = [];
let chatReady = false;
let chatAbort = null;
let chatUnread = 0;
let chatListener = null;
//: Мини-графы ответов живут, пока жива нить. Нить вычищается - их надо снести
//: руками: контейнер исчезает, а слушатели экземпляра остаются.
const chatMinis = new Map();

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

/* ── Follow the bottom only while the reader is already there ─────────────
 *
 * The one scrolling rule a chat owes its reader: never drag somebody who is
 * reading upwards. While they sit at the bottom, new content arrives under
 * their eyes; the moment they scroll up, finished answers are counted in the
 * «вниз» pill instead, and one click brings them back. */

function chatNearBottom() {
  try {
    const page = document.documentElement || document.body;
    return window.innerHeight + window.scrollY >= page.scrollHeight - 140;
  } catch (error) {
    return true;
  }
}

let chatDownLabel = "";

function chatFollowBottom(node, { counts = true, move = true, label = "" } = {}) {
  if (label) chatDownLabel = label;
  if (move && chatNearBottom()) {
    scrollToNode(node, "end");
    return;
  }
  // The reader's own question is not news to them: only a finished answer is
  // worth a number in the pill.
  if (!counts) return;
  chatUnread += 1;
  chatDownSync();
}

function chatDownSync() {
  const pill = document.getElementById("chatDown");
  if (!pill) return;
  pill.hidden = chatUnread === 0;
  const label = document.getElementById("chatDownCount");
  if (label && chatUnread > 0) {
    label.textContent = chatUnread > 1
      ? `${chatUnread} новых внизу`
      : (chatDownLabel || "новый ответ внизу");
  }
  if (chatUnread === 0) chatDownLabel = "";
}

document.getElementById("chatDown")?.addEventListener("click", () => {
  chatUnread = 0;
  chatDownSync();
  scrollToNode(document.getElementById("agentThread")?.lastElementChild, "end");
});

/* ── The dialogue's own map: one collapsible control, a list, a position ────
 *
 * Best practice for a chat's bottom edge: the composer owns it - one compact
 * «вопросы диалога ▾» instead of a chip row that grows with every turn. The
 * list is a real listbox (number, time, question), the current turn is marked
 * by a scroll-spy, and the toggle itself says «вопрос N из M» while the reader
 * is up in the thread, so the way back is always one glance away. */
let chatCurrentTurn = -1;
let chatCiteFlash = null;

function chatShort(text, max) {
  const value = String(text || "");
  return value.length > max ? `${value.slice(0, max - 1)}…` : value;
}

function chatHistoryRows() {
  return chatTurns.map((turn, index) => `
    <button class="chat-history__row${index === chatCurrentTurn ? " is-current" : ""}"
      type="button" role="option" data-history-turn="${index}"
      title="${escapeHtml(turn.question || "")}">
      <span class="mono chat-history__n">${index + 1}</span>
      <span class="chat-history__q">${escapeHtml(chatShort(turn.question, 44))}</span>
      <span class="mono chat-history__at">${escapeHtml(turn.at || "")}</span>
    </button>`).join("");
}

function chatRenderNav() {
  const toggle = document.getElementById("chatHistoryToggle");
  const count = document.getElementById("chatHistoryCount");
  const list = document.getElementById("chatHistoryList");
  if (!toggle) return;
  toggle.hidden = chatTurns.length < 2;
  if (count) count.textContent = chatTurns.length ? `(${chatTurns.length})` : "";
  if (list && !list.hidden) list.innerHTML = chatHistoryRows();
}

function chatHistoryToggleOpen(open) {
  const list = document.getElementById("chatHistoryList");
  const toggle = document.getElementById("chatHistoryToggle");
  if (!list || !toggle) return;
  if (open) closeLayers("chatHistoryList");
  list.hidden = !open;
  toggle.setAttribute("aria-expanded", open ? "true" : "false");
  if (open) list.innerHTML = chatHistoryRows();
}

document.getElementById("chatHistoryToggle")?.addEventListener("click", () => {
  const list = document.getElementById("chatHistoryList");
  chatHistoryToggleOpen(Boolean(list && list.hidden));
});

document.getElementById("chatHistoryList")?.addEventListener("click", event => {
  const row = event.target.closest("[data-history-turn]");
  if (!row) return;
  const turn = document.querySelector(`#agentThread [data-turn="${row.dataset.historyTurn}"]`);
  if (!turn) return;
  // Поворот выше окна: его «центр» — это середина ответа, а не вопрос.
  // История ведёт к вопросу, поэтому позиционируем на начало поворота.
  scrollToNode(turn, "start");
  turn.classList.add("is-flash");
  setTimeout(() => turn.classList.remove("is-flash"), 1600);
  chatHistoryToggleOpen(false);
});

/** The scroll-spy: which turn is on screen right now. Runs inside the same
 *  passive scroll listener the unread pill uses - one listener, both duties. */
function chatUpdatePosition() {
  const thread = document.getElementById("agentThread");
  if (!thread || !chatTurns.length) return;
  const mark = (window.innerHeight || 800) * 0.4;
  let current = chatTurns.length - 1;
  for (const node of thread.querySelectorAll?.("[data-turn]") || []) {
    const top = node.getBoundingClientRect ? node.getBoundingClientRect().top : 0;
    if (top <= mark) {
      const parsed = Number(node.dataset.turn);
      if (Number.isInteger(parsed) && parsed >= 0) current = parsed;
    }
  }
  // The chip answers two things - which turn, and whether the reader is above
  // the bottom - and only the first is `current`. Leaving on an unchanged turn
  // left it showing «вопрос 3 из 5» to somebody already back at the foot.
  const pos = document.getElementById("chatHistoryPos");
  if (pos) {
    const show = !chatNearBottom() && chatTurns.length > 1;
    pos.hidden = !show;
    pos.textContent = show ? `· вопрос ${current + 1} из ${chatTurns.length}` : "";
  }
  if (current === chatCurrentTurn) return;
  chatCurrentTurn = current;
  const list = document.getElementById("chatHistoryList");
  if (list && !list.hidden) {
    list.querySelectorAll("[data-history-turn]").forEach(row => {
      row.classList.toggle("is-current", Number(row.dataset.historyTurn) === current);
    });
  }
}

/* The way up is as owed as the way down: a thread taller than a screen gets
 * both pills, and they never appear together with the welcome screen. */
function chatUpSync() {
  const up = document.getElementById("chatUp");
  if (!up) return;
  try {
    up.hidden = window.scrollY < (window.innerHeight || 800) * 1.2;
  } catch {
    up.hidden = true;
  }
}

document.getElementById("chatUp")?.addEventListener("click", () => {
  try {
    window.scrollTo({ top: 0, behavior: "smooth" });
  } catch {
    /* no window to scroll: nothing to offer */
  }
});

function agentChatInit() {
  if (chatReady) return;
  chatReady = true;
  // Reaching the bottom unaided is the same answer as clicking the pill: the
  // count stood for «there is something below you», and now there is not.
  // Attached on first open rather than at module evaluation - a reader who
  // never opens the chat gets no global listener, and a host without
  // `window.addEventListener` must not take the whole file down with it.
  // Coalesced into one frame: the handler reads the geometry of every turn, and
  // a scroll fires far more often than the screen redraws.
  let scrollPending = false;
  window.addEventListener?.("scroll", () => {
    if (scrollPending) return;
    scrollPending = true;
    const run = () => {
      scrollPending = false;
      if (chatUnread && chatNearBottom()) {
        chatUnread = 0;
        chatDownSync();
      }
      chatUpdatePosition();
      chatUpSync();
    };
    if (typeof requestAnimationFrame === "function") requestAnimationFrame(run);
    else run();
  }, { passive: true });
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
      // A restored session is not a performance: no stagger, no pop - the
      // answers the reader has already read simply stand where they stood.
      let previousDate = null;
      chatTurns.forEach((turn, index) => {
        thread.insertAdjacentHTML("beforeend", chatTurnHtml(turn, index, false, previousDate));
        previousDate = turn.dateLabel || previousDate;
      });
    }
    chatRenderNav();
    chatComposerSync();
    chatShowWelcome(false);
    return;
  }
  // Свежая сессия начинается с приветствия: карточки-примеры и есть вход
  // в диалог. Без этой строки пустая нить показывала пустую страницу.
  chatShowWelcome(true);
  agentLoadPrompts();
  chatComposerSync();
}

/** Ярлык категории примера — словами макета, а не ключом сервиса. */
const AGENT_PROMPT_CATEGORY = {
  find: "поиск",
  concept: "тема",
  contra: "противоречие",
  watch: "обсерватория"
};

async function agentLoadPrompts() {
  const grid = document.getElementById("promptGrid");
  if (!grid) return;
  grid.innerHTML = agentLoadingHtml("Собираю примеры из базы…");
  try {
    const data = await kbFetch("/prompts");
    const prompts = Array.isArray(data.prompts) ? data.prompts : [];
    grid.innerHTML = prompts.map(prompt => {
      const category = prompt.category || "find";
      return `
      <button class="agent-prompt" type="button">
        <span class="agent-prompt__cat agent-prompt__cat--${escapeHtml(category)}">${escapeHtml(AGENT_PROMPT_CATEGORY[category] || category)}</span>
        <p class="agent-prompt__text">${escapeHtml(prompt.text || "")}</p>
        ${prompt.hint ? `<span class="agent-prompt__hint mono">→ ${escapeHtml(prompt.hint)}</span>` : ""}
      </button>`;
    }).join("");
    const note = document.getElementById("poolNote");
    if (note) {
      note.textContent = data.pool
        ? `СЭМПЛ ИЗ ПУЛА ${data.pool} ЗАПРОСОВ · КЛИК ПО КАРТОЧКЕ — СРАЗУ СПРОСИТЬ`
        : "";
    }
    grid.querySelectorAll(".agent-prompt").forEach((card, index) => {
      card.addEventListener("click", () => {
        const input = document.getElementById("agentQuestion");
        if (input) input.value = prompts[index] ? prompts[index].text : "";
        chatComposerSync();
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
  if (answered.refusalReason === "rate_limited_key") {
    return "Дневной предел этого ключа исчерпан. Он вернётся завтра; поиск и разделы работают.";
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
 *  its quote and its source, the same card the Find tab renders.
 *
 *  A fresh turn reveals itself in steps - clauses, then cards, then evidence -
 *  the way a streamed answer does; a restored session skips the theatre and
 *  just is what it is. The verified text arrives whole (the server streams
 *  stages, never an unverified draft), so this is pacing, not pretending. */
function chatTurnHtml(turn, index, fresh = false, previousDate = null) {
  // A dialogue that spans days gets its seams: a quiet separator names the day,
  // the way every messenger a reader already knows does it.
  const daySeparator = turn.dateLabel && turn.dateLabel !== previousDate
    ? `<div class="chat-day"><span>${escapeHtml(turn.dateLabel)}</span></div>`
    : "";
  const answered = turn.answered || {};
  const evidence = Array.isArray(answered.evidence) ? answered.evidence : [];
  const clauses = Array.isArray(answered.clauses) ? answered.clauses : [];
  const chip = number =>
    `<a class="agent-cite" href="#ev-${index}-${number}">${Number(number) || "?"}</a>`;
  const step = order => fresh ? ` style="--i:${order}"` : "";
  // Пункты ответа пронумерованы, как в макете: янтарный «01» слева от опоры.
  // Один пункт номера не получает - нумеровать нечего.
  const numbered = clauses.length > 1;
  const body = clauses.length
    ? clauses.map((clause, order) => `
        <p class="agent-answer__text${numbered ? " agent-answer__text--point" : ""}"${step(order)}>${
          numbered ? `<span class="agent-answer__no">${String(order + 1).padStart(2, "0")}</span>` : ""
        }<span>${escapeHtml(clause.text || "")}${
          Array.isArray(clause.evidence) ? clause.evidence.map(chip).join("") : ""
        }</span></p>`).join("")
    : answered.answer
      ? `<p class="agent-answer__text"${step(0)}>${escapeHtml(answered.answer)}</p>`
      : `<p class="agent-answer__text agent-answer__text--refused"${step(0)}>${escapeHtml(chatRefusalText(answered, evidence))}</p>`;
  const evidenceId = `ev-${index}`;
  // Отказ — не бледный ответ, а другой жанр: серо-фиолетовая кромка вместо
  // янтарной, счёт проверенного вместо счёта утверждений, и «ближайшее, что
  // есть» вместо пунктов. Правило работы, а не ошибка.
  const refused = !clauses.length && !answered.answer;
  if (refused) {
    return `
    ${daySeparator}
    <div class="chat-turn${fresh ? " is-fresh" : ""}" data-turn="${index}">
      ${chatQuestionHtml(turn.question, turn.at, index)}
      <div class="agent-answer__card agent-answer__card--refusal">
        <div class="agent-answer__notice agent-answer__notice--refusal">
          <span>В базе нет подтверждений</span>
          <span class="agent-when">${escapeHtml(answered.machineNotice || "")}</span>
        </div>
        <p class="agent-answer__text agent-answer__text--refused"${step(0)}>${escapeHtml(chatRefusalText(answered, evidence))}</p>
        <p class="agent-answer__text agent-answer__text--rule"${step(1)}>Агент не достраивает ответ догадками — это правило работы, а не ошибка.</p>
        ${evidence.length ? `
          <div class="agent-answer__near">
            <span class="agent-answer__nearlabel mono">ближайшее, что есть</span>
            <div class="agent-evidence" id="${evidenceId}">
              ${evidence.map((row, ordinal) =>
                `<div id="ev-${index}-${ordinal + 1}"${fresh ? ` style="--j:${ordinal}"` : ""}>${agentStatementCard(row, ordinal + 1)}</div>`
              ).join("")}
            </div>
          </div>` : ""}
        <div class="agent-answer__levels">
          <button class="agent-turn__copy" type="button" data-copy-turn="${index}">копировать ответ</button>
        </div>
      </div>
    </div>`;
  }
  return `
    ${daySeparator}
    <div class="chat-turn${fresh ? " is-fresh" : ""}" data-turn="${index}">
      ${chatQuestionHtml(turn.question, turn.at, index)}
      <div class="agent-answer__card">
        <div class="agent-answer__notice">
          <span>${escapeHtml(answered.machineNotice || "")}</span>
          <span class="agent-when">${evidence.length} ${plural(evidence.length, "утверждение", "утверждения", "утверждений")}</span>
        </div>
        ${body}
        ${chatTopicsRow(evidence)}
        ${chatToolCardsHtml(answered)}
        ${evidence.length ? `
          <div class="agent-answer__levels">
            <button class="agent-level" type="button" data-toggle="${evidenceId}">Доказательства
              <span class="agent-level__count">· ${evidence.length}</span>
            </button>
            ${evidence.length >= 2 ? `
              <button class="agent-level" type="button" data-graph-mini="${index}">В графе</button>` : ""}
            <button class="agent-turn__copy" type="button" data-copy-turn="${index}">копировать ответ</button>
            <span class="agent-answer__trail">ОТВЕТ → УТВЕРЖДЕНИЕ → ЦИТАТА → ПЕРВОИСТОЧНИК</span>
          </div>
          <div class="chat-mini" id="mini-${index}" hidden></div>
          <div class="agent-evidence" id="${evidenceId}" hidden>
            ${evidence.map((row, ordinal) =>
              `<div id="ev-${index}-${ordinal + 1}"${fresh ? ` style="--j:${ordinal}"` : ""}>${agentStatementCard(row, ordinal + 1)}</div>`
            ).join("")}
          </div>` : ""}
      </div>
    </div>`;
}

/** Пузырь вопроса макета v3: подпись «ВЫ · 12:41» стоит НАД графитом,
 *  а не внутри него. Один вид и для сохранённого хода, и для текущего. */
function chatQuestionHtml(question, at, index) {
  const copy = Number.isInteger(index) ? ` data-copy-q="${index}" title="нажмите — вопрос скопируется"` : "";
  return `<div class="agent-q"${copy}>
      <span class="agent-q__meta">ВЫ · ${escapeHtml(at || "")}</span>
      <span class="agent-q__bubble">${escapeHtml(question || "")}</span>
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
    const title = data.title || data.topicKey || "тема";
    return `
      <details class="agent-tool" open>
        <summary class="agent-tool__head">Карточка темы: ${escapeHtml(String(title))}
          <span class="agent-tool__count">${statements} ${plural(statements, "утверждение", "утверждения", "утверждений")}</span>
        </summary>
        <div class="agent-tool__body">
          ${Array.isArray(data.statements)
            ? data.statements.slice(0, 5).map(statement => agentStatementCard(statement)).join("")
            : `<pre class="agent-tool__raw">${escapeHtml(JSON.stringify(data, null, 2))}</pre>`}
          <div class="agent-answer__topics">
            ${chatSubTeaser(String(title))}
            <span class="agent-sub__note" hidden>Подписка на получение информации по темам доступна после авторизации.</span>
          </div>
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

/* The conveyor must look alive while it works, and say how long it has been
 * working: dots breathe on the current step, a quiet bar runs under the card,
 * and a timer counts the seconds - «3,4 с» answers «зависло?» before the
 * question is asked. */
function chatWorkHtml() {
  return `
    <div class="agent-work">
      <div class="agent-work__top">
        <span class="agent-work__title">АГЕНТ РАБОТАЕТ</span>
        <span class="agent-work__timer mono" hidden></span>
        <button class="agent-work__stop" type="button" data-stop-run>■ Стоп</button>
      </div>
      <div class="agent-work__steps">
        ${CHAT_STEPS.map(([step, label]) => `
        <span class="agent-work__step" data-step="${step}"><span class="agent-work__mark" aria-hidden="true"><i></i><i></i><i></i></span>${label}</span>
      `).join("")}
      </div>
      <span class="agent-work__bar" aria-hidden="true"><i style="width:10%"></i></span>
    </div>`;
}

/** Starts the conveyor's clock; returns the stop. The timer is stopped on
 *  every exit from a turn - completion, stop, failure - because a clock that
 *  outlives its process is a lie. */
function chatWorkTimer(work) {
  const node = work?.querySelector?.(".agent-work__timer");
  if (!node) return () => {};
  const now = () => (typeof performance !== "undefined" && performance.now) 
    ? performance.now()
    : Date.now();
  const started = now();
  const tick = () => {
    const seconds = (now() - started) / 1000;
    node.textContent = seconds < 60
      ? `${seconds.toFixed(1).replace(".", ",")} с`
      : `${Math.floor(seconds / 60)} мин ${String(Math.floor(seconds % 60)).padStart(2, "0")} с`;
  };
  node.hidden = false;
  tick();
  const timer = setInterval(tick, 100);
  return () => clearInterval(timer);
}

function chatWorkAdvance(work, stage) {
  const step = work.querySelector(`[data-step="${stage.step}"]`);
  if (!step || !stage.done) return;
  step.classList.remove("is-now");
  step.classList.add("is-done");
  // A stage that finishes says what it found: the conveyor is not a spinner,
  // it is the turn's own account of itself.
  if (stage.step === "search" && stage.hits != null) {
    step.insertAdjacentHTML("beforeend",
      ` <span class="agent-work__detail">${stage.hits} ${plural(stage.hits, "находка", "находки", "находок")}${stage.cache ? " · из кэша" : ""}</span>`);
  }
  if (stage.step === "verify") {
    step.insertAdjacentHTML("beforeend",
      ` <span class="agent-work__detail">${stage.passes ? "прошла" : "не прошла"}</span>`);
  }
  // An arrow sits between two steps, so the neighbour to light up is the next
  // node carrying `data-step`, not the next sibling - which is the arrow.
  const steps = Array.from(work.querySelectorAll("[data-step]"));
  const next = steps[steps.indexOf(step) + 1];
  if (next) next.classList.add("is-now");
  const done = steps.filter(node => node.classList.contains("is-done")).length;
  const bar = work.querySelector(".agent-work__bar i");
  if (bar) bar.style.width = `${Math.min(100, done / CHAT_STEPS.length * 100 + 10)}%`;
}

/** Read the SSE frames the service sends: `event: name` + `data: json`, one
 *  blank line between frames. The stream answers or errors; both are frames. */
/* Что уходит в службу за выбором чипов. Одна полка — её имя, обе — «all»
 * (ADR-0012): значение, которое расширяет поиск, потому что его назвали.
 * Пустая строка здесь стояла до 25.08.2026 и означала «неизвестно» —
 * `agent_api._answer_flow` сужал такое до знания, молча и правильно, а горящий
 * чип «хронике рынка» при этом не делал ничего. */
function admissionScope() {
  return agentState.admission.length === 1 ? agentState.admission[0] : "all";
}

async function chatStreamTurn(question, work, signal) {
  const response = await fetch(`${KB}/chat/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question, admission: admissionScope(), session: chatSession }),
    signal
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
async function chatJsonTurn(question, work, signal) {
  const answered = await kbFetch("/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question, admission: admissionScope(), session: chatSession }),
    signal
  });
  (answered.stages || []).forEach(stage => chatWorkAdvance(work, stage));
  return answered;
}

/** The button has two faces: «спросить» when idle, «стоп» while a turn runs.
 *  Stopping aborts the stream - the reader's page, the reader's call. */
function chatComposerBusy(busy) {
  const send = document.getElementById("agentSend");
  if (!send) return;
  send.textContent = busy ? "■ Стоп" : "Спросить";
  send.classList.toggle("is-stop", busy);
  const typed = String(chatInput()?.value || "");
  send.disabled = busy ? false : (!typed.trim() || typed.length > CHAT_MAX_QUESTION);
}

function chatInput() {
  return document.getElementById("agentQuestion");
}

function chatComposerSync() {
  const input = chatInput();
  if (!input) return;
  // The field grows with the question, to a point: a long question is
  // scrollable, the page is not for pushing around by a textarea.
  if (input.scrollHeight) {
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 132)}px`;
  }
  const length = (input.value || "").length;
  const counter = document.getElementById("agentChars");
  if (counter) {
    counter.hidden = length < 400;
    counter.textContent = `${Math.min(length, 999)}/${CHAT_MAX_QUESTION}`;
    counter.classList.toggle("is-over", length > CHAT_MAX_QUESTION);
  }
  if (!agentState.busy) {
    const send = document.getElementById("agentSend");
    if (send) send.disabled = !length || length > CHAT_MAX_QUESTION;
  }
}

function chatSubmit() {
  const input = chatInput();
  const question = String(input?.value || "").trim();
  if (agentState.busy) {
    chatAbort?.abort();
    return;
  }
  // Служба режет вопрос на пятистах знаках. Счётчик об этом говорит с
  // четырёхсот; отправлять то, что заведомо приедет обрезанным, — не надо.
  if (!question || question.length > CHAT_MAX_QUESTION) return;
  agentAsk(question);
}

async function agentAsk(question) {
  const thread = document.getElementById("agentThread");
  if (!thread || agentState.busy || !question) return;
  agentState.busy = true;
  chatAbort = new AbortController();
  chatComposerBusy(true);
  chatShowWelcome(false);
  const now = new Date();
  const at = `${String(now.getHours()).padStart(2, "0")}:${String(now.getMinutes()).padStart(2, "0")}`;
  const dateLabel = now.toLocaleDateString
    ? now.toLocaleDateString("ru-RU", { day: "numeric", month: "long" })
    : "";
  // The running turn is a wrapper: the question appears at once, the conveyor
  // under it, and the finished card replaces the conveyor in the same wrapper.
  const wrapper = document.createElement("div");
  wrapper.className = "chat-turn is-fresh";
  wrapper.dataset.turn = "run";
  wrapper.innerHTML = chatQuestionHtml(question, at);
  thread.appendChild(wrapper);
  const work = document.createElement("div");
  work.innerHTML = chatWorkHtml();
  const workNode = work.firstElementChild;
  wrapper.appendChild(workNode);
  workNode.querySelector('[data-step="search"]')?.classList.add("is-now");
  const stopTimer = chatWorkTimer(workNode);
  chatFollowBottom(wrapper, { counts: false });
  const input = chatInput();
  if (input) input.value = "";
  chatComposerSync();
  let answered = null;
  let stopped = false;
  let failure = null;
  try {
    answered = await chatStreamTurn(question, workNode, chatAbort.signal);
  } catch (error) {
    if (error && error.name === "AbortError") {
      stopped = true;
    } else {
      try {
        answered = await chatJsonTurn(question, workNode, chatAbort.signal);
      } catch (fallback) {
        if (fallback && fallback.name === "AbortError") stopped = true;
        else failure = fallback.message || error.message;
      }
    }
  }
  // Часы конвейера — единственное место, где эта цифра живёт; забрать её
  // надо до того, как узел уедет.
  const spent = workNode.querySelector?.(".agent-work__timer")?.textContent || "";
  stopTimer();
  workNode.remove();
  if (stopped) {
    wrapper.insertAdjacentHTML("beforeend", `
      <div class="agent-answer__card agent-answer__card--quiet">
        <div class="agent-answer__notice agent-answer__notice--quiet">Ответ остановлен${spent ? ` · ${escapeHtml(spent)}` : ""}</div>
        <p class="agent-answer__text agent-answer__text--refused">Ответ остановлен.</p>
        <div class="agent-answer__levels">
          <button class="agent-turn__copy" type="button" data-retry="${escapeHtml(question)}">↻ спросить снова</button>
        </div>
      </div>`);
    chatFollowBottom(wrapper);
    agentState.busy = false;
    chatAbort = null;
    chatComposerBusy(false);
    return;
  }
  if (!answered) {
    wrapper.insertAdjacentHTML("beforeend", `
      <div class="agent-answer__card agent-answer__card--quiet">
        <div class="agent-answer__notice agent-answer__notice--quiet">Агентный ответ, не редакция базы</div>
        <p class="agent-answer__text agent-answer__text--refused">${escapeHtml(failure || "поток оборвался")}</p>
        <div class="agent-answer__levels">
          <button class="agent-turn__copy" type="button" data-retry="${escapeHtml(question)}">↻ спросить снова</button>
        </div>
      </div>`);
    chatFollowBottom(wrapper);
    agentState.busy = false;
    chatAbort = null;
    chatComposerBusy(false);
    return;
  }
  wrapper.remove();
  const turn = { question, at, dateLabel, answered };
  chatTurns.push(turn);
  chatPersist();
  chatRenderNav();
  thread.insertAdjacentHTML("beforeend",
    chatTurnHtml(turn, chatTurns.length - 1, true, chatTurns[chatTurns.length - 2]?.dateLabel || null));
  chatFollowBottom(thread.lastElementChild);
  agentState.busy = false;
  chatAbort = null;
  chatComposerBusy(false);
}

/** Levels are free: the disclosure button only unhides evidence that arrived
 *  with the answer, so the thread is delegated here once, not per turn. */
document.getElementById("agentThread")?.addEventListener("click", event => {
  // Copy is the quietest courtesy a card can offer, and a refusal with its
  // nearby evidence is as worth copying as an answer.
  const copyAnswer = event.target.closest("[data-copy-turn]");
  if (copyAnswer) {
    chatCopyTurn(Number(copyAnswer.dataset.copyTurn), copyAnswer);
    return;
  }
  const copyQuestion = event.target.closest("[data-copy-q]");
  if (copyQuestion) {
    const turn = chatTurns[Number(copyQuestion.dataset.copyQ)];
    // Подпись над пузырём, а не сам пузырь: «СКОПИРОВАНО ✓» вместо «ВЫ · 12:41».
    // Раньше сюда передавалась обёртка, и копирование стирало вопрос.
    chatCopyText(String(turn?.question || ""), copyQuestion.querySelector(".agent-q__meta"));
    return;
  }
  const retry = event.target.closest("[data-retry]");
  if (retry) {
    agentAsk(retry.dataset.retry || "");
    return;
  }
  // «Стоп» стоит и на конвейере, и на кнопке отправки: одна и та же отмена,
  // просто под рукой у того, кто смотрит на ход, а не на строку ввода.
  if (event.target.closest("[data-stop-run]")) {
    chatAbort?.abort();
    return;
  }
  const mini = event.target.closest("[data-graph-mini]");
  if (mini) {
    const host = document.getElementById(`mini-${mini.dataset.graphMini}`);
    if (!host) return;
    const opening = host.hidden;
    host.hidden = !opening;
    mini.classList.toggle("is-open", opening);
    if (opening && !host.dataset.drawn) {
      host.dataset.drawn = "1";
      // Отказ внутри — не молчаливый: без этого панель навсегда оставалась на
      // «Загружаю граф…», а повторное открытие не пробовало снова.
      chatMiniGraph(Number(mini.dataset.graphMini), host).catch(error => {
        delete host.dataset.drawn;
        host.innerHTML = `<p class="agent-waiting">${escapeHtml(error.message)}</p>`;
      });
    }
    return;
  }
  const jump = event.target.closest("[data-open-links]");
  if (jump) {
    // In the conversation, the neighbourhood arrives as a card in the thread:
    // a free reader's whole interface is the chat, so the graph comes to them.
    chatGraphTurn(jump.dataset.openLinks ? `claim=${encodeURIComponent(jump.dataset.openLinks)}` : "");
    return;
  }
  const chatNode = event.target.closest("[data-chat-node]");
  if (chatNode) {
    chatGraphTurn(agentGraphRoute(chatNode.dataset.chatNode));
    return;
  }
  const closer = event.target.closest("[data-close-graph]");
  if (closer) {
    closer.closest(".chat-turn--graph")?.remove();
    return;
  }
  const teaser = event.target.closest("[data-sub-teaser]");
  if (teaser) {
    // Not dead, not lying: the click says what the feature costs and when it
    // is coming, once per row.
    const note = teaser.closest(".agent-answer__topics, .agent-tool__body")?.querySelector(".agent-sub__note");
    if (note) note.hidden = !note.hidden;
    return;
  }
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
    // Раскрыть блок мало: в нём может быть восемь карточек, а сноска вела в
    // одну. Вспышка говорит, в какую именно.
    const target = document.getElementById(String(cite.getAttribute("href") || "").slice(1));
    if (target) {
      target.classList.add("is-flash");
      clearTimeout(chatCiteFlash);
      chatCiteFlash = setTimeout(() => target.classList.remove("is-flash"), 1700);
      scrollToNode(target, "center");
    }
    return;
  }
  const button = event.target.closest(".agent-level[data-toggle]");
  if (!button) return;
  const block = document.getElementById(button.dataset.toggle);
  if (block) block.hidden = !block.hidden;
  button.classList.toggle("is-open", !block?.hidden);
});

function chatCopyText(text, button) {
  const done = () => {
    if (!button) return;
    const was = button.textContent;
    button.textContent = "скопировано ✓";
    setTimeout(() => { button.textContent = was; }, 1600);
  };
  if (navigator.clipboard?.writeText) {
    // Отказ буфера — не повод молчать: браузер может запретить запись без
    // жеста, и тогда читатель нажимал кнопку, а не происходило ничего.
    // Второй путь ниже работает и в этом случае.
    navigator.clipboard.writeText(text).then(done, () => chatCopyFallback(text, done));
    return;
  }
  chatCopyFallback(text, done);
}

/** The old road: a transient textarea. Executor-based copy is deprecated but
 *  not gone, and a no-op promise is worse than a second-best copy. */
function chatCopyFallback(text, done) {
  const scratch = document.createElement("textarea");
  scratch.value = text;
  document.body?.appendChild?.(scratch);
  scratch.select?.();
  try { document.execCommand("copy"); } catch (error) { /* denied is survivable */ }
  scratch.remove?.();
  done();
}

function chatCopyTurn(index, button) {
  const turn = chatTurns[index];
  if (!turn) return;
  const answered = turn.answered || {};
  const clauses = Array.isArray(answered.clauses) ? answered.clauses : [];
  const text = clauses.length
    ? clauses.map(clause => clause.text).join("\n")
    : answered.answer || chatRefusalText(answered, answered.evidence || []);
  chatCopyText(`${turn.question}\n\n${text}\n\n${answered.machineNotice || ""}`, button);
}

/* ── «В графе»: the answer's own shape, drawn from what it already carries ──
 *
 * The evidence rows arrive with their topics, so the map needs no new request:
 * statements on one side, the subjects they land on on the other. It is a
 * sketch of the answer, not the base - one hop deep, and every statement node
 * is one click from its full neighbourhood in «Связи». Where Cytoscape is
 * absent the block still opens: it just offers the jump instead of the sketch. */

/** The conversation walks in place: a node's neighbourhood arrives as a card
 *  in the thread, the way the answer did. Nothing in the chat switches a free
 *  reader to a tab - the chat is their whole interface, so the graph comes to
 *  the chat. Subscribers get the same card; their tab remains for browsing. */
async function chatGraphTurn(query, { follow = true } = {}) {
  const thread = document.getElementById("agentThread");
  if (!thread || !query) return;
  const card = document.createElement("div");
  card.className = "chat-turn chat-turn--graph is-fresh";
  card.innerHTML = `
    <div class="chat-graph__head">
      <span class="agent-ask__label">связи узла</span>
      <span class="agent-graph__legend"></span>
      <span class="agent-spacer"></span>
      <button class="chat-graph__close" type="button" data-close-graph aria-label="Убрать карточку">✕</button>
    </div>
    <p class="agent-links__meta" aria-live="polite">Собираю соседей…</p>
    <div class="agent-graph__canvas" hidden></div>
    <div class="chat-graph__list"></div>`;
  thread.appendChild(card);
  // Ход из композера читатель ждёт внизу; карточку, рождённую тапом по узлу, -
  // нет. Тащить ленту за ней значит увести человека с того места, где он читал.
  if (follow) chatFollowBottom(card);
  else chatFollowBottom(card, { counts: true, move: false, label: "карточка связей ниже" });
  const meta = card.querySelector(".agent-links__meta");
  try {
    const data = await kbFetch(`/graph?${query}&limit=40`);
    if (!data.centre) {
      if (meta) meta.textContent = "Такого узла в базе нет.";
      return;
    }
    agentGraphDestroy();
    card.querySelector(".agent-graph__legend")?.replaceChildren();
    const legend = card.querySelector(".agent-graph__legend");
    if (legend) legend.innerHTML = agentGraphKey(data.edges);
    const info = data.meta || {};
    const policy = {
      "most-recent": "показаны самые свежие",
      "most-recent-knowledge": "показаны самые свежие из знания",
      "link-limit": "показана часть связей",
      "all-neighbours": "показаны все соседи"
    }[info.selectionPolicy] || "";
    if (meta) {
      meta.textContent = info.truncated
        ? `Показано ${info.returnedNeighborCount} из ${info.totalNeighborCount} соседей — ${policy}.`
        : `Соседей: ${info.returnedNeighborCount || (data.nodes || []).length - 1}. ${policy}.`;
    }
    const machineProposed = (data.edges || []).some(edge => edge.layer === "authorial");
    const list = card.querySelector(".chat-graph__list");
    if (list) {
      list.innerHTML = `
        ${machineProposed
          ? `<p class="agent-links__notice">Цветные связи предложила машина; владелец базы их не подтверждал.</p>`
          : ""}
        ${agentLinksListHtml(data)}`;
      // Rows walk the conversation, not the tab: rename the routing attribute
      // so the thread's own delegation picks them up.
      list.querySelectorAll("[data-graph-node]").forEach(row => {
        row.dataset.chatNode = row.dataset.graphNode || "";
        delete row.dataset.graphNode;
      });
    }
    const host = card.querySelector(".agent-graph__canvas");
    const width = document.documentElement?.clientWidth || 1200;
    const isSubject = String(data.centre).startsWith("topic:");
    const wantCanvas = Boolean(host)
      && (String(query).includes("force=1") || (!isSubject && width >= 640))
      && (data.nodes || []).length <= 41;
    if (host) host.__chatWalk = true;
    const drawn = wantCanvas && await agentLinksGraphRender(data, host);
    if (host) host.hidden = !drawn;
  } catch (error) {
    if (meta) meta.textContent = error.message;
  }
}

/* ── Тап по узлу графа: сначала выделение, потом действие ─────────────────
 *
 * Одиночный тап уводил читателя немедленно: рождал карточку связей и
 * прокручивал к ней ленту. На таче панорамирование канвы регулярно
 * заканчивается тапом, и читатель терял место чтения ни за что.
 *
 * Теперь первый тап только называет узел — плашка под графом говорит, что это
 * и чем оно окажется, — а уводит второй тап по тому же узлу или кнопка. Двойной
 * клик и долгое нажатие работают сразу, для тех, кто уже знает дорогу.
 */
function graphSelection(host, act) {
  let chosen = null;
  let bar = host.parentElement?.querySelector?.(".graph-pick");
  if (!bar) {
    bar = document.createElement?.("div");
    if (bar && host.insertAdjacentElement) {
      bar.className = "graph-pick";
      bar.hidden = true;
      host.insertAdjacentElement("afterend", bar);
    } else {
      bar = null;
    }
  }
  // Где плашку показать негде - просить второй тап нечестно: читатель не увидит,
  // что выбрал. Тогда тап работает как раньше, сразу.
  if (!bar) return { tap: node => act(node.id()), clear: () => {} };
  const KIND = { topic: "тема", entity: "имя", statement: "утверждение" };
  const clear = () => {
    chosen = null;
    bar.hidden = true;
    bar.innerHTML = "";
  };
  bar.addEventListener("click", event => {
    if (!event.target?.closest?.("[data-graph-go]")) return;
    const going = chosen;
    clear();
    if (going) act(going);
  });
  return {
    tap(node, { now = false } = {}) {
      const id = node.id();
      if (now || chosen === id) {
        clear();
        act(id);
        return;
      }
      chosen = id;
      // Идентификаторы у двух графов разные: в «Связях» это `kind:key`, в
      // наброске ответа — `s0`/`t0`. Род узла надёжнее спросить у данных.
      const kind = node.data("topic") !== undefined ? "topic"
        : node.data("claim") !== undefined ? "statement"
        : String(id).split(":")[0];
      bar.hidden = false;
      bar.innerHTML = `
        <span class="graph-pick__kind mono">${escapeHtml(KIND[kind] || kind)}</span>
        <span class="graph-pick__name">${escapeHtml(node.data("label") || "")}</span>
        <button class="graph-pick__go" type="button" data-graph-go>Показать связи</button>`;
    },
    clear,
  };
}

/* ── Граф грузится, когда его открыли ───────────────────────────────────
 *
 * cytoscape весит 374 КБ — больше самого приложения — и ехал каждому
 * читателю синхронным <script> перед app.mjs, задерживая радар ради вкладки,
 * которую открывает один из многих. Теперь его просит первый мини-граф или
 * вкладка «Граф», один раз; ошибка сети оставляет читателю список соседей,
 * который и так рисуется всегда. Путь тот же, что был в index.html: он в
 * матчере ассетов Caddy и в артефакте, версия — в имени файла. */
const CYTOSCAPE_SRC = "/assets/vendor/cytoscape.3.30.4.min.js";
let cytoscapeLoading = null;

function loadCytoscape() {
  if (typeof cytoscape === "function") return Promise.resolve(true);
  if (cytoscapeLoading) return cytoscapeLoading;
  cytoscapeLoading = new Promise(resolve => {
    try {
      const script = document.createElement("script");
      script.src = CYTOSCAPE_SRC;
      const done = loaded => {
        // Неудавшийся тег снимается: иначе повторные отказы копят в <head> по
        // тегу на попытку, все с одним и тем же src.
        if (!loaded) script.remove?.();
        resolve(loaded);
      };
      script.onload = () => done(typeof cytoscape === "function");
      script.onerror = () => done(false);
      document.head.appendChild(script);
    } catch {
      resolve(false); // консольный смоук: документа нет, есть список
    }
  }).then(loaded => {
    if (!loaded) cytoscapeLoading = null; // следующая попытка — новый запрос
    return loaded;
  });
  return cytoscapeLoading;
}

function chatMinisDestroy() {
  chatMinis.forEach(instance => {
    try {
      instance.destroy();
    } catch (error) {
      /* a thread being cleared owes nothing to its sketches */
    }
  });
  chatMinis.clear();
}

async function chatMiniGraph(index, host) {
  const turn = chatTurns[index];
  const evidence = Array.isArray(turn?.answered?.evidence) ? turn.answered.evidence : [];
  const openButton = claim =>
    `<button class="agent-turn__copy" type="button" data-open-links="${escapeHtml(claim || "")}">открыть в «Связях»</button>`;
  host.innerHTML = agentLoadingHtml("Загружаю граф…");
  if (!(await loadCytoscape())) {
    host.innerHTML = `
      <p class="agent-waiting">Мини-граф здесь не рисуется, но соседей видно целиком.</p>
      <div class="chat-mini__foot">${openButton(chatTopClaim(evidence))}</div>`;
    return;
  }
  const ranked = chatTopicsRanked(evidence);
  const shown = ranked.slice(0, CHAT_TOPICS_SHOWN);
  host.innerHTML = `
    <div class="chat-mini__head">
      <span class="chat-mini__title">Связи ответа в графе</span>
      <span class="chat-mini__note mono">узлы из данных ответа · без запроса к базе</span>
    </div>
    <div class="chat-mini__grid">
      <div class="chat-mini__host"></div>
      <div class="chat-mini__neighbours">
        <span class="chat-mini__label mono">соседи узла</span>
        <span class="agent-graph__legend chat-mini__key">${agentGraphKey([])}</span>
        ${evidence.slice(0, CHAT_TOPICS_SHOWN).map(row => {
          const one = agentRow(row);
          const status = AGENT_STATUS_LABEL[one.status] || one.status || "утверждение";
          return `<span class="chat-mini__row"><i class="chat-mini__dot chat-mini__dot--${escapeHtml(AGENT_STATUS_CLASS[one.status] || "agent-label--far")}"></i><b>${escapeHtml(chatShort(one.statement || one.quote || "", 34))}</b><em>${escapeHtml(status)}</em></span>`;
        }).join("")}
        ${shown.map(topic =>
          `<span class="chat-mini__row"><i class="chat-mini__dot chat-mini__dot--topic"></i><b>${escapeHtml(chatShort(topic, 34))}</b><em>тема</em></span>`).join("")}
      </div>
    </div>
    <div class="chat-mini__foot">${
      openButton(chatTopClaim(evidence))
    } показано ${shown.length + Math.min(evidence.length, CHAT_TOPICS_SHOWN)} из ${ranked.length + evidence.length} соседей — самые свежие · связи предложила машина, владелец не подтверждал · полный граф — по подписке</div>`;
  const statements = evidence.map((row, order) => ({
    data: {
      id: `s${order}`,
      label: chatShort(row.statement || row.quote_text || row.quote || "", 30),
      claim: row.claim_id || row.claimId || ""
    }
  }));
  const topics = shown.map((label, order) => ({
    data: { id: `t${order}`, label: chatShort(label, 24), topic: label }
  }));
  const edges = [];
  evidence.forEach((row, order) => {
    (row.topics || []).forEach(topic => {
      const target = topics.findIndex(node => node.data.topic === topic);
      if (target !== -1) edges.push({ data: { id: `e${order}-${target}`, source: `s${order}`, target: `t${target}` } });
    });
  });
  const canvas = host.querySelector(".chat-mini__host");
  const mini = cytoscape({
    container: canvas,
    elements: [...statements, ...topics, ...edges],
    style: [
      { selector: "node", style: {
        shape: ele => (ele.data("topic") !== undefined ? "rectangle" : "ellipse"),
        "background-color": ele => (ele.data("topic") !== undefined ? "#2b4a75" : "#ffffff"),
        "border-width": 2,
        "border-color": ele => (ele.data("topic") !== undefined ? "#2b4a75" : "#1f242a"),
        width: 24, height: 24,
        label: "data(label)", "text-wrap": "wrap", "text-max-width": 110,
        "font-size": 9, color: "#1f242a", "text-valign": "bottom", "text-margin-y": 6
      } },
      { selector: "edge", style: {
        "line-color": "#c9cdcf", width: 1.2, "curve-style": "haystack", opacity: 0.7
      } }
    ],
    layout: { name: "cose", nodeDimensionsIncludeLabels: true, animate: false, padding: 24 }
  });
  const pick = graphSelection(canvas, id => {
    const claim = mini.getElementById(id)?.data?.("claim");
    if (claim) chatGraphTurn(`claim=${encodeURIComponent(claim)}`, { follow: false });
  });
  mini.on("tap", "node", event => {
    if (event.target.data("claim")) pick.tap(event.target);
  });
  mini.on("dbltap", "node", event => {
    if (event.target.data("claim")) pick.tap(event.target, { now: true });
  });
  chatMinis.set(index, mini);
}

function chatTopClaim(evidence) {
  let best = null;
  let bestScore = -1;
  evidence.forEach(row => {
    const score = Number(row.relevance != null ? row.relevance : 0);
    const claim = row.claim_id || row.claimId || "";
    if (claim && score >= bestScore) { best = claim; bestScore = score; }
  });
  return best || "";
}

/* ── «Подписаться на узел»: shown, priced, not yet live ────────────────────
 *
 * The owner's decision: the button is part of the interface now, marked
 * «доступна по подписке», and turns real once access levels exist. A visible
 * control that says what it costs beats an invisible feature - and a dead
 * button that pretends to work beats nothing only in products that lie. */

const CHAT_SUB_LOCK = `<svg width="10" height="10" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><rect x="2.5" y="5" width="7" height="5" rx="1"></rect><path d="M4 5V3.5a2 2 0 0 1 4 0V5"></path></svg>`;
const CHAT_SUB_RSS = `<svg width="11" height="11" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><circle cx="3" cy="11" r="1.3" fill="currentColor" stroke="none"></circle><path d="M1.5 6.5a6 6 0 0 1 6 6"></path><path d="M1.5 2.5a10 10 0 0 1 10 10"></path></svg>`;

function chatSubTeaser(label) {
  return `
    <button class="agent-sub" type="button" data-sub-teaser="${escapeHtml(label)}"
      title="Подписка на узел — доступна по подписке" aria-label="Подписаться на «${escapeHtml(label)}» — доступна по подписке">
      ${CHAT_SUB_RSS}${CHAT_SUB_LOCK}${escapeHtml(label)}
    </button>`;
}

//: Measured on production: one answer carried fifteen subjects. A row without a
//: ceiling is a wall, and the rest of this file shows five of anything.
const CHAT_TOPICS_SHOWN = 8;

/** The subjects an answer's evidence lands on, most-connected first, so a cut
 *  row keeps the ones that actually hold the answer together. */
function chatTopicsRanked(evidence) {
  const weight = new Map();
  (evidence || []).forEach(row =>
    (row.topics || []).forEach(topic => weight.set(topic, (weight.get(topic) || 0) + 1)));
  return [...weight.entries()].sort((a, b) => b[1] - a[1]).map(entry => entry[0]);
}

function chatTopicsRow(evidence) {
  const titles = chatTopicsRanked(evidence);
  if (!titles.length) return "";
  const shown = titles.slice(0, CHAT_TOPICS_SHOWN);
  const hidden = titles.length - shown.length;
  return `
    <div class="agent-answer__topics">
      <span class="agent-ask__label">темы ответа:</span>
      ${shown.map(chatSubTeaser).join("")}
      ${hidden ? `<span class="agent-sub__more mono" title="${escapeHtml(titles.slice(CHAT_TOPICS_SHOWN).join(", "))}">и ещё ${hidden}</span>` : ""}
      <span class="agent-sub__note" hidden>Подписка на получение информации по темам доступна после авторизации.</span>
    </div>`;
}

/* Enter asks, Shift+Enter breaks a line - the convention every chat reader
 * already carries in their hands. */
chatInput()?.addEventListener("input", chatComposerSync);
chatInput()?.addEventListener("keydown", event => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    chatSubmit();
  }
});

document.getElementById("morePrompts")?.addEventListener("click", agentLoadPrompts);

document.getElementById("newDialog")?.addEventListener("click", () => {
  chatMinisDestroy();
  chatTurns = [];
  chatUnread = 0;
  chatDownSync();
  chatPersist();
  chatRenderNav();
  const thread = document.getElementById("agentThread");
  if (thread) thread.innerHTML = "";
  chatShowWelcome(true);
  agentLoadPrompts();
  window.scrollTo({ top: 0, behavior: "smooth" });
});

/* ── Голос: два уровня, как в эталоне ────────────────────────────────────
 *
 * Клик — диктовка в строку: узкая полоса над композером, живой транскрипт,
 * текст дописывается в поле вопроса. Удержание от 450 мс — голосовой режим:
 * оверлей на всё окно, и на отпускании распознанное падает в ту же строку.
 * Отправку не делает ни один из них: решение спросить остаётся за читателем.
 *
 * Микрофон — браузерный, Web Speech API, без зависимостей и без сервера. Где
 * его нет (Firefox), кнопка остаётся на месте и говорит об этом словами:
 * мёртвая кнопка, притворяющаяся слушающей, — единственная ложь, которой
 * этому экрану нельзя. */

const VOICE_HOLD_MS = 450;
const voice = {
  mode: null,           // "inline" | "overlay"
  session: null,        // экземпляр распознавателя
  base: "",             // что было в строке до начала
  final: "",
  interim: "",
  startedAt: 0,
  tick: null,
  hold: null,
  suppressClick: false,
};

function voiceEngine() {
  return window.SpeechRecognition || window.webkitSpeechRecognition || null;
}

function voiceTranscript() {
  return `${voice.final}${voice.interim}`.trim();
}

function voiceElapsed() {
  const seconds = Math.max(0, Math.round((Date.now() - voice.startedAt) / 1000));
  return `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
}

/** Полоса над композером: четыре дышащие палочки, транскрипт с курсором, СТОП. */
function voiceRenderInline() {
  const live = document.getElementById("chatMicLive");
  if (!live) return;
  live.hidden = voice.mode !== "inline";
  if (voice.mode !== "inline") return;
  const heard = escapeHtml(voiceTranscript()) || "…";
  live.innerHTML = `
    <span class="agent-composer__bars" aria-hidden="true"><i></i><i></i><i></i><i></i></span>
    <span class="agent-composer__heard">слушаю: <i>${heard}</i><span class="agent-composer__caret" aria-hidden="true">▊</span></span>
    <button class="agent-composer__stop mono" type="button" data-voice-stop>СТОП</button>`;
}

/** Оверлей голосового режима: графитовая карточка, эквалайзер, транскрипт. */
function voiceRenderOverlay() {
  const overlay = document.getElementById("voiceOverlay");
  if (!overlay) return;
  overlay.hidden = voice.mode !== "overlay";
  if (voice.mode !== "overlay") return;
  const clock = document.getElementById("voiceClock");
  if (clock) clock.textContent = `СЛУШАЮ · ${voiceElapsed()}`;
  const said = document.getElementById("voiceHeard");
  if (said) {
    const heard = voiceTranscript();
    said.innerHTML = heard
      ? `«${escapeHtml(heard)}»<span class="voice-overlay__caret" aria-hidden="true">▊</span>`
      : `<span class="voice-overlay__caret" aria-hidden="true">▊</span>`;
  }
}

function voiceRender() {
  voiceRenderInline();
  voiceRenderOverlay();
}

function voiceNotice(text) {
  const live = document.getElementById("chatMicLive");
  if (!live) return;
  live.hidden = false;
  live.textContent = text;
  clearTimeout(voice.notice);
  voice.notice = setTimeout(() => {
    if (voice.mode !== "inline") live.hidden = true;
  }, 4000);
}

function voiceStart(mode) {
  const Engine = voiceEngine();
  const mic = document.getElementById("agentMic");
  if (!Engine) {
    voiceNotice("Голосовой ввод не поддерживается этим браузером.");
    return;
  }
  if (voice.mode) voiceStop({ commit: false });
  voice.mode = mode;
  voice.base = String(chatInput()?.value || "");
  voice.final = "";
  voice.interim = "";
  voice.startedAt = Date.now();
  mic?.classList.add("is-listening");
  voiceRender();
  // Часы оверлея идут сами: распознаватель молчит между фразами.
  clearInterval(voice.tick);
  voice.tick = setInterval(voiceRenderOverlay, 250);

  const session = new Engine();
  voice.session = session;
  session.lang = "ru-RU";
  session.interimResults = true;
  // В голосовом режиме читатель держит кнопку и говорит фразами; в диктовке
  // хватает одной.
  session.continuous = mode === "overlay";
  session.maxAlternatives = 1;
  session.onresult = event => {
    let final = "";
    let interim = "";
    for (let index = event.resultIndex; index < event.results.length; index += 1) {
      const result = event.results[index];
      if (result.isFinal) final += result[0].transcript;
      else interim += result[0].transcript;
    }
    if (final) voice.final += final;
    voice.interim = interim;
    // Диктовка пишет в строку по ходу дела; голосовой режим — на отпускании,
    // чтобы читатель видел в оверлее ровно то, что попадёт в вопрос.
    if (voice.mode === "inline") voiceCommit();
    voiceRender();
  };
  session.onerror = event => {
    const reason = event.error === "not-allowed" || event.error === "service-not-allowed"
      ? "Микрофон запрещён: сайт или браузер блокирует запись (Permissions-Policy)."
      : event.error === "no-speech" ? ""
      : `Микрофон недоступен: ${event.error || "неизвестная причина"}.`;
    voiceStop({ commit: false });
    if (reason) voiceNotice(reason);
  };
  session.onend = () => {
    // Голосовой режим живёт, пока держат кнопку: браузер обрывает сессию по
    // паузе, и её надо поднять заново, иначе оверлей слушает пустоту.
    if (voice.mode === "overlay" && voice.session === session) {
      try { session.start(); return; } catch (error) { /* поднять не вышло */ }
    }
    if (voice.mode === "inline") voiceStop({ commit: true });
  };
  try {
    session.start();
  } catch (error) {
    voiceStop({ commit: false });
  }
}

/** Распознанное — в строку вопроса, к тому, что там уже было. */
function voiceCommit() {
  const input = chatInput();
  const heard = voiceTranscript();
  if (!input || !heard) return;
  const before = voice.base ? `${voice.base.trimEnd()} ` : "";
  input.value = before + heard;
  chatComposerSync();
}

function voiceStop({ commit = true } = {}) {
  const session = voice.session;
  voice.session = null;
  const wasMode = voice.mode;
  voice.mode = null;
  clearInterval(voice.tick);
  if (session) {
    session.onend = null;
    session.onresult = null;
    session.onerror = null;
    try { session.stop(); } catch (error) { /* уже остановлен */ }
  }
  if (commit && wasMode) voiceCommit();
  voice.final = "";
  voice.interim = "";
  document.getElementById("agentMic")?.classList.remove("is-listening");
  voiceRender();
}

function voiceInit() {
  const mic = document.getElementById("agentMic");
  if (!mic) return;
  if (!voiceEngine()) {
    // Кнопка остаётся, но говорит правду: и наведением, и по клику.
    mic.classList.add("is-mute");
    mic.setAttribute("title", "Голосовой ввод не поддерживается этим браузером");
    mic.setAttribute("aria-label", "Голосовой ввод не поддерживается этим браузером");
    mic.addEventListener("click", () => voiceNotice("Голосовой ввод не поддерживается этим браузером."));
    return;
  }
  mic.setAttribute("title", "Клик — диктовка в строку · удержание — голосовой режим");

  const hold = event => {
    if (event.button !== undefined && event.button !== 0) return;
    clearTimeout(voice.hold);
    voice.hold = setTimeout(() => {
      // Клик прилетит следом за отпусканием — он здесь уже лишний.
      voice.suppressClick = true;
      voiceStart("overlay");
    }, VOICE_HOLD_MS);
  };
  mic.addEventListener("mousedown", hold);
  mic.addEventListener("touchstart", hold, { passive: true });

  mic.addEventListener("click", () => {
    if (voice.suppressClick) { voice.suppressClick = false; return; }
    if (voice.mode) voiceStop({ commit: true });
    else voiceStart("inline");
  });

  const release = () => {
    clearTimeout(voice.hold);
    if (voice.mode === "overlay") voiceStop({ commit: true });
  };
  document.addEventListener("mouseup", release);
  document.addEventListener("touchend", release);
}

voiceInit();

/* СТОП на полосе диктовки — та же остановка, что и повторный клик по кнопке. */
document.getElementById("chatMicLive")?.addEventListener("click", event => {
  if (event.target?.closest?.("[data-voice-stop]")) voiceStop({ commit: true });
});

function plural(n, one, few, many) {
  const mod10 = n % 10;
  const mod100 = n % 100;
  if (mod10 === 1 && mod100 !== 11) return one;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return few;
  return many;
}

/** One waiting state for the whole agent mode: a quiet spinner and the words
 *  of what is being waited for. Every slow surface speaks it - panels, lists,
 *  the graph's pickers - so «идёт загрузка» looks the same wherever it is
 *  looked for, and never again happens invisibly. */
function agentLoadingHtml(text) {
  return `<div class="agent-loading" role="status" aria-live="polite">
    <span class="agent-loading__spin" aria-hidden="true"></span>
    <span class="agent-loading__text">${escapeHtml(text || "загружаю…")}</span>
  </div>`;
}

/** A picker that is filling says so in its own frame: disabled, dimmed, its
 *  label wearing the same spinner. Empty-looking selects that are actually
 *  loading are how a fast page teaches its reader to distrust it. */
function agentSelectLoading(select, on, label) {
  if (!select) return;
  select.classList.toggle("is-loading", on);
  select.disabled = on;
  select.closest?.(".agent-select")?.classList?.toggle?.("is-loading", on);
  const first = select.options?.[0];
  if (first && label) first.textContent = label;
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
  agentSelectLoading(select, true, "загружаю темы…");
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
  } finally {
    agentSelectLoading(select, false, "тема");
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
  box.innerHTML = agentLoadingHtml("Ищу по словам и по смыслу…");
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

async function agentLinksGraphRender(data, host) {
  if (!(await loadCytoscape())) return false;
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
  // Where a node tap goes depends on whose graph this is: the tab's canvas walks
  // the tab, a card inside the conversation walks the conversation - a free
  // reader's only surface is the chat, and it must never bounce them into a tab
  // they cannot read.
  const walk = id => (host.__chatWalk
    ? chatGraphTurn(agentGraphRoute(id), { follow: false })
    : agentLoadGraph(agentGraphRoute(id)));
  const picked = graphSelection(host, walk);
  linksCanvas.on("tap", "node", event => picked.tap(event.target));
  linksCanvas.on("dbltap", "node", event => picked.tap(event.target, { now: true }));
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

/** Двухчастный ключ к любой графовой поверхности.
 *
 *  Прежняя легенда строилась только из типов рёбер, которые оказались в
 *  данных: узлы не расшифровывались нигде, а объяснение серой и цветной линии
 *  появлялось лишь тогда, когда в ответе был машинный слой. Читатель видел
 *  синий квадрат и белый круг и не имел способа узнать, что это.
 *
 *  Часть про узлы постоянна - она описывает язык картинки, а не её содержимое. */
function agentGraphKey(edges) {
  return `<span class="agent-graph__part">
      <span class="agent-graph__partname">узлы</span>
      <span class="agent-graph__key"><i class="agent-graph__node agent-graph__node--statement"></i>утверждение</span>
      <span class="agent-graph__key"><i class="agent-graph__node agent-graph__node--topic"></i>тема</span>
      <span class="agent-graph__key"><i class="agent-graph__node agent-graph__node--entity"></i>имя</span>
    </span>
    <span class="agent-graph__part">
      <span class="agent-graph__partname">связи</span>
      <span class="agent-graph__key agent-graph__key--structural"><i style="background:#c9cdcf"></i>устройство базы</span>
      <span class="agent-graph__key"><i style="background:${AGENT_RELATION_COLOUR.qualifies}"></i>предложила машина</span>
      ${agentGraphLegend(edges)}
    </span>`;
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
    agentSelectLoading(select, true, "загружаю темы…");
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
    } finally {
      agentSelectLoading(select, false, "выберите тему");
    }
  }
  const picked = document.getElementById("agentGraphEntity");
  if (picked && !picked.dataset.loaded) {
    agentSelectLoading(picked, true, "загружаю имена…");
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
    } finally {
      agentSelectLoading(picked, false, "выберите имя");
    }
  }
  const query = target
    || (picked?.value ? `entity=${encodeURIComponent(picked.value)}` : "")
    || (select?.value ? `topic=${encodeURIComponent(select.value)}` : "");
  if (!query) {
    box.innerHTML = `<p class="agent-intro">Выберите тему — и агент покажет, что под ней стоит
    и чем эти утверждения связаны между собой. Из карточки любого утверждения сюда же
    ведёт кнопка «Что агент связал с этим».</p>`;
    return;
  }
  box.innerHTML = agentLoadingHtml("Собираю связи…");
  try {
    const data = await kbFetch(`/graph?${query}&limit=40`);
    if (!data.centre) {
      box.innerHTML = `<p class="agent-links__empty">Такого узла в базе нет.</p>`;
      return;
    }
    agentGraphDestroy();
    const legend = document.getElementById("agentGraphLegend");
    if (legend) legend.innerHTML = agentGraphKey(data.edges);
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
    const drawn = wantCanvas && await agentLinksGraphRender(data, host);
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
  box.innerHTML = agentLoadingHtml("Загрузка разногласий…");
  try {
    const data = await kbFetch("/contradictions?limit=40");
    const pairs = Array.isArray(data.pairs) ? data.pairs : [];
    box.innerHTML = `
      <p class="agent-intro">Пары, которые агент считает несовместимыми: об одном предмете
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
  box.innerHTML = agentLoadingHtml("Загрузка хроники…");
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
  box.innerHTML = agentLoadingHtml("Загрузка скелета тем…");
  try {
    const data = await kbFetch("/topics");
    const topics = (data.topics || []).filter(topic => topic.statements > 0);
    box.innerHTML = `
      <p class="agent-intro">Тема — это узел авторского рубрикатора базы: в ней живут
      утверждения, а не готовое описание. Карточка собирается из утверждений.</p>
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
    scrollToNode(card);
  } catch (error) {
    card.innerHTML = `<p class="agent-waiting">${escapeHtml(error.message)}</p>`;
  }
}

async function agentLoadWiki() {
  const box = document.getElementById("agentWiki");
  if (!box || box.dataset.loaded) return;
  box.innerHTML = agentLoadingHtml("Загрузка страниц…");
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
    scrollToNode(card);
  } catch (error) {
    card.innerHTML = `<p class="agent-waiting">${escapeHtml(error.message)}</p>`;
  }
}

async function agentLoadGaps() {
  const box = document.getElementById("agentGaps");
  if (!box || box.dataset.loaded) return;
  box.innerHTML = agentLoadingHtml("Загрузка карты пробелов…");
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
  // Композер принадлежит диалогу: на вкладке «Темы» строка вопроса — шум.
  document.getElementById("agentComposer")?.toggleAttribute("hidden", tab !== "ask");
  document.body.classList.toggle("is-chat", tab === "ask");
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
  chatSubmit();
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
