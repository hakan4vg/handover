// The media button over players whose <video> lives in an open shadow root.
//
// Loads the built extension into Chromium (new headless) with no resident
// running, so the extension uses its default policy (media buttons on), and
// opens one local page per way a page can create such a player: before the
// content script runs, after it, detached and inserted later, nested, filled
// in later, removed and re-inserted, and in a closed root (no button
// expected). A few plain-document cases cover players added late, inserted
// after loading, under an overlay, or given a source late. For each it points at the video and checks whether the button
// shows. Runs anywhere Chromium does; no Windows or resident needed.
//
//   npm i --no-save playwright-core
//   node e2e/shadow-media.mjs [--chromium PATH] [--extension DIR]
//
// Writes e2e/results/shadow-media-<stamp>.json; exits 1 if any case fails.
import { chromium } from 'playwright-core';
import { build } from 'vite';
import http from 'node:http';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const args = Object.fromEntries(process.argv.slice(2).reduce((pairs, arg, index, all) => {
  if (arg.startsWith('--')) pairs.push([arg.slice(2), all[index + 1]?.startsWith('--') || all[index + 1] === undefined ? 'true' : all[index + 1]]);
  return pairs;
}, []));
const CHROMIUM = args.chromium ?? process.env.CHROMIUM_PATH ?? findChromium();
const BUTTON = 'dm-media-download-button';
/** Every page's late steps are done by then. */
const LATE_MS = 2500;

function findChromium() {
  const base = process.env.PLAYWRIGHT_BROWSERS_PATH ?? '/opt/pw-browsers';
  const dir = fs.existsSync(base) ? fs.readdirSync(base).find((name) => /^chromium-\d+$/.test(name)) : undefined;
  return dir ? path.join(base, dir, 'chrome-linux', 'chrome') : undefined;
}

const VIDEO = '<video preload="none" width="640" height="360" src="/clip.mp4"></video>';
const PLAYER = (mode = 'open') => `
  customElements.define('x-player', class extends HTMLElement {
    constructor() {
      super();
      this.attachShadow({ mode: '${mode}' }).innerHTML = '<div>${VIDEO}</div>';
    }
  });`;
const later = (ms, code) => `setTimeout(() => { ${code} }, ${ms});`;

/** name → [what, body, script, button expected] */
const CASES = {
  declarative: ['declarative shadow DOM in the HTML', `<div><template shadowrootmode="open">${VIDEO}</template></div>`, '', true],
  early: ['custom element upgraded during parsing', '<x-player></x-player>', PLAYER(), true],
  late: ['created and inserted 1 s after load, inside a wrapper', '<div id="slot"></div>', PLAYER() + later(1000, "const wrapper = document.createElement('div'); wrapper.innerHTML = '<figure><x-player></x-player></figure>'; slot.append(wrapper)"), true],
  detached: ['created at load inside a wrapper, inserted 1.5 s later', '<div id="slot"></div>', PLAYER() + "const held = document.createElement('div'); held.innerHTML = '<figure><x-player></x-player></figure>';" + later(1500, 'slot.append(held)'), true],
  nested: ['open root inside an open root, inserted 1 s after load', '<div id="slot"></div>', `
    customElements.define('x-inner', class extends HTMLElement {
      constructor() { super(); this.attachShadow({ mode: 'open' }).innerHTML = '${VIDEO}'; }
    });
    customElements.define('x-outer', class extends HTMLElement {
      constructor() { super(); this.attachShadow({ mode: 'open' }).innerHTML = '<section><x-inner></x-inner></section>'; }
    });` + later(1000, "slot.append(document.createElement('x-outer'))"), true],
  filled: ['empty open root at load, video added 1.5 s later', '<div id="host"></div>', "const shadow = host.attachShadow({ mode: 'open' });" + later(1500, `shadow.innerHTML = '${VIDEO}'`), true],
  reinserted: ['removed 0.5 s after load, re-inserted 1.5 s later', '<div id="slot"><x-player></x-player></div>', PLAYER() + "const player = slot.firstElementChild;" + later(500, 'player.remove()') + later(2000, 'slot.append(player)'), true],
  closed: ['closed shadow root (out of reach, no button)', '<x-player></x-player>', PLAYER('closed'), false],
  // Plain <video> in the document, added in the ways that only events (not a
  // DOM observer) have to catch.
  'light-late': ['plain video inserted 1 s after load', '<div id="slot"></div>', later(1000, `slot.innerHTML = '${VIDEO}'`), true],
  'light-detached': ['plain video given its source at load, inserted 1.5 s later', '<div id="slot"></div>', `const held = document.createElement('div'); held.innerHTML = '${VIDEO}';` + later(1500, 'slot.append(held)'), true],
  'light-overlaid': ['as light-detached, under a transparent overlay that takes the pointer', '<div id="slot" style="position:relative"></div>', `const held = document.createElement('div'); held.innerHTML = '${VIDEO}<div style="position:absolute;inset:0"></div>';` + later(1500, 'slot.append(held)'), true],
  'light-source-later': ['plain video with no source until 1.5 s after load', '<video id="late" preload="none" width="640" height="360"></video>', later(1500, "late.src = '/clip.mp4'"), true],
};

