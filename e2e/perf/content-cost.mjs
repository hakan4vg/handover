// What the extension's content scripts cost a web page's main thread.
//
// Loads the built extension into Chromium (new headless; extensions need the
// full browser, not headless-shell) and opens synthetic pages from a local
// server, each once with the extension and once in a browser without it. For
// every page it records, over a fixed window after the page settles:
//   - CDP Performance metrics: total task, script, layout and style time per
//     second, with vs without the extension (the honest total, including work
//     Blink does on the page's behalf such as MutationObserver bookkeeping);
//   - a CPU profile, attributing samples to the extension's scripts and to the
//     content-script functions on the stack (which mechanism the time is in).
//
// The profile uses an unminified build of the same sources so function names
// survive; the totals use whichever build --extension points at (default: a
// fresh production build).
//
//   npm i --no-save playwright-core
//   node e2e/perf/content-cost.mjs [--seconds 10] [--runs 3] [--only heavy-mutating,media]
//                                  [--chromium PATH] [--extension DIR]
//
// Writes e2e/results/content-cost-<stamp>.json and prints a summary table.
import { chromium } from 'playwright-core';
import { build } from 'vite';
import http from 'node:http';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const args = Object.fromEntries(process.argv.slice(2).reduce((pairs, arg, index, all) => {
  if (arg.startsWith('--')) pairs.push([arg.slice(2), all[index + 1]?.startsWith('--') || all[index + 1] === undefined ? 'true' : all[index + 1]]);
  return pairs;
}, []));
const SECONDS = Number(args.seconds ?? 10);
const RUNS = Number(args.runs ?? 3);
const SETTLE_MS = 3000;
const CHROMIUM = args.chromium ?? process.env.CHROMIUM_PATH ?? findChromium();

function findChromium() {
  const base = process.env.PLAYWRIGHT_BROWSERS_PATH ?? '/opt/pw-browsers';
  const dir = fs.existsSync(base) ? fs.readdirSync(base).find((name) => /^chromium-\d+$/.test(name)) : undefined;
  return dir ? path.join(base, dir, 'chrome-linux', 'chrome') : undefined;
}

// ---------------------------------------------------------------- extension

async function buildExtension(outDir, minify) {
  await build({
    configFile: path.join(root, 'extension/vite.config.ts'),
    logLevel: 'warn',
    build: { outDir, minify, emptyOutDir: true },
  });
  // The config's own copy step writes into extension/dist; place them here too.
  fs.copyFileSync(path.join(root, 'extension/manifest.json'), path.join(outDir, 'manifest.json'));
  fs.cpSync(path.join(root, 'extension/icons'), path.join(outDir, 'icons'), { recursive: true });
  return outDir;
}

// ---------------------------------------------------------------- pages

const NODES = 50_000;

/** A deep-ish, wide document of about `count` elements: rows of nested cells
 *  with text, the shape of a long feed or a data grid. */
function bulkScript(count) {
  return `
    const host = document.getElementById('bulk');
    const perRow = 10;
    const frag = document.createDocumentFragment();
    for (let i = 0; i < ${count} / perRow; i++) {
      const row = document.createElement('div');
      row.className = 'row';
      for (let j = 0; j < perRow - 1; j++) {
        const cell = document.createElement(j % 3 ? 'span' : 'p');
        cell.textContent = 'cell ' + i + '.' + j;
        row.appendChild(cell);
      }
      frag.appendChild(row);
    }
    host.appendChild(frag);`;
}

/** Steady childList churn: every 20 ms append one 10-element row to a small
 *  live feed and drop the oldest, about 50 mutation batches per second. The
 *  feed is layout-contained so the page's own rendering stays cheap and the
 *  extension's share is visible; the observer still watches the whole
 *  document either way. */
const LIVE = '<div id="live" style="contain:strict;height:200px;overflow:hidden">' + '<div class="row"><span>live</span></div>'.repeat(20) + '</div>';
const MUTATE = `
  const feed = document.getElementById('live');
  let n = 0;
  setInterval(() => {
    const row = document.createElement('div');
    row.className = 'row';
    for (let j = 0; j < 9; j++) {
      const cell = document.createElement('span');
      cell.textContent = 'live ' + n + '.' + j;
      row.appendChild(cell);
    }
    feed.prepend(row);
    feed.lastElementChild?.remove();
    n++;
  }, 20);`;

const VIDEOS = Array.from({ length: 4 }, (_, i) => `<video controls preload="none" width="640" height="360" src="/clip-${i}.mp4"></video>`).join('\n');

