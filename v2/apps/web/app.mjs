export const applicationIdentity = Object.freeze({
  application: "radar-v2-web",
  stage: "stage-8-implemented",
  status: "ready",
});

const content = document.querySelector("#content");
const health = document.querySelector("#health");

if (!(content instanceof HTMLElement) || !(health instanceof HTMLElement)) {
  throw new Error("Radar V2 application shell is incomplete");
}

function element(tag, options = {}) {
  const node = document.createElement(tag);
  if (options.className) node.className = options.className;
  if (options.text !== undefined) node.textContent = String(options.text);
  for (const [name, value] of Object.entries(options.attrs ?? {})) {
    node.setAttribute(name, String(value));
  }
  return node;
}

function replaceContent(...nodes) {
  content.replaceChildren(...nodes);
  content.focus({ preventScroll: true });
}

function formatDate(value) {
  const parsed = new Date(`${value}T12:00:00Z`);
  if (Number.isNaN(parsed.valueOf())) return value;
  return new Intl.DateTimeFormat("ru-RU", {
    day: "numeric",
    month: "long",
    year: "numeric",
    timeZone: "UTC",
  }).format(parsed);
}

function safeExternalUrl(value) {
  try {
    const parsed = new URL(value);
    if (!["http:", "https:"].includes(parsed.protocol)) return null;
    if (parsed.username || parsed.password) return null;
    return parsed.href;
  } catch {
    return null;
  }
}

async function api(path) {
  const response = await fetch(path, {
    headers: { Accept: "application/json" },
    credentials: "same-origin",
    signal: AbortSignal.timeout(10000),
  });
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    const error = new Error("Published data request failed");
    error.status = response.status;
    error.code = payload?.code ?? "REQUEST_FAILED";
    throw error;
  }
  return payload;
}

function titleBlock(kicker, title, description = null) {
  const block = element("header", { className: "page-title" });
  block.append(element("p", { className: "eyebrow", text: kicker }));
  block.append(element("h1", { text: title }));
  if (description) block.append(element("p", { className: "lede", text: description }));
  return block;
}

function llmNotice(llm) {
  if (!llm || llm.status === "success") return null;
  const unavailable = llm.status === "unavailable";
  const notice = element("aside", {
    className: `notice ${unavailable ? "notice-warning" : "notice-info"}`,
    attrs: { role: "note" },
  });
  notice.append(
    element("strong", {
      text: unavailable ? "Анализ без LLM" : "Использован резервный анализ",
    }),
    element("p", {
      text: unavailable
        ? "Модели были недоступны. Структура выпуска и факты сформированы детерминированно."
        : "Основная модель не дала принятого результата; показано проверенное резервное представление.",
    }),
  );
  return notice;
}

function statCard(label, value) {
  const card = element("div", { className: "stat" });
  card.append(element("strong", { text: value }), element("span", { text: label }));
  return card;
}

function renderStats(stats) {
  const grid = element("section", {
    className: "stats-grid",
    attrs: { "aria-label": "Статистика выпуска" },
  });
  grid.append(
    statCard("просмотрено", stats.viewed),
    statCard("включено", stats.included),
    statCard("отсечено", stats.cut),
    statCard("ядро", stats.core),
  );
  return grid;
}

function renderAnalysis(analysis) {
  const section = element("section", { className: "analysis" });
  const heading = element("div", { className: "section-heading" });
  heading.append(element("p", { className: "eyebrow", text: "Редакционный вывод" }));
  heading.append(element("h2", { text: analysis.headline || "Картина дня" }));
  if (analysis.brief) heading.append(element("p", { text: analysis.brief }));
  section.append(heading);
  const blocks = element("div", { className: "analysis-grid" });
  for (const block of analysis.blocks) {
    const article = element("article", { className: `analysis-card kind-${block.kind}` });
    article.append(element("h3", { text: block.title }), element("p", { text: block.text }));
    blocks.append(article);
  }
  section.append(blocks);
  return section;
}

function materialCard(material) {
  const article = element("article", { className: "material-card" });
  const meta = element("div", { className: "material-meta" });
  meta.append(
    element("span", { className: `signal signal-${material.signalStrength}`, text: material.signalStrength }),
    element("span", { text: material.sourceName || "Источник не указан" }),
    element("span", { text: material.perimeter }),
  );
  const heading = element("h3");
  const safeUrl = safeExternalUrl(material.url);
  if (safeUrl) {
    heading.append(
      element("a", {
        text: material.title,
        attrs: { href: safeUrl, rel: "noopener noreferrer", target: "_blank" },
      }),
    );
  } else {
    heading.textContent = material.title;
  }
  article.append(meta, heading);
  if (material.summary) article.append(element("p", { text: material.summary }));
  if (material.agpmTakeaway) {
    const takeaway = element("p", { className: "takeaway" });
    takeaway.append(element("strong", { text: "Для AgPM: " }), document.createTextNode(material.agpmTakeaway));
    article.append(takeaway);
  }
  if (material.rubrics?.length) {
    const tags = element("ul", { className: "tags", attrs: { "aria-label": "Рубрики" } });
    for (const rubric of material.rubrics) tags.append(element("li", { text: rubric }));
    article.append(tags);
  }
  const notice = llmNotice(material.llm);
  if (notice) article.append(notice);
  return article;
}

function materialList(materials, emptyText = "По выбранным условиям материалов нет.") {
  if (!materials.length) {
    const empty = element("section", { className: "empty-state" });
    empty.append(element("span", { className: "empty-mark", text: "○" }));
    empty.append(element("h2", { text: "Нет материалов" }), element("p", { text: emptyText }));
    return empty;
  }
  const section = element("section", { className: "materials" });
  section.append(element("h2", { text: "Материалы выпуска" }));
  const list = element("div", { className: "material-list" });
  for (const material of materials) list.append(materialCard(material));
  section.append(list);
  return section;
}

