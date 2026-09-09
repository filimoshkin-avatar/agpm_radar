// Real layout, scrolling and keyboard checks using the existing QA browser.
// Run: node tools/frontend_issue_nav_browser_smoke.mjs
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
const { chromium } = await import(process.env.RADAR_PLAYWRIGHT_MODULE || '../../work/qa/node_modules/playwright/index.mjs');
const web = new URL('../apps/web/', import.meta.url);
const materials = Array.from({ length: 24 }, (_, i) => ({
  id: `nav-${i}`, title: `Сигнал ${i + 1}: агентные системы в управлении проектами`,
  perimeter: ['near', 'mid', 'far'][i % 3], rubrics: ['workflow_orchestration'],
  issueDate: '2026-09-09', url: `https://example.test/${i}`,
  brief: 'Оркестрация рабочих процессов и проверка результатов агентной работы.',
  agpmTakeaway: 'Проверить ответственность человека и подтверждение каждого решения.',
  verdict: 'core', llm: { status: 'fallback' },
}));
const stats = { viewed: 40, included: 24, cut: 16, near: 8, mid: 8, far: 8 };
const issue = {
  issueDate: '2026-09-09', issueNumber: 95, materials, stats,
  theses: [{ lead: 'Агенты переходят к исполнению', rest: 'Контроль результата остаётся за человеком.' }],
  analysis: { headline: 'От эксперимента к рабочему процессу', blocks: [
    { kind: 'overview', text: 'Организации проверяют агентные рабочие процессы. '.repeat(15) },
    { kind: 'why_agpm', text: 'Нужны измеримые результаты и ответственность.' },
    { kind: 'actions', text: 'Следить за практикой внедрения.' },
  ] },
};
const browser = await chromium.launch({ headless: true });
try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 800 }, reducedMotion: 'reduce' });
  await context.route('https://radar.test/**', async route => {
    const url = new URL(route.request().url());
    if (url.pathname.startsWith('/api/')) {
      let json = {};
      if (url.pathname === '/api/latest') json = issue;
      else if (url.pathname.startsWith('/api/issues/')) json = { ...issue, issueDate: url.pathname.split('/').at(-1), analysis: null };
      else if (url.pathname === '/api/rubric-catalog') json = [{ id: 'workflow_orchestration', label: 'Оркестрация', title: 'Оркестрация' }];
      else if (url.pathname === '/api/rubrics') json = [{ id: 'workflow_orchestration', label: 'Оркестрация', title: 'Оркестрация', count: 24 }];
      else if (url.pathname === '/api/materials') json = { items: materials, nextCursor: null };
      else if (url.pathname === '/api/stats') json = stats;
      else if (url.pathname === '/api/timeseries') json = { items: [{ date: issue.issueDate, ...stats }] };
      else if (url.pathname === '/api/sources') json = [{ name: 'Источники выпуска', included: 24 }];
      else if (url.pathname === '/api/issues' || url.pathname === '/api/gazettes') json = { items: [] };
      return route.fulfill({ json });
    }
    const relative = url.pathname.startsWith('/assets/') ? url.pathname.slice(8) : 'index.html';
    const contentType = relative.endsWith('.mjs') ? 'text/javascript' : relative.endsWith('.css') ? 'text/css' : relative.endsWith('.ttf') ? 'font/ttf' : 'text/html';
    return route.fulfill({ body: await fs.readFile(new URL(relative, web)), contentType });
  });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(String(error)));
  const ready = () => page.waitForFunction(() => !document.querySelector('#issueNav').hidden && !document.querySelector('#columns').classList.contains('loading'));
  const railButton = id => page.locator(`#issueNavRail [data-issue-target="${id}"]`);
  const active = async id => {
    try {
      await page.waitForFunction(id => document.querySelector('#issueNavRail [aria-current="location"]')?.dataset.issueTarget === id, id, { timeout: 5000 });
    } catch (error) {
      console.error(await page.evaluate(() => ({ scrollY, height: innerHeight, total: document.documentElement.scrollHeight,
        current: document.querySelector('#issueNavRail [aria-current]')?.dataset.issueTarget,
        sections: [...document.querySelectorAll('[data-issue-section]')].map(node => [node.id, node.getBoundingClientRect().top]),
      })));
      throw error;
    }
  };
  const aligned = id => page.waitForFunction(id => {
    const top = document.getElementById(id).getBoundingClientRect().top;
    const header = document.querySelector('.topbar').getBoundingClientRect().bottom;
    return top >= header && top < header + 24;
  }, id);
  await page.goto('https://radar.test/?perimeter=near'); await ready();
  const originalURL = page.url();
  assert.equal(await page.locator('#issueNavRail li:visible').count(), 6);
  await active('issueOverview');
  assert.equal(await page.locator('#issueNavToggle').isVisible(), false);
  const navBox = await page.locator('#issueNavRail').boundingBox();
  const cardsBox = await page.locator('#columns').boundingBox();
  assert.ok(cardsBox.x + cardsBox.width <= navBox.x, 'rail has its own gutter');
  await railButton('dailyAnalysis').focus();
  assert.equal(await page.locator('#issueNavTooltip').isVisible(), true);
  assert.equal(await page.locator('#issueNavTooltip').innerText(), 'Аналитический разбор');
  await page.keyboard.press('Enter');
  assert.equal(await page.locator('#dailyAnalysis').getAttribute('open'), '');
  await aligned('dailyAnalysis'); await active('dailyAnalysis');
  assert.equal(await page.evaluate(() => document.activeElement.tagName), 'SUMMARY');
  await railButton('columns').click(); await aligned('columns'); await active('columns');
  assert.equal(await page.evaluate(() => document.activeElement.id), 'columns');
  assert.equal(page.url(), originalURL, 'navigation preserves issue and filters');
  await page.evaluate(() => window.scrollTo({ top: scrollY + document.getElementById('issueTrends').getBoundingClientRect().top - document.querySelector('.topbar').getBoundingClientRect().bottom - 19, behavior: 'instant' }));
  await active('issueTrends');
  await railButton('issueSources').click(); await active('issueSources');
  await railButton('issueOverview').focus(); await page.keyboard.press('ArrowDown');
  assert.equal(await page.evaluate(() => document.activeElement.dataset.issueTarget), 'dailyAnalysis');
  await page.keyboard.press('End');
  assert.equal(await page.evaluate(() => document.activeElement.dataset.issueTarget), 'issueSources');
  await page.locator('[data-period="7d"]').click(); await ready();
  await page.waitForFunction(() => !document.querySelector('#issueNavRail [data-issue-target="dailyAnalysis"]'));
  assert.equal(await page.locator('#issueNavRail li:visible').count(), 5);
  await page.locator('[data-period="issue"]').click(); await ready();
  await page.waitForFunction(() => document.querySelector('#issueNavRail [data-issue-target="dailyAnalysis"]'));
  for (const mode of ['gazette', 'agent', 'radar']) {
    await page.locator(`.topbar [data-view-mode="${mode}"]`).click();
    await page.waitForFunction(mode => document.querySelector('#issueNav').hidden === (mode === 'gazette'), mode);
  }
  await railButton('issueOverview').click(); await aligned('issueOverview');
  await page.emulateMedia({ reducedMotion: 'no-preference' });
  const buttonBox = await railButton('columns').boundingBox();
  await page.mouse.move(buttonBox.x + buttonBox.width - 2, buttonBox.y + buttonBox.height / 2);
  assert.ok(Number(await railButton('columns').evaluate(node => node.style.getPropertyValue('--issue-proximity'))) > 2);
  await page.screenshot({ path: '/tmp/radar-issue-nav-desktop.png' });
  await page.mouse.move(100, 100);
  assert.equal(await railButton('columns').evaluate(node => node.style.getPropertyValue('--issue-proximity')), '');
  await page.emulateMedia({ reducedMotion: 'reduce' });
  assert.equal(await railButton('columns').locator('.issue-nav__dash').evaluate(node => getComputedStyle(node).transitionDuration), '0s');
  await page.setViewportSize({ width: 390, height: 844 });
  const toggle = page.locator('#issueNavToggle');
  const menu = page.locator('#issueNavMenu');
  assert.equal(await toggle.isVisible(), true);
  assert.equal(await page.locator('#issueNavRail').isVisible(), false);
  await toggle.click();
  assert.equal(await toggle.getAttribute('aria-expanded'), 'true');
  assert.equal(await page.evaluate(() => document.querySelector('#issueNavMenu').contains(document.activeElement)), true);
  await page.screenshot({ path: '/tmp/radar-issue-nav-mobile.png' });
  assert.ok((await menu.boundingBox()).y >= (await page.locator('.topbar').boundingBox()).height);
  for (const button of await menu.locator('li:visible button').all()) assert.ok((await button.boundingBox()).height >= 40);
  await page.keyboard.press('Escape');
  assert.equal(await menu.isVisible(), false);
  assert.equal(await page.evaluate(() => document.activeElement.id), 'issueNavToggle');
  await toggle.click(); await page.locator('#thesesTitle').click();
  assert.equal(await menu.isVisible(), false);
  await toggle.click(); await menu.locator('[data-issue-target="columns"]').click();
  await aligned('columns'); await active('columns');
  assert.equal(await menu.isVisible(), false);
  assert.equal(await page.evaluate(() => document.activeElement.id), 'columns');
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  await toggle.click();
  await page.setViewportSize({ width: 1440, height: 960 });
  assert.equal(await menu.isVisible(), false);
  await page.emulateMedia({ media: 'print' });
  assert.equal(await page.locator('#issueNav').isVisible(), false);
  await page.emulateMedia({ media: 'screen' });
  await page.goto('https://radar.test/issues/2026-09-08'); await ready();
  assert.equal(await page.locator('#issueNavRail li:visible').count(), 5, 'issue without analysis has no dead anchor');
  assert.deepEqual(errors, []);
  console.log('Issue navigator browser smoke: PASS (scroll, analysis, filters, periods, modes, keyboard, proximity, mobile, reduced motion, print)');
} finally { await browser.close(); }