const SHADOW_PLAYER = `
  customElements.define('x-player', class extends HTMLElement {
    constructor() {
      super();
      this.attachShadow({ mode: 'open' }).innerHTML = '<div><video preload="none" width="640" height="360" src="/shadow-clip.mp4"></video></div>';
    }
  });`;

function page(title, body, script = '') {
  return `<!doctype html><html><head><meta charset="utf-8"><title>${title}</title>
<style>body{font:14px system-ui;margin:0 16px}.row{display:flex;gap:4px}p,span{margin:0}</style></head>
<body><h1>${title}</h1>${body}<script>${script}</script></body></html>`;
}

const ARTICLE = Array.from({ length: 40 }, (_, i) => `<p>Paragraph ${i}: a quiet static article with a few hundred elements and no script activity.</p>`).join('');

const PAGES = {
  quiet: { what: 'static article, ~100 elements', html: page('Quiet', ARTICLE) },
  heavy: { what: `${NODES / 1000}k elements, static`, html: page('Heavy', '<div id="bulk"></div>', bulkScript(NODES)) },
  'heavy-mutating': { what: `${NODES / 1000}k elements, a 10-element row added to and removed from a small feed every 20 ms`, html: page('Heavy mutating', LIVE + '<div id="bulk"></div>', bulkScript(NODES) + MUTATE) },
  media: { what: 'article + 4 paused <video>, pointer away', html: page('Media', VIDEOS + ARTICLE) },
  'media-hover': { what: 'article + 4 paused <video>, pointer over one (button shown)', html: page('Media hover', VIDEOS + ARTICLE), hover: 'video' },
  'heavy-shadow': { what: `${NODES / 1000}k elements + one open-shadow-root player (1 s shadow scan)`, html: page('Heavy shadow', '<x-player></x-player><div id="bulk"></div>', SHADOW_PLAYER + bulkScript(NODES)) },
  'heavy-late-shadow': { what: `${NODES / 1000}k elements + an open-shadow-root player added 1 s after load`, html: page('Heavy late shadow', '<div id="slot"></div><div id="bulk"></div>', SHADOW_PLAYER + bulkScript(NODES) + "setTimeout(() => document.getElementById('slot').append(document.createElement('x-player')), 1000);") },
  'heavy-mutating-shadow': { what: `heavy-mutating + one open-shadow-root player`, html: page('Heavy mutating shadow', '<x-player></x-player>' + LIVE + '<div id="bulk"></div>', SHADOW_PLAYER + bulkScript(NODES) + MUTATE) },
};

function serve() {
  const server = http.createServer((req, res) => {
    const name = req.url.slice(1).split('?')[0];
    if (PAGES[name]) {
      res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' });
      res.end(PAGES[name].html);
      return;
    }
    // Media sources are never fetched (preload=none, nothing plays); a 404 is fine.
    res.writeHead(404);
    res.end();
  });
  return new Promise((resolve) => server.listen(0, '127.0.0.1', () => resolve(server)));
}

// ---------------------------------------------------------------- browsers

async function launch(extensionDir) {
  const userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'dm-perf-'));
  const extensionArgs = extensionDir ? [`--disable-extensions-except=${extensionDir}`, `--load-extension=${extensionDir}`] : [];
  const context = await chromium.launchPersistentContext(userDataDir, {
    executablePath: CHROMIUM,
    headless: true,
    viewport: { width: 1280, height: 900 },
    args: ['--headless=new', ...extensionArgs],
  });
  if (extensionDir) {
    // Wait for the service worker so the first page's policy request is answered.
    if (!context.serviceWorkers().length) await context.waitForEvent('serviceworker', { timeout: 15000 });
  }
  return { context, close: async () => { await context.close(); fs.rmSync(userDataDir, { recursive: true, force: true }); } };
}

async function setPolicy(context, showMediaButtons) {
  // The same message the popup's toggle sends; the worker ignores its own.
  const id = new URL(context.serviceWorkers()[0].url()).host;
  const popup = await context.newPage();
  await popup.goto(`chrome-extension://${id}/popup.html`);
  const answer = await popup.evaluate((show) => chrome.runtime.sendMessage({ type: 'update-policy', patch: { showMediaButtons: show } }), showMediaButtons);
  await popup.close();
  if (answer?.policy?.showMediaButtons !== showMediaButtons) throw new Error(`policy not applied: ${JSON.stringify(answer)}`);
}

async function openPage(context, base, name) {
  const tab = await context.newPage();
  const cdp = await context.newCDPSession(tab);
  await cdp.send('Performance.enable', { timeDomain: 'threadTicks' });
  await tab.goto(`${base}/${name}`, { waitUntil: 'load' });
  if (PAGES[name].hover) {
    const box = await tab.locator(PAGES[name].hover).first().boundingBox();
    await tab.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  } else {
    await tab.mouse.move(5, 890);
  }
  await tab.waitForTimeout(SETTLE_MS);
  const state = await tab.evaluate(() => ({ hidden: document.hidden, elements: document.getElementsByTagName('*').length, button: !!document.getElementById('dm-media-download-button') }));
  return { tab, cdp, state };
}

