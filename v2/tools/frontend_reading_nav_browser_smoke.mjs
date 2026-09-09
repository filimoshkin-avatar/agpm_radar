// Browser integration for the newspaper frame and dynamic agent reading surfaces.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
const { chromium } = await import(process.env.RADAR_PLAYWRIGHT_MODULE || '../../work/qa/node_modules/playwright/index.mjs');
const web = new URL('../apps/web/', import.meta.url);
const answered = { answer: 'Ответ с проверкой результата. '.repeat(45), clauses: [], evidence: [], refused: false };
const turns = Array.from({ length: 20 }, (_, i) => ({ question: `Как проверить этап ${i + 1}?`, at: '12:00', answered }));
const wikiBody = '# Начало методики\n' + 'Порядок работы.\n'.repeat(35)
  + '## Проверка результата\n' + 'Критерии проверки.\n'.repeat(35)
  + '```text\n# Это пример кода\n```\n## Следующий шаг\n<script>unsafe()</script>\n';
const newspaper = month => `<!doctype html><html lang="ru"><head><style>
  body { margin:0; padding:24px; font:16px/1.5 serif; } h1,h2,h3,h4 { margin:0 0 16px; }
  .intro { height:360px; } .cols { display:grid; grid-template-columns:1fr 1fr; gap:24px; }
  article { min-height:360px; } @media(max-width:600px) { .cols { grid-template-columns:1fr; } }
  </style></head><body><h1>Газета ${month}</h1><section class="intro"><h2>Главное за ${month}</h2></section>
  <div class="cols"><section><h3>Практика ${month}</h3><article><h4>Первый материал ${month}</h4></article>
  <article><h4>Третий материал ${month}</h4></article></section><section><h3>Рынок ${month}</h3>
  <article><h4>Второй материал ${month}</h4></article><article><h4>Четвёртый материал ${month}</h4></article></section></div>
  <section style="height:300px"><h3>Итоги ${month}</h3></section></body></html>`;
