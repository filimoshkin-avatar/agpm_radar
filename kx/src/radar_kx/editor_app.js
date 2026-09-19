"use strict";
// No inline script and no inline handlers: radar.agpm.space serves a strict CSP
// (script-src 'self'), and the editor lives under it. Everything is bound here.

const nav = document.getElementById("nav");
const body = document.getElementById("body");
const why = document.getElementById("why");
let current = null;

const observationLabels = {
  same_event: "Дубль", different_event: "Разные события", development: "Развитие",
  insufficient_evidence: "Недостаточно данных", review_overlap: "Пересечение обзора",
};
let observationGeneration = 0;

function downloadObservation(value, filename) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2)], { type: "application/json" }));
  const link = el("a"); link.href = url; link.download = filename; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

async function showObservations() {
  current = "observations";
  const generation = ++observationGeneration;
  body.replaceChildren(el("p", "empty", "Загрузка выпусков…"));
  why.textContent = "Редакторская калибровка. Подозрения не меняют карточки и не блокируют публикацию. Сравниваются видимые тексты карточек, без проверки полного текста источников; уверенность модели ещё не откалибрована.";
  await drawNav();
  try {
    const index = await api("api/event-observations");
    if (current !== "observations" || generation !== observationGeneration) return;
    const controls = el("div", "observation-controls");
    const label = el("label", null, "Выпуск ");
    const day = el("input"); day.type = "date"; day.value = index.issues?.[0]?.issueDate || "";
    label.appendChild(day); controls.appendChild(label);
    const load = el("button", "act", "Открыть отчёт"); controls.appendChild(load);
    const recent = el("select"); recent.setAttribute("aria-label", "Последние выпуски");
    for (const issue of index.issues || []) {
      const option = el("option", null, issue.issueDate); option.value = issue.issueDate;
      recent.appendChild(option);
    }
    controls.appendChild(recent);
    const result = el("div", "observation-report");
    body.replaceChildren(controls, result);
    const render = async () => {
      const request = ++observationGeneration;
      result.replaceChildren(el("p", "empty", "Загрузка отчёта…"));
      try {
        const report = await api("api/event-observations?date=" + encodeURIComponent(day.value));
        if (current !== "observations" || request !== observationGeneration) return;
        if (report.issueDate !== day.value) throw new Error("issue mismatch");
        const statuses = {pending: "Проверка готовится", partial: "Проверено частично", error: "Проверка не завершена"};
        const pairs = report.pairs || [];
        const suspects = pairs.filter(pair => pair.prediction.relation !== "different_event");
        result.replaceChildren(el("h2", null, statuses[report.status] || (suspects.length ? `Подозрений: ${suspects.length}` : "Среди проверенных пар подозрений нет")));
        if (report.coverage) result.appendChild(el("p", null, `Карточек: ${report.coverage.currentCards}; в истории за 45 дней: ${report.coverage.historicalCards}. Проверено ${report.coverage.checkedPairs} из ${report.coverage.candidatePairs} пар. Полнота поиска ещё не измерена.`));
        for (const error of report.errors || []) result.appendChild(el("p", null, error));
        if (!report.reportId) return;
        const storeKey = "radar-observation-labels:" + report.reportId;
        let pairLabels = {}, labels = {};
        try {
          pairLabels = JSON.parse(localStorage.getItem("radar-observation-pair-labels") || "{}");
          if (!pairLabels || typeof pairLabels !== "object" || Array.isArray(pairLabels)) pairLabels = {};
          labels = {...JSON.parse(localStorage.getItem(storeKey) || "{}"), ...pairLabels};
        } catch { /* local storage can be unavailable */ }
        const notice = el("p", null, "Оценки сохраняются в этом браузере. Скачайте разметку для добавления оператором в общий набор калибровки.");
        const renderPair = pair => {
          const p = pair.prediction;
          const section = el("section", "card observation-pair");
          const head = el("div", "head"); section.appendChild(head);
          head.appendChild(el("h3", null, `${observationLabels[p.relation]} · уверенность модели ${Math.round(p.confidence * 100)}%`));
          for (const side of ["left", "right"]) {
            const card = pair[side]; const line = el("p");
            const issueLink = el("a", null, card.issueDate); issueLink.href = "/issues/" + encodeURIComponent(card.issueDate);
            issueLink.target = "_blank"; issueLink.rel = "noopener noreferrer";
            line.appendChild(issueLink); line.appendChild(document.createTextNode(" · "));
            const link = el("a", null, card.title);
            if (/^https?:\/\//.test(card.url)) { link.href = card.url; link.target = "_blank"; link.rel = "noopener noreferrer"; }
            line.appendChild(link); head.appendChild(line);
          }
          if (p.commonEvent) head.appendChild(el("p", null, "Общее событие: " + p.commonEvent));
          head.appendChild(el("p", null, p.explanation));
          for (const side of ["Left", "Right"]) {
            const name = side === "Left" ? "Первый материал" : "Второй материал";
            if (p["evidence" + side]) head.appendChild(el("blockquote", null, name + ": " + p["evidence" + side]));
            if (p["uniqueFacts" + side]?.length) head.appendChild(el("p", null, name + ", уникальные факты: " + p["uniqueFacts" + side].join("; ")));
          }
          const field = el("label", null, "Редакторская оценка ");
          const select = el("select"); select.dataset.observationPair = pair.pairId;
          const empty = el("option", null, "Не оценено"); empty.value = ""; select.appendChild(empty);
          for (const [value, title] of Object.entries(observationLabels)) {
            if (value === "review_overlap") continue;
            const option = el("option", null, title); option.value = value; select.appendChild(option);
          }
          select.value = labels[pair.pairId] || "";
          select.addEventListener("change", () => {
            labels[pair.pairId] = select.value; pairLabels[pair.pairId] = select.value;
            try { localStorage.setItem(storeKey, JSON.stringify(labels)); localStorage.setItem("radar-observation-pair-labels", JSON.stringify(pairLabels)); }
            catch { notice.textContent = "Браузер не сохранил оценки. Скачайте разметку до закрытия страницы."; }
          });
          field.appendChild(select); head.appendChild(field); return section;
        };
        for (const pair of suspects) result.appendChild(renderPair(pair));
        const other = el("details"); other.appendChild(el("summary", null, `Остальные проверенные пары · ${pairs.length - suspects.length}`));
        for (const pair of pairs.filter(pair => pair.prediction.relation === "different_event")) other.appendChild(renderPair(pair));
        result.appendChild(other); result.appendChild(notice);
        const exportLabels = el("button", "act", "Скачать разметку");
        exportLabels.addEventListener("click", () => {
          const allowed = new Set(pairs.map(pair => pair.pairId));
          const exported = Object.fromEntries(Object.entries(labels).filter(([id, value]) => allowed.has(id) && ["same_event", "different_event", "development", "insufficient_evidence"].includes(value)));
          downloadObservation({reportId: report.reportId, labels: exported}, `radar-${report.issueDate}-event-labels.json`);
        });
        result.appendChild(exportLabels);
        const exportReport = el("button", "act", "Скачать отчёт JSON");
        exportReport.addEventListener("click", () => downloadObservation(report, `radar-${report.issueDate}-observations.json`));
        result.appendChild(exportReport);
      } catch {
        if (current === "observations" && request === observationGeneration) result.replaceChildren(el("p", "empty", "Отчёт недоступен. Это не означает отсутствия повторов. Повторите загрузку."));
      }
    };
    load.addEventListener("click", render);
    recent.addEventListener("change", () => {day.value = recent.value; void render();});
    if (day.value) await render();
  } catch {
    if (current === "observations") body.replaceChildren(el("p", "empty", "Список выпусков временно недоступен."));
  }
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

async function api(path, options) {
  const response = await fetch(path, options || {});
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}

async function drawNav() {
  const summary = await api("api/summary");
  nav.replaceChildren();
  // Two groups, because they answer different questions. The first is "what is
  // waiting for me"; a reference tab that always reads zero, standing among
  // them, teaches the eye to skip a zero.
  const waiting = summary.queues.filter(queue => !queue.reference);
  const reference = summary.queues.filter(queue => queue.reference);
  for (const queue of waiting) {
    const button = el("button", queue.key === current ? "on" : "", queue.title);
    const count = el("span", "n", queue.count);
    button.appendChild(count);
    button.addEventListener("click", () => show(queue.key));
    nav.appendChild(button);
  }
  if (reference.length) {
    nav.appendChild(el("span", "sep", "справочно"));
    for (const queue of reference) {
      const button = el("button", queue.key === current ? "on ref" : "ref", queue.title);
      button.addEventListener("click", () => show(queue.key));
      nav.appendChild(button);
    }
  }
  const observations = el("button", current === "observations" ? "on" : "", "Повторы выпусков");
  observations.addEventListener("click", () => showObservations());
  nav.appendChild(observations);
  const docs = el("button", current === "docs" ? "on" : "", "Документы");
  docs.addEventListener("click", () => showDocs());
  nav.appendChild(docs);
  const keys = el("button", current === "keys" ? "on" : "", "Ключи");
  keys.addEventListener("click", () => showKeys());
  nav.appendChild(keys);
}

function childRow(queueKey, itemId, child, scope) {
  const row = el("div", "child");
  const left = el("div");
  left.appendChild(el("blockquote", null, child.quote));
  const src = el("div", "src");
  if (child.sourceUrl) {
    const link = el("a", null, child.sourceUrl);
    link.href = child.sourceUrl;
    link.target = "_blank";
    link.rel = "noreferrer noopener";
    src.appendChild(link);
  }
  const bits = [];
  if (child.span) bits.push("символы " + child.span);
  if (child.membershipClass) bits.push(child.membershipClass);
  if (child.relevance !== null && child.relevance !== undefined) {
    bits.push("совпадение слов " + Math.round(child.relevance * 100) + "%");
  }
  if (bits.length) src.appendChild(document.createTextNode(" · " + bits.join(" · ")));
  left.appendChild(src);
  row.appendChild(left);

  const buttons = el("div", "buttons");
  for (const action of child.actions || []) {
    const button = el("button", "act " + action.kind, action.label);
    button.addEventListener("click", () =>
      decide(button, scope || row, queueKey, itemId + "/" + child.id, action.action));
    buttons.appendChild(button);
  }
  row.appendChild(buttons);
  return row;
}

function card(queueKey, item) {
  const section = el("section", "card");
  const head = el("div", "head");
  head.appendChild(el("div", "primary", item.primary));
  if (item.secondary) head.appendChild(el("div", "secondary", item.secondary));
  if (item.meta.length) {
    const meta = el("div", "meta");
    for (const entry of item.meta) {
      const span = el("span");
      span.appendChild(el("b", null, entry.label + ": "));
      span.appendChild(document.createTextNode(entry.value));
      meta.appendChild(span);
    }
    head.appendChild(meta);
  }
  if (item.actions.length) {
    const buttons = el("div", "buttons");
    buttons.style.marginTop = "10px";
    for (const action of item.actions) {
      const button = el("button", "act " + action.kind, action.label);
      button.addEventListener("click", () =>
        decide(button, section, queueKey, item.id, action.action));
      buttons.appendChild(button);
    }
    head.appendChild(buttons);
  }
  section.appendChild(head);
  // An exclusive card is decided once: picking one child finishes the whole
  // card, so the scope handed to decide() is the card and not the row.
  for (const child of item.children || [])
    section.appendChild(childRow(queueKey, item.id, child, item.exclusive ? section : null));
  return section;
}

async function decide(button, container, queueKey, itemId, action) {
  const buttons = container.querySelectorAll("button.act");
  buttons.forEach((b) => (b.disabled = true));
  try {
    await api("api/decide", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ queue: queueKey, id: itemId, action: action }),
    });
    container.classList.add("done");
    drawNav();
  } catch (error) {
    buttons.forEach((b) => (b.disabled = false));
    alert("Не удалось: " + error.message);
  }
}

