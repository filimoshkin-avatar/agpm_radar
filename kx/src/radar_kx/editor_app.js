"use strict";
// No inline script and no inline handlers: radar.agpm.space serves a strict CSP
// (script-src 'self'), and the editor lives under it. Everything is bound here.

const nav = document.getElementById("nav");
const body = document.getElementById("body");
const why = document.getElementById("why");
let current = null;

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
  for (const queue of summary.queues) {
    const button = el("button", queue.key === current ? "on" : "", queue.title);
    const count = el("span", "n", queue.count);
    button.appendChild(count);
    button.addEventListener("click", () => show(queue.key));
    nav.appendChild(button);
  }
  const docs = el("button", current === "docs" ? "on" : "", "Документы");
  docs.addEventListener("click", () => showDocs());
  nav.appendChild(docs);
}

function childRow(queueKey, itemId, child) {
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
      decide(button, row, queueKey, itemId + "/" + child.id, action.action));
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
  for (const child of item.children || []) section.appendChild(childRow(queueKey, item.id, child));
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

drawNav().then(() => show("comparison"));