const browser = await chromium.launch({ headless: true });
try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, reducedMotion: 'reduce' });
  let releaseGazette = null, releaseAnswer = null;
  await context.route('https://radar.test/**', async route => {
    const url = new URL(route.request().url());
    let json = {};
    if (url.pathname.startsWith('/api/')) {
      if (url.pathname === '/api/latest') json = { issueDate: '2026-09-09', materials: [{ id: 'a', title: 'Сигнал', perimeter: 'near', rubrics: [], url: 'https://example.test/a' }], stats: { included: 1, near: 1 }, theses: [] };
      else if (url.pathname === '/api/gazettes') json = { items: [
        { period: '2026-09', url: '/gazettes/new.html', publishedAt: '2026-09-01', title: 'Сентябрь' },
        { period: '2026-08', url: '/gazettes/old.html', publishedAt: '2026-08-01', title: 'Август' },
      ] };
      else if (['/api/rubric-catalog', '/api/rubrics', '/api/sources'].includes(url.pathname)) json = [];
      return route.fulfill({ json });
    }
    if (url.pathname.startsWith('/gazettes/')) {
      if (url.pathname.includes('old') && releaseGazette) await releaseGazette;
      return route.fulfill({ contentType: 'text/html', body: newspaper(url.pathname.includes('old') ? 'август' : 'сентябрь') });
    }
    if (url.pathname.startsWith('/kb/')) {
      if (url.pathname === '/kb/chat/stream') {
        if (releaseAnswer) await releaseAnswer;
        return route.fulfill({ contentType: 'text/event-stream', body: `event: result\ndata: ${JSON.stringify(answered)}\n\n` });
      }
      if (url.pathname === '/kb/pages') json = { pages: [{ relative_path: 'method.md', title: 'Методика', chars: wikiBody.length }] };
      else if (url.pathname.startsWith('/kb/pages/')) json = { title: 'Методика', body: wikiBody };
      else if (url.pathname === '/kb/search') json = { hits: Array.from({ length: 3 }, (_, i) => ({ claim_id: String(i), statement: `Утверждение ${i + 1}`, quote_text: 'Цитата. '.repeat(50), source_url: 'https://example.test/' })) };
      else if (url.pathname === '/kb/prompts') json = { prompts: [{ question: 'Как начать?', category: 'find' }] };
      return route.fulfill({ json });
    }
    const file = url.pathname.startsWith('/assets/') ? url.pathname.slice(8) : 'index.html';
    return route.fulfill({ body: await fs.readFile(new URL(file, web)), contentType: file.endsWith('.mjs') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : file.endsWith('.ttf') ? 'font/ttf' : 'text/html' });
  });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(String(error)));
  const mode = name => page.locator(`.topbar [data-view-mode="${name}"]`).click();
  const rail = page.locator('#issueNavRail');
  const navButton = label => rail.getByRole('button', { name: label, exact: true });
  const count = n => page.waitForFunction(n => document.querySelectorAll('#issueNavRail button').length === n, n);
  await page.goto('https://radar.test/');
  await page.waitForFunction(() => document.querySelectorAll('#columns .card').length > 0);
  await mode('gazette'); await count(9);
  assert.equal(await page.locator('#issueNavTitle').innerText(), 'По газете');
  assert.deepEqual(await rail.locator('button').allTextContents(), [
    'Начало номера', 'Главное за сентябрь', 'Практика сентябрь', 'Рынок сентябрь',
    'Первый материал сентябрь', 'Второй материал сентябрь', 'Третий материал сентябрь', 'Четвёртый материал сентябрь', 'Итоги сентябрь',
  ]);
  const frame = page.frameLocator('.gazette-frame');
  await navButton('Второй материал сентябрь').click();
  await page.waitForFunction(() => {
    const frame = document.querySelector('.gazette-frame'), target = frame.contentDocument.activeElement;
    const top = frame.getBoundingClientRect().top + target.getBoundingClientRect().top;
    const header = Math.max(document.querySelector('.topbar').getBoundingClientRect().bottom, document.querySelector('.gazette-bar').getBoundingClientRect().bottom);
    return target.textContent === 'Второй материал сентябрь' && top >= header && top < header + 24;
  });
  await page.waitForFunction(() => document.querySelector('#issueNavRail [aria-current]')?.textContent === 'Второй материал сентябрь');
  assert.equal(await page.locator('.gazette-frame').getAttribute('sandbox'), 'allow-same-origin allow-modals');
  assert.equal(await frame.locator('body').evaluate(node => node.ownerDocument.defaultView.scrollY), 0);
  await navButton('Второй материал сентябрь').hover();
  await page.screenshot({ path: '/tmp/radar-reading-gazette-desktop.png' });
  const desktopHeight = await page.locator('.gazette-frame').evaluate(node => node.offsetHeight);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.waitForFunction(height => document.querySelector('.gazette-frame').offsetHeight > height + 500, desktopHeight);
  await page.locator('#issueNavToggle').click();
  await page.screenshot({ path: '/tmp/radar-reading-gazette-mobile.png' });
  await frame.getByRole('heading', { name: 'Первый материал сентябрь', exact: true }).click();
  assert.equal(await page.locator('#issueNavMenu').isVisible(), false, 'clicks inside the sandboxed frame close the outline');
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.waitForFunction(height => Math.abs(document.querySelector('.gazette-frame').offsetHeight - height) < 3, desktopHeight);
  let finishGazette;
  releaseGazette = new Promise(resolve => { finishGazette = resolve; });
  await page.locator('#gazetteIssue').click();
  await page.locator('[data-gazette-issue="2026-08"]').click();
  await page.waitForFunction(() => document.querySelector('#issueNav').hidden);
  finishGazette(); releaseGazette = null;
  await navButton('Главное за август').waitFor();
  assert.equal(await navButton('Главное за сентябрь').count(), 0);
  await mode('agent'); await count(2);
  await page.locator('[data-agent-tab="wiki"]').click();
  assert.equal(await page.locator('#agentSubPanel').isVisible(), true);
  assert.equal(await page.locator('#issueNavTitle').innerText(), 'По диалогу');
  // Isolated fixture subscription and conversation, never a production key or request.
  await page.evaluate(turns => {
    localStorage.setItem('radarAgentChat.v1', JSON.stringify({ session: 'fixture', turns }));
    localStorage.setItem('radarAccess.v1', JSON.stringify({ key: 'fixture-access' }));
  }, turns);
  await page.reload(); await mode('agent'); await count(20);
  await navButton('3. Как проверить этап 3?').click();
  await page.waitForFunction(() => document.activeElement.dataset.turn === '2');
  await page.waitForFunction(() => document.querySelector('#chatHistoryPos').textContent.includes('вопрос 3 из 20'));
  await page.setViewportSize({ width: 1440, height: 500 });
  await rail.locator('button').first().focus(); await page.keyboard.press('End');
  const last = await rail.locator('button').last().boundingBox(), railBox = await rail.boundingBox();
  assert.ok(last.y >= railBox.y && last.y + last.height <= railBox.y + railBox.height + 1);
  assert.equal(await page.locator('#issueNavTooltip').isVisible(), true);
  await page.setViewportSize({ width: 1440, height: 900 });
  await navButton('3. Как проверить этап 3?').hover();
  await page.screenshot({ path: '/tmp/radar-reading-agent-desktop.png' });
  let finishAnswer;
  releaseAnswer = new Promise(resolve => { finishAnswer = resolve; });
  await page.locator('#agentQuestion').fill('Новый вопрос'); await page.locator('#agentSend').click();
  await navButton('21. Новый вопрос').waitFor();
  assert.ok(!(await rail.innerText()).includes('NaN'));
  finishAnswer(); releaseAnswer = null;
  await page.waitForFunction(() => document.querySelector('#agentThread [data-turn="20"]'));
  await count(21);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.locator('#issueNavToggle').click();
  const toggleBox = await page.locator('#issueNavToggle').boundingBox(), composer = await page.locator('#agentComposer').boundingBox();
  assert.ok(toggleBox.y + toggleBox.height <= composer.y, 'outline never covers the composer');
  await page.screenshot({ path: '/tmp/radar-reading-agent-mobile.png' });
  await page.keyboard.press('Escape');
  await page.locator('#newDialog').click(); await count(2);
  assert.equal(await navButton('21. Новый вопрос').count(), 0);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.locator('[data-agent-tab="wiki"]').click();
  await page.locator('[data-agent-page="method.md"]').click();
  await navButton('Проверка результата').waitFor();
  assert.equal(await page.locator('.agent-page').textContent(), wikiBody, 'plain text remains byte-for-byte unchanged');
  assert.equal(await navButton('Это пример кода').count(), 0);
  assert.equal(await page.locator('.agent-page script').count(), 0);
  await navButton('Проверка результата').click();
  assert.equal(await page.evaluate(() => document.activeElement.dataset.agentPageSection), 'Проверка результата');
  await page.locator('[data-agent-tab="find"]').click();
  await page.locator('#agentFindQuery').fill('проверка'); await page.locator('#agentFindForm').evaluate(node => node.requestSubmit());
  await navButton('Утверждение 2').waitFor();
  assert.equal(await navButton('Проверка результата').count(), 0);
  await navButton('Утверждение 2').click();
  assert.equal(await page.evaluate(() => document.activeElement.dataset.claim), '1');
  await mode('radar');
  await page.waitForFunction(() => document.querySelector('#issueNavTitle').textContent === 'По выпуску');
  assert.equal(await page.locator('#issueNavTitle').innerText(), 'По выпуску');
  assert.equal(await navButton('Утверждение 2').count(), 0);
  assert.deepEqual(errors, []);
  console.log('Reading navigator browser smoke: PASS (iframe coordinates, columns, resize, archive, access, chat append/reset, history, dense rail, mobile composer, Wiki, results)');
} finally { await browser.close(); }