function renderIssue(issue) {
  document.title = `${issue.title} — AgPM Radar`;
  const header = titleBlock(formatDate(issue.issueDate), issue.title, issue.brief);
  const notice = llmNotice(issue.llm);
  const nodes = [header];
  if (notice) nodes.push(notice);
  nodes.push(renderStats(issue.stats));
  if (issue.materialCount === 0) {
    nodes.push(materialList([], "Выпуск опубликован корректно, но квалифицирующих материалов в этот день не было."));
  } else {
    nodes.push(renderAnalysis(issue.analysis));
    if (issue.theses?.length) {
      const theses = element("section", { className: "theses" });
      theses.append(element("h2", { text: "Ключевые тезисы" }));
      const list = element("ul");
      for (const thesis of issue.theses) {
        const item = element("li");
        item.append(element("strong", { text: thesis.lead }), document.createTextNode(` ${thesis.rest}`));
        list.append(item);
      }
      theses.append(list);
      nodes.push(theses);
    }
    nodes.push(materialList(issue.materials));
  }
  replaceContent(...nodes);
}

async function renderArchives() {
  const payload = await api("/api/issues?limit=100");
  const header = titleBlock("Опубликованные выпуски", "Архив Radar", "Только принятые и опубликованные редакционные срезы.");
  const list = element("div", { className: "archive-list" });
  for (const issue of payload.items) {
    const link = element("a", { className: "archive-item", attrs: { href: `/issues/${issue.issueDate}` } });
    const copy = element("span");
    copy.append(element("small", { text: formatDate(issue.issueDate) }), element("strong", { text: issue.title }));
    link.append(copy, element("span", { className: "archive-count", text: `${issue.materialCount} мат.` }));
    list.append(link);
  }
  if (!payload.items.length) list.append(element("p", { text: "Опубликованных выпусков пока нет." }));
  replaceContent(header, list);
}

async function renderGazettes() {
  const payload = await api("/api/gazettes?limit=100");
  const header = titleBlock("Длинная форма", "Газеты", "Тематические обзоры и специальные выпуски Radar.");
  const grid = element("div", { className: "gazette-grid" });
  for (const gazette of payload.items) {
    const link = element("a", { className: "gazette-card", attrs: { href: gazette.url } });
    link.append(element("span", { className: "gazette-period", text: gazette.period }));
    link.append(element("h2", { text: gazette.title }));
    link.append(element("span", { className: "arrow", text: "Читать →" }));
    grid.append(link);
  }
  if (!payload.items.length) grid.append(element("p", { text: "Опубликованных газет пока нет." }));
  replaceContent(header, grid);
}

async function renderSearch() {
  const params = new URLSearchParams(window.location.search);
  const initialQuery = (params.get("q") || "").slice(0, 200);
  const header = titleBlock("По опубликованным материалам", "Поиск", "Поиск не затрагивает черновики и редакционную очередь.");
  const form = element("form", { className: "search-form", attrs: { role: "search" } });
  const label = element("label", { text: "Запрос" });
  const input = element("input", {
    attrs: { autocomplete: "off", maxlength: "200", name: "q", placeholder: "Например: оркестрация агентов", required: "", type: "search", value: initialQuery },
  });
  const button = element("button", { text: "Найти", attrs: { type: "submit" } });
  label.append(input);
  form.append(label, button);
  const results = element("section", { className: "search-results", attrs: { "aria-live": "polite" } });
  async function search(query) {
    results.replaceChildren(element("p", { text: "Ищу…" }));
    const payload = await api(`/api/search?q=${encodeURIComponent(query)}&period=30d&limit=100`);
    results.replaceChildren(materialList(payload.items, "Совпадений в опубликованных материалах не найдено."));
  }
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const query = input.value.trim();
    if (!query) return;
    const url = new URL(window.location.href);
    url.search = new URLSearchParams({ q: query }).toString();
    window.history.replaceState({}, "", url);
    search(query).catch(renderFailure);
  });
  replaceContent(header, form, results);
  if (initialQuery) await search(initialQuery);
}

function renderFailure(error) {
  const status = error?.status === 404 ? "Материал не найден" : "Данные временно недоступны";
  const message = error?.status === 404
    ? "Проверьте адрес или вернитесь к последнему выпуску."
    : "Попробуйте обновить страницу через несколько минут.";
  const section = element("section", { className: "error-state", attrs: { role: "alert" } });
  section.append(element("p", { className: "eyebrow", text: "Radar" }));
  section.append(element("h1", { text: status }), element("p", { text: message }));
  section.append(element("a", { text: "К последнему выпуску", attrs: { href: "/" } }));
  replaceContent(section);
}

async function route() {
  const path = window.location.pathname;
  const healthPayload = await api("/api/health");
  health.textContent = healthPayload.status === "ok" ? "Данные актуальны" : "Проверка данных";
  health.classList.add("health-ok");
  if (path === "/") return renderIssue(await api("/api/latest"));
  if (path === "/issues") return renderArchives();
  if (path.startsWith("/issues/")) {
    const issueDate = path.slice("/issues/".length);
    return renderIssue(await api(`/api/issues/${issueDate}`));
  }
  if (path === "/gazettes") return renderGazettes();
  if (path === "/search") return renderSearch();
  throw Object.assign(new Error("Unknown route"), { status: 404 });
}

route().catch(renderFailure);

export { element, formatDate, safeExternalUrl };