function page(title, body, script) {
  return `<!doctype html><html><head><meta charset="utf-8"><title>${title}</title></head>
<body style="margin:16px"><h1>${title}</h1>${body}<script>${script}</script></body></html>`;
}

function serve() {
  const server = http.createServer((req, res) => {
    const name = req.url.slice(1).split('?')[0];
    if (!CASES[name]) {
      res.writeHead(404);
      res.end();
      return;
    }
    res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' });
    res.end(page(name, CASES[name][1], CASES[name][2]));
  });
  return new Promise((resolve) => server.listen(0, '127.0.0.1', () => resolve(server)));
}

async function buildExtension(outDir) {
  await build({ configFile: path.join(root, 'extension/vite.config.ts'), logLevel: 'warn', build: { outDir, emptyOutDir: true } });
  fs.copyFileSync(path.join(root, 'extension/manifest.json'), path.join(outDir, 'manifest.json'));
  fs.cpSync(path.join(root, 'extension/icons'), path.join(outDir, 'icons'), { recursive: true });
  return outDir;
}

/** Points at the video wherever it is (Playwright's CSS pierces open roots;
 *  a closed root's video is found through its host's box instead). */
async function runCase(context, base, name) {
  const tab = await context.newPage();
  await tab.goto(`${base}/${name}`, { waitUntil: 'load' });
  await tab.mouse.move(5, 5);
  await tab.waitForTimeout(LATE_MS);
  const target = (await tab.locator('video').count()) ? tab.locator('video').first() : tab.locator('x-player').first();
  const box = await target.boundingBox();
  let shown = false;
  if (box) {
    await tab.mouse.move(box.x + box.width / 2, box.y + box.height / 2, { steps: 4 });
    shown = await tab.waitForSelector(`#${BUTTON}`, { timeout: 3000 }).then(() => true, () => false);
  }
  await tab.close();
  return { case: name, what: CASES[name][0], videoFound: !!box, buttonShown: shown, expected: CASES[name][3], pass: shown === CASES[name][3] };
}

async function main() {
  if (!CHROMIUM) throw new Error('no Chromium found: pass --chromium PATH');
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'dm-shadow-'));
  const extensionDir = args.extension ? path.resolve(args.extension) : await buildExtension(path.join(temp, 'ext'));
  const server = await serve();
  const base = `http://127.0.0.1:${server.address().port}`;
  const context = await chromium.launchPersistentContext(path.join(temp, 'profile'), {
    executablePath: CHROMIUM,
    headless: true,
    viewport: { width: 1280, height: 900 },
    args: ['--headless=new', `--disable-extensions-except=${extensionDir}`, `--load-extension=${extensionDir}`],
  });
  if (!context.serviceWorkers().length) await context.waitForEvent('serviceworker', { timeout: 15000 });
  const results = [];
  for (const name of Object.keys(CASES)) {
    const result = await runCase(context, base, name);
    results.push(result);
    process.stderr.write(`${result.pass ? 'pass' : 'FAIL'}  ${name.padEnd(18)} button ${result.buttonShown ? 'shown' : 'absent'} (${result.what})\n`);
  }
  await context.close();
  server.close();
  fs.rmSync(temp, { recursive: true, force: true });

  const outDir = path.join(root, 'e2e/results');
  fs.mkdirSync(outDir, { recursive: true });
  const out = path.join(outDir, `shadow-media-${new Date().toISOString().replace(/[:.]/g, '-')}.json`);
  fs.writeFileSync(out, JSON.stringify({ chromium: CHROMIUM, results }, null, 2));
  console.log(path.relative(root, out));
  process.exit(results.every((result) => result.pass) ? 0 : 1);
}

main().catch((error) => { console.error(error); process.exit(1); });