async function show(key) {
  current = key;
  body.replaceChildren(el("p", "empty", "Загрузка…"));
  await drawNav();
  const queue = await api("api/queue?key=" + encodeURIComponent(key));
  why.textContent = queue.why;
  body.replaceChildren();
  if (!queue.items.length) {
    body.appendChild(el("p", "empty", queue.empty));
    return;
  }
  for (const item of queue.items) body.appendChild(card(key, item));
}

async function showDocs(name) {
  current = "docs";
  await drawNav();
  if (!name) {
    const list = await api("api/docs");
    why.textContent =
      "Документы по каждому срезу: что сделано, что измерено и что осталось за вами.";
    body.replaceChildren();
    const ul = el("ul", "docs");
    for (const doc of list.documents) {
      const li = el("li");
      const link = el("a", null, doc.title);
      link.href = "#";
      link.addEventListener("click", (event) => {
        event.preventDefault();
        showDocs(doc.name);
      });
      li.appendChild(link);
      li.appendChild(el("span", "src", " · " + doc.size + " КБ"));
      ul.appendChild(li);
    }
    body.replaceChildren(ul);
    return;
  }
  const doc = await api("api/doc?name=" + encodeURIComponent(name));
  why.textContent = doc.name;
  const back = el("a", "back", "← ко всем документам");
  back.href = "#";
  back.addEventListener("click", (event) => {
    event.preventDefault();
    showDocs();
  });
  const article = el("article", "doc");
  article.innerHTML = doc.html;
  body.replaceChildren(back, article);
}