async function metrics(cdp) {
  const { metrics } = await cdp.send('Performance.getMetrics');
  return Object.fromEntries(metrics.map(({ name, value }) => [name, value]));
}

/** Main-thread milliseconds per wall second, by category, over the window. */
async function measureTotals(context, base, name) {
  const { tab, cdp, state } = await openPage(context, base, name);
  const before = await metrics(cdp);
  await tab.waitForTimeout(SECONDS * 1000);
  const after = await metrics(cdp);
  await tab.close();
  const per = (key) => ((after[key] - before[key]) * 1000) / ((after.Timestamp - before.Timestamp) || SECONDS);
  return { state, task: per('TaskDuration'), script: per('ScriptDuration'), layout: per('LayoutDuration'), style: per('RecalcStyleDuration') };
}

// ---------------------------------------------------------------- profile

const KEY_FUNCTIONS = ['track', 'collectMedia', 'scanShadowMedia', 'watchShadowRoot', 'onMediaEvent', 'discoverAt', 'pick', 'positionButton', 'loop', 'requestMediaFilter', 'onPointerMove', 'refreshPolicy'];

async function profile(context, base, name, extensionId) {
  const { tab, cdp } = await openPage(context, base, name);
  await cdp.send('Profiler.enable');
  await cdp.send('Profiler.setSamplingInterval', { interval: 100 });
  await cdp.send('Profiler.start');
  await tab.waitForTimeout(SECONDS * 1000);
  const { profile: cpu } = await cdp.send('Profiler.stop');
  await tab.close();

  const byId = new Map(cpu.nodes.map((node) => [node.id, node]));
  const parent = new Map();
  cpu.nodes.forEach((node) => (node.children ?? []).forEach((child) => parent.set(child, node.id)));
  const prefix = `chrome-extension://${extensionId}/`;
  const wall = (cpu.endTime - cpu.startTime) / 1000; // ms
  const totals = { extensionSelf: 0, extensionInclusive: 0, byScript: {}, inclusive: {}, entry: {}, idle: 0, program: 0, gc: 0, page: 0 };
  const label = (frame) => frame.functionName || `(anonymous ${frame.url.slice(prefix.length)}:${frame.lineNumber + 1})`;

  cpu.samples.forEach((id, index) => {
    const ms = (cpu.timeDeltas[index + 1] ?? 0) / 1000;
    const node = byId.get(id);
    const frame = node.callFrame;
    if (frame.functionName === '(idle)') { totals.idle += ms; return; }
    if (frame.functionName === '(program)') { totals.program += ms; return; }
    if (frame.functionName === '(garbage collector)') totals.gc += ms;
    // Walk the stack: which extension functions are on it, and the outermost one.
    const seen = new Set();
    let outermost = null;
    let onExtension = false;
    for (let cur = id; cur !== undefined; cur = parent.get(cur)) {
      const f = byId.get(cur).callFrame;
      if (f.url.startsWith(prefix)) {
        onExtension = true;
        outermost = f;
        if (f.url.endsWith('content.js') && KEY_FUNCTIONS.includes(f.functionName)) seen.add(f.functionName);
      }
    }
    if (frame.url.startsWith(prefix)) {
      totals.extensionSelf += ms;
      const script = frame.url.slice(prefix.length);
      totals.byScript[script] = (totals.byScript[script] ?? 0) + ms;
    }
    if (onExtension) {
      totals.extensionInclusive += ms;
      seen.forEach((fn) => { totals.inclusive[fn] = (totals.inclusive[fn] ?? 0) + ms; });
      const entry = label(outermost);
      totals.entry[entry] = (totals.entry[entry] ?? 0) + ms;
    } else if (frame.functionName !== '(garbage collector)') {
      totals.page += ms;
    }
  });
  const perSecond = (value) => Math.round((value / wall) * 1000 * 100) / 100;
  const mapPer = (object) => Object.fromEntries(Object.entries(object).sort((a, b) => b[1] - a[1]).map(([key, value]) => [key, perSecond(value)]));
  return {
    wallSeconds: wall / 1000,
    extensionInclusive: perSecond(totals.extensionInclusive),
    extensionSelf: perSecond(totals.extensionSelf),
    pageScript: perSecond(totals.page),
    gc: perSecond(totals.gc),
    byScript: mapPer(totals.byScript),
    inclusive: mapPer(totals.inclusive),
    entry: mapPer(totals.entry),
  };
}

