"use strict";

class FakeElement {
  constructor(tag = "div") {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.attributes = new Map();
    this.className = "";
    this.textContent = "";
    this.value = "";
    this.classList = { add: (...names) => names.forEach((name) => this.attributes.set(`class:${name}`, "")) };
  }

  append(...children) {
    this.children.push(...children);
  }

  replaceChildren(...children) {
    this.children = [...children];
  }

  setAttribute(name, value) {
    this.attributes.set(name, value);
    if (name === "value") this.value = value;
  }

  addEventListener() {}
  focus() {}
}

const content = new FakeElement("main");
const health = new FakeElement("span");
const unhandled = [];
process.on("unhandledRejection", (error) => unhandled.push(error));

globalThis.HTMLElement = FakeElement;
globalThis.document = {
  title: "",
  querySelector(selector) {
    if (selector === "#content") return content;
    if (selector === "#health") return health;
    return null;
  },
  createElement(tag) {
    return new FakeElement(tag);
  },
  createTextNode(text) {
    return { textContent: text };
  },
};
globalThis.window = {
  history: { replaceState() {} },
  location: {
    href: "https://radar.test/",
    pathname: "/",
    search: "",
  },
};
globalThis.fetch = async (path) => {
  const payload = path === "/api/health"
    ? {
        databaseStateHash: "a".repeat(64),
        releaseId: "release_console_smoke",
        schemaVersion: 1,
        status: "ok",
      }
    : {
        analysis: {
          blocks: [{ kind: "overview", text: "Нет материалов.", title: "Итог" }],
          brief: "Детерминированный выпуск.",
          headline: null,
        },
        brief: "Пустой выпуск.",
        issueDate: "2026-08-21",
        issueNumber: 76,
        llm: { effectiveModel: null, status: "unavailable" },
        materialCount: 0,
        materials: [],
        publishedAt: "2026-08-21T05:10:00Z",
        stats: { adjacent: 0, core: 0, cut: 1, far: 0, included: 0, mid: 0, near: 0, viewed: 1 },
        theses: [],
        title: "Console smoke",
      };
  return { ok: true, status: 200, async json() { return payload; } };
};

await import("../apps/web/app.mjs");
await new Promise((resolve) => setTimeout(resolve, 25));

if (unhandled.length || health.textContent !== "Данные актуальны" || content.children.length < 3) {
  throw new Error("frontend console smoke did not render the empty/no-LLM route cleanly");
}

process.stdout.write("Frontend console smoke: PASS (empty/no-LLM route)\n");