drawNav().then(() => location.hash === "#event-observations" ? showObservations() : show("comparison"));

// -- Subscription keys: issued here, shown once, revoked here ------------------
// The whole key exists only in the answer to "выдать"; the list that follows
// carries prefixes. Copy it before leaving the screen - nothing can show it
// again, and that is the point.

async function showKeys() {
  current = "keys";
  await drawNav();
  const list = await api("api/keys");
  why.textContent =
    "Ключи подписки: разговор с агентом свободен, блуждание по базе — по ключу. " +
    "Полный ключ показывается один раз, при выдаче; в списке остаются только префиксы.";
  body.replaceChildren();

  const form = el("div", "keys__form");
  const days = el("input", "keys__days");
  days.type = "number";
  days.min = "1";
  days.max = "3650";
  days.value = "30";
  const note = el("input", "keys__note");
  note.type = "text";
  note.placeholder = "кому выдан (заметка владельца)";
  const issue = el("button", "keys__issue", "выдать ключ");
  const issued = el("div", "keys__issued");
  issue.addEventListener("click", async () => {
    issue.disabled = true;
    try {
      const made = await api("api/keys/issue", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ days: Number(days.value), note: note.value }),
      });
      issued.replaceChildren();
      const warn = el("p", "keys__warn",
        "Ключ показан один раз — скопируйте сейчас и отправьте подписчику:");
      const key = el("code", "keys__key", made.key);
      issued.replaceChildren(warn, key);
      showKeys();
    } catch (error) {
      issued.replaceChildren(el("p", "keys__warn", String(error.message || error)));
    } finally {
      issue.disabled = false;
    }
  });
  form.append(el("span", null, "срок, дней:"), days, note, issue, issued);
  body.appendChild(form);

  const table = el("table", "keys__table");
  const head = el("tr");
  for (const title of ["префикс", "план", "статус", "до", "заметка", "выдан", "использован", ""]) {
    head.appendChild(el("th", null, title));
  }
  table.appendChild(head);
  for (const key of list.keys) {
    const row = el("tr", key.status === "revoked" ? "keys__row--revoked" : "");
    row.appendChild(el("td", "mono", key.key_prefix + "…"));
    row.appendChild(el("td", null, key.plan));
    row.appendChild(el("td", null, key.status === "active" ? "действует" : "отозван"));
    row.appendChild(el("td", null, String(key.expires_at || "").slice(0, 10)));
    row.appendChild(el("td", null, (key.note || "").split("\n")[0]));
    row.appendChild(el("td", null, String(key.created_at || "").slice(0, 10)));
    row.appendChild(el("td", null, key.last_used_at ? String(key.last_used_at).slice(0, 10) : "—"));
    const actions = el("td", "keys__actions");
    if (key.status === "active") {
      const revoke = el("button", null, "отозвать");
      revoke.addEventListener("click", () =>
        api("api/keys/revoke", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ id: key.keyId }),
        }).then(() => showKeys(), () => showKeys()));
      actions.appendChild(revoke);
    }
    const extend = el("button", null, "+30 дней");
    extend.addEventListener("click", () =>
      api("api/keys/extend", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: key.keyId, days: 30 }),
      }).then(() => showKeys(), () => showKeys()));
    actions.appendChild(extend);
    row.appendChild(actions);
    table.appendChild(row);
  }
  body.appendChild(table);
}
