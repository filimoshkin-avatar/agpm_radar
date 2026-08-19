const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");

const OUT = "/mnt/vdd/Radar/work/qa-screenshots";
const BASE_URL = process.env.RADAR_QA_URL || "http://127.0.0.1:8780/";
fs.mkdirSync(OUT, { recursive: true });

const cases = [
  { name: "desktop-issue", viewport: { width: 1440, height: 1400 }, actions: async () => {} },
  {
    name: "desktop-yesterday",
    viewport: { width: 1440, height: 1400 },
    actions: async page => {
      await page.click('[data-period="yesterday"]');
      await page.waitForTimeout(500);
    },
  },
  {
    name: "desktop-7d",
    viewport: { width: 1440, height: 1400 },
    actions: async page => {
      await page.click('[data-period="7d"]');
      await page.waitForTimeout(500);
    },
  },
  {
    name: "desktop-30d",
    viewport: { width: 1440, height: 1600 },
    actions: async page => {
      await page.click('[data-period="30d"]');
      await page.waitForTimeout(500);
    },
  },
  {
    name: "desktop-30d-multi-rubric",
    viewport: { width: 1440, height: 1600 },
    actions: async page => {
      await page.click('[data-period="30d"]');
      const rubrics = page.locator('[data-rubric]');
      await rubrics.nth(0).click();
      await rubrics.nth(1).click();
      await page.waitForTimeout(500);
    },
  },
  {
    name: "mobile-yesterday",
    viewport: { width: 390, height: 1600 },
    actions: async page => {
      await page.click('[data-period="yesterday"]');
      await page.waitForTimeout(500);
    },
  },
];

function visibleOverlap(rects) {
  const overlaps = [];
  for (let i = 0; i < rects.length; i += 1) {
    for (let j = i + 1; j < rects.length; j += 1) {
      const a = rects[i];
      const b = rects[j];
      const intersects = a.right > b.left && b.right > a.left && a.bottom > b.top && b.bottom > a.top;
      if (intersects) overlaps.push([a.label, b.label]);
    }
  }
  return overlaps.slice(0, 20);
}

(async () => {
  const browser = await chromium.launch();
  const results = [];
  for (const item of cases) {
    const page = await browser.newPage({ viewport: item.viewport });
    const errors = [];
    page.on("console", msg => {
      if (["error", "warning"].includes(msg.type())) errors.push(`${msg.type()}: ${msg.text()}`);
    });
    page.on("pageerror", err => errors.push(`pageerror: ${err.message}`));
    const response = await page.goto(BASE_URL, { waitUntil: "networkidle" });
    await item.actions(page);
    await page.screenshot({ path: path.join(OUT, `${item.name}.png`), fullPage: true });
    const metrics = await page.evaluate(() => {
      const body = document.body;
      const controls = [...document.querySelectorAll(".topbar, .filters, .summary-grid, .today, .column, .panel, .timeline-row, .rubric-cell")]
        .map((el, index) => {
          const r = el.getBoundingClientRect();
          return { label: `${el.className || el.tagName}-${index}`, left: r.left, top: r.top, right: r.right, bottom: r.bottom, width: r.width, height: r.height };
        })
        .filter(r => r.width > 0 && r.height > 0 && r.bottom >= 0 && r.top <= window.innerHeight);
      return {
        title: document.title,
        scrollWidth: body.scrollWidth,
        innerWidth: window.innerWidth,
        overflowX: body.scrollWidth > window.innerWidth + 1,
        cards: document.querySelectorAll(".card").length,
        columns: [...document.querySelectorAll(".column")].map(el => ({
          title: el.querySelector(".column__title")?.textContent?.trim(),
          cards: el.querySelectorAll(".card").length,
        })),
        activeFilter: document.querySelector("#activeFilter")?.textContent || "",
        apiError: !!document.querySelector(".api-error"),
        missingSections: ["#trendBars", "#heatmap", "#timeline", "#rubricator", "#footerSources"].filter(selector => {
          const el = document.querySelector(selector);
          return !el || !el.textContent.trim() && !el.children.length;
        }),
        controlRects: controls,
      };
    });
    const overlaps = visibleOverlap(metrics.controlRects);
    delete metrics.controlRects;
    results.push({ name: item.name, status: response && response.status(), errors, metrics: { ...metrics, overlaps } });
    await page.close();
  }
  await browser.close();
  fs.writeFileSync(path.join(OUT, "qa-results.json"), JSON.stringify(results, null, 2), "utf8");
  console.log(JSON.stringify(results, null, 2));
})();