// ---------------------------------------------------------------- run

const median = (values) => {
  const sorted = [...values].sort((a, b) => a - b);
  return sorted.length % 2 ? sorted[(sorted.length - 1) / 2] : (sorted[sorted.length / 2 - 1] + sorted[sorted.length / 2]) / 2;
};

async function main() {
  if (!CHROMIUM) throw new Error('no Chromium found: pass --chromium PATH');
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'dm-perf-ext-'));
  const prodDir = args.extension ? path.resolve(args.extension) : await buildExtension(path.join(temp, 'prod'), true);
  const debugDir = await buildExtension(path.join(temp, 'debug'), false);
  const server = await serve();
  const base = `http://127.0.0.1:${server.address().port}`;
  const names = args.only ? args.only.split(',') : Object.keys(PAGES);

  const variants = [
    { key: 'none', dir: null },
    { key: 'extension', dir: prodDir },
    { key: 'extension-buttons-off', dir: prodDir, policy: false },
  ];
  const results = { chromium: CHROMIUM, seconds: SECONDS, runs: RUNS, pages: {} };
  for (const name of names) results.pages[name] = { what: PAGES[name].what, totals: {}, profile: {} };

  for (const variant of variants) {
    const browser = await launch(variant.dir);
    if (variant.policy !== undefined) await setPolicy(browser.context, variant.policy);
    for (const name of names) {
      const runs = [];
      for (let run = 0; run < RUNS; run++) runs.push(await measureTotals(browser.context, base, name));
      const summary = { state: runs[0].state };
      for (const key of ['task', 'script', 'layout', 'style']) summary[key] = Math.round(median(runs.map((r) => r[key])) * 100) / 100;
      summary.runs = runs.map(({ task, script }) => ({ task: Math.round(task * 100) / 100, script: Math.round(script * 100) / 100 }));
      results.pages[name].totals[variant.key] = summary;
      process.stderr.write(`${variant.key.padEnd(22)} ${name.padEnd(22)} task ${summary.task.toFixed(2)} ms/s  script ${summary.script.toFixed(2)} ms/s  ${JSON.stringify(summary.state)}\n`);
    }
    await browser.close();
  }

  // Attribution with the unminified build.
  for (const policy of [true, false]) {
    const browser = await launch(debugDir);
    if (!policy) await setPolicy(browser.context, false);
    const extensionId = new URL(browser.context.serviceWorkers()[0].url()).host;
    for (const name of names) {
      results.pages[name].profile[policy ? 'extension' : 'extension-buttons-off'] = await profile(browser.context, base, name, extensionId);
      const p = results.pages[name].profile[policy ? 'extension' : 'extension-buttons-off'];
      process.stderr.write(`profile ${policy ? 'on ' : 'off'} ${name.padEnd(22)} extension ${p.extensionInclusive.toFixed(2)} ms/s  ${JSON.stringify(p.inclusive)}\n`);
    }
    await browser.close();
  }
  server.close();
  fs.rmSync(temp, { recursive: true, force: true });

  const outDir = path.join(root, 'e2e/results');
  fs.mkdirSync(outDir, { recursive: true });
  const stamp = new Date().toISOString().replace(/[:.]/g, '-');
  const out = path.join(outDir, `content-cost-${stamp}.json`);
  fs.writeFileSync(out, JSON.stringify(results, null, 2));

  console.log('\nmain-thread ms per wall second. task = all main-thread work (median of runs);\next JS = profiler samples with a content-script frame on the stack, natives it calls included\n');
  console.log('page'.padEnd(24) + 'task none'.padStart(11) + 'task ext'.padStart(11) + 'task off'.padStart(11) + 'ext JS on'.padStart(11) + 'ext JS off'.padStart(12) + '  top mechanism');
  for (const name of names) {
    const r = results.pages[name];
    const top = Object.entries(r.profile.extension.inclusive).filter(([fn]) => fn !== 'track')[0];
    console.log(name.padEnd(24)
      + r.totals.none.task.toFixed(2).padStart(11)
      + r.totals.extension.task.toFixed(2).padStart(11)
      + r.totals['extension-buttons-off'].task.toFixed(2).padStart(11)
      + r.profile.extension.extensionInclusive.toFixed(2).padStart(11)
      + r.profile['extension-buttons-off'].extensionInclusive.toFixed(2).padStart(12)
      + (top ? `  ${top[0]} ${top[1].toFixed(2)}` : ''));
  }
  console.log(`\n${path.relative(root, out)}`);
}

main().catch((error) => { console.error(error); process.exit(1); });
