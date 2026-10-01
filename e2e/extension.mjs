// Extension service-worker scenarios against the real resident and fixtures.
//
// Bundles the real extension/src/background.ts and runs it in a VM whose
// `fetch` is Node's real fetch, so every handoff reaches the running resident's
// bridge and every resident fetch reaches the fixture server. Only Chromium's
// own events (downloads, webRequest, runtime messages) are simulated.
//
// Invoked by e2e/native.py while the resident is running:
//   node e2e/extension.mjs <fixture-base-url>
// Prints one JSON object: { scenarios: [...], handedOver: [...] }.
import { build } from 'esbuild';
import vm from 'node:vm';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const base = process.argv[2];
// The pairing native.py made with the running resident.
const PAIRING = JSON.parse(process.env.DM_PAIRING ?? 'null');
const bundle = await build({ entryPoints: [process.env.DM_EXTENSION_ENTRY ?? path.join(root, 'extension/src/background.ts')], bundle: true, format: 'iife', platform: 'browser', write: false });
const code = bundle.outputFiles[0].text;

function world({ loseCaptureAnswers = false, excludedSites = null, activeTabUrl = undefined, stored = {}, paired = true, answer = null, browserCookies = [] } = {}) {
  if (paired && PAIRING && !stored['dm-pairing']) stored['dm-pairing'] = PAIRING;
  const listeners = {};
  const calls = [];
  const cookieQueries = [];
  const outbound = [];
  const on = (name) => ({ addListener: (fn) => { (listeners[name] ??= []).push(fn); } });
  const emit = (name, ...args) => (listeners[name] ?? []).map((fn) => fn(...args));
  const fetchThrough = async (url, options = {}) => {
    if (answer && url.includes('/v1/') && !url.includes('/v1/pair')) return answer(url, options);
    const message = options.body && url.endsWith('/v1/message') ? await openRequest(options.body) : options.body ? JSON.parse(options.body) : undefined;
    if (message) outbound.push(message);
    if (excludedSites && message?.type === 'get-policy') {
      // The resident's settings with an exclusion list, without touching the
      // running resident that other scenarios share.
      return sealedAnswer(options.body, { ok: true, policy: { interceptDownloads: true, showMediaButtons: true, excludedSites, updatedAt: 0 } });
    }
    const response = await fetch(url, options);
    if (loseCaptureAnswers && message?.type === 'capture-acquisition') {
      // The resident received and acted on the request; its answer is lost.
      await response.text();
      throw new TypeError('connection reset before the answer arrived');
    }
    return response;
  };
  const chrome = {
    runtime: { id: 'e2e-extension', onMessage: on('message') },
    webRequest: { onBeforeRequest: on('beforeRequest'), onResponseStarted: on('responseStarted'), onHeadersReceived: on('headers'), onCompleted: on('completed'), onErrorOccurred: on('error') },
    downloads: {
      onDeterminingFilename: on('determining'),
      pause: async (id) => { calls.push(['pause', id]); },
      resume: async (id) => { calls.push(['resume', id]); },
      cancel: async (id) => { calls.push(['cancel', id]); },
      download: async (options) => { calls.push(['browser-download', options.url]); return 99; },
    },
    storage: { local: { get: async (key) => (key in stored ? { [key]: stored[key] } : {}), set: async (items) => { Object.assign(stored, items); } }, session: { get: async () => ({}), set: async () => {} }, onChanged: on('storage') },
    // Chromium's cookie store: answers by host, as chrome.cookies.getAll({ url }) does.
    cookies: { getAll: async (details) => { cookieQueries.push(details); const host = new URL(details.url).hostname; return details.partitionKey ? [] : browserCookies.filter((c) => c.domain.replace(/^\./, '') === host); } },
    tabs: { sendMessage: async () => undefined, query: async () => (activeTabUrl ? [{ id: 1, url: activeTabUrl }] : []) },
  };
  const context = { URL, URLSearchParams, AbortController, setTimeout, clearTimeout, console, crypto: globalThis.crypto, fetch: fetchThrough, chrome, TextEncoder, TextDecoder, btoa, atob, Response };
  vm.runInNewContext(code, context);
  const message = (payload, sender) => new Promise((resolve) => { emit('message', payload, sender, resolve); });
  const determine = (item) => new Promise((resolve) => { emit('determining', item, resolve); });
  return { emit, calls, outbound, message, determine, cookieQueries };
}

// The harness reads what the worker sends (and can answer for the resident)
// with the same pairing key.
const subtle = globalThis.crypto.subtle;
const keyFor = async () => subtle.importKey('raw', Buffer.from(PAIRING.key, 'base64'), 'AES-GCM', false, ['encrypt', 'decrypt']);
async function openRequest(body) {
  const { iv, data } = JSON.parse(body);
  const plain = await subtle.decrypt({ name: 'AES-GCM', iv: Buffer.from(iv, 'base64'), additionalData: new TextEncoder().encode('dm-bridge-1 request') }, await keyFor(), Buffer.from(data, 'base64'));
  return JSON.parse(new TextDecoder().decode(plain)).message;
}
async function sealedAnswer(requestBody, value) {
  const nonce = JSON.parse(requestBody).iv;
  const iv = globalThis.crypto.getRandomValues(new Uint8Array(12));
  const data = await subtle.encrypt({ name: 'AES-GCM', iv, additionalData: new TextEncoder().encode('dm-bridge-1 response ' + nonce) }, await keyFor(), new TextEncoder().encode(JSON.stringify(value)));
  return new Response(JSON.stringify({ iv: Buffer.from(iv).toString('base64'), data: Buffer.from(data).toString('base64') }), { headers: { 'Content-Type': 'application/json' } });
}

/** Ask the running resident directly, over the paired channel. */
async function askResident(message) {
  const iv = globalThis.crypto.getRandomValues(new Uint8Array(12));
  const plain = new TextEncoder().encode(JSON.stringify({ t: Date.now(), message }));
  const data = await subtle.encrypt({ name: 'AES-GCM', iv, additionalData: new TextEncoder().encode('dm-bridge-1 request') }, await keyFor(), plain);
  const nonce = Buffer.from(iv).toString('base64');
  const response = await fetch('http://127.0.0.1:38217/v1/message', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ k: PAIRING.keyId, iv: nonce, data: Buffer.from(data).toString('base64') }) });
  const envelope = await response.json();
  const opened = await subtle.decrypt({ name: 'AES-GCM', iv: Buffer.from(envelope.iv, 'base64'), additionalData: new TextEncoder().encode('dm-bridge-1 response ' + nonce) }, await keyFor(), Buffer.from(envelope.data, 'base64'));
  return JSON.parse(new TextDecoder().decode(opened));
}

const scenarios = [];
const handedOver = [];
const check = (scenario, guards, pass, evidence) => scenarios.push({ scenario, guards, pass: !!pass, evidence });
const settle = (ms = 50) => new Promise((resolve) => setTimeout(resolve, ms));
const captures = (w) => w.outbound.filter((m) => m.type === 'capture-acquisition' || m.type === 'media-capture');

// --- F02: the browser copy survives a source the resident cannot fetch -----
{
  const w = world(); await settle();
  await w.determine({ id: 1, url: `${base}/cookie.bin`, finalUrl: `${base}/cookie.bin`, filename: 'x-handback.bin', referrer: `${base}/page` });
  const order = w.calls.map(([call]) => call);
  check('downloads-api/handback', 'a session-gated download is paused, handed back, and resumed, never cancelled', order.join(',') === 'pause,resume', { calls: w.calls, sent: captures(w).map((m) => m.payload.requireViable) });
  handedOver.push(...captures(w).map((m) => ({ name: m.payload.name, scenario: 'downloads-api/handback', source: m.payload.source, captureId: m.payload.captureId })));
}
{
  const w = world(); await settle();
  await w.determine({ id: 2, url: `${base}/file/range.bin`, finalUrl: `${base}/file/range.bin`, filename: 'x-takeover.bin', referrer: `${base}/page` });
  const order = w.calls.map(([call]) => call);
  check('downloads-api/takeover', 'a fetchable download is paused, proven by the resident, then cancelled in the browser', order.join(',') === 'pause,cancel', { calls: w.calls });
  handedOver.push(...captures(w).map((m) => ({ name: m.payload.name, scenario: 'downloads-api/takeover', source: m.payload.source, captureId: m.payload.captureId, expectJob: true })));
}
{
  const w = world(); await settle();
  const reply = await w.message({ type: 'ordinary-capture', payload: { source: `${base}/login-page.bin`, name: 'x-anchor-handback.pdf', pageUrl: `${base}/page` } }, { tab: { id: 5 }, frameId: 0 });
  check('anchor/handback', 'an intercepted link the resident cannot fetch is downloaded by the browser instead', reply.ok === true && w.calls.some(([call, url]) => call === 'browser-download' && url.endsWith('/login-page.bin')), { reply, calls: w.calls });
  handedOver.push(...captures(w).map((m) => ({ name: m.payload.name, scenario: 'anchor/handback', source: m.payload.source, captureId: m.payload.captureId })));
}

// --- 1.10: an empty referrer does not bypass an excluded site ---------------
{
  const w = world({ excludedSites: ['127.0.0.1'] }); await settle();
  await w.determine({ id: 11, url: `${base}/file/range.bin?noref`, finalUrl: `${base}/file/range.bin?noref`, filename: 'x-excluded-source.bin', referrer: '' });
  check('exclusion/no-referrer-source', 'a download with no referrer from an excluded site is left to the browser', w.calls.length === 0 && captures(w).length === 0, { calls: w.calls, sent: captures(w).length });
}
{
  const w = world({ excludedSites: ['example.com'], activeTabUrl: 'https://www.example.com/reports' }); await settle();
  await w.determine({ id: 12, url: `${base}/file/range.bin?cdn`, finalUrl: `${base}/file/range.bin?cdn`, filename: 'x-excluded-tab.bin', referrer: '' });
  check('exclusion/no-referrer-tab', 'a no-referrer download while an excluded site is the focused tab is left to the browser', w.calls.length === 0 && captures(w).length === 0, { calls: w.calls, sent: captures(w).length });
}
{
  const w = world({ excludedSites: ['example.com'], activeTabUrl: 'https://other.test/page' }); await settle();
  await w.determine({ id: 13, url: `${base}/file/range.bin?allowed`, finalUrl: `${base}/file/range.bin?allowed`, filename: 'x-not-excluded.bin', referrer: '' });
  check('exclusion/no-referrer-allowed', 'a no-referrer download unrelated to any excluded site is still taken over (control)', w.calls.map(([c]) => c).join(',') === 'pause,cancel', { calls: w.calls });
  handedOver.push(...captures(w).map((m) => ({ name: m.payload.name, scenario: 'exclusion/no-referrer-allowed', source: m.payload.source, captureId: m.payload.captureId, expectJob: true })));
}

// --- F03: a POST body is replayed only when its owner is unambiguous -------
{
  const w = world(); await settle();
  const url = `${base}/post-ok.bin`;
  w.emit('beforeRequest', { tabId: 1, frameId: 0, method: 'POST', type: 'main_frame', url, requestId: 'a', timeStamp: Date.now(), requestBody: { formData: { account: ['A'], export: ['first'] } } });
  w.emit('beforeRequest', { tabId: 2, frameId: 0, method: 'POST', type: 'main_frame', url, requestId: 'b', timeStamp: Date.now(), requestBody: { formData: { account: ['B'], export: ['second'] } } });
  await w.determine({ id: 3, url, finalUrl: url, filename: 'export.csv', referrer: `${base}/tab-b` });
  check('post/ambiguous', 'two different submissions to one URL leave the download with the browser, untouched', w.calls.length === 0 && captures(w).length === 0, { calls: w.calls, sent: captures(w).length });
}
{
  const w = world(); await settle();
  const url = `${base}/post-ok.bin`;
  w.emit('beforeRequest', { tabId: 1, frameId: 0, method: 'POST', type: 'main_frame', url, requestId: 'c', timeStamp: Date.now(), requestBody: { formData: { account: ['A'], export: ['only'] } } });
  await w.determine({ id: 4, url, finalUrl: url, filename: 'x-post-single.csv', referrer: `${base}/tab-a` });
  const sent = captures(w)[0]?.payload;
  check('post/single', 'a single observed submission is replayed as that POST and the takeover completes', sent?.postBody === 'account=A&export=only' && w.calls.map(([c]) => c).join(',') === 'pause,cancel', { calls: w.calls, postBody: sent?.postBody });
  handedOver.push(...captures(w).map((m) => ({ name: m.payload.name, scenario: 'post/single', source: m.payload.source, captureId: m.payload.captureId, expectJob: true })));
}

// --- F05: a lost answer never leaves two owners ------------------------------
{
  const w = world({ loseCaptureAnswers: true }); await settle();
  await w.determine({ id: 6, url: `${base}/file/range.bin?lost`, finalUrl: `${base}/file/range.bin?lost`, filename: 'x-lost.bin', referrer: `${base}/page` });
  await settle(300);
  const cancel = w.outbound.find((m) => m.type === 'cancel-acquisition');
  const created = captures(w)[0]?.payload;
  check('lost-answer/downloads-api', 'when the answer is lost the browser copy resumes and the resident job is cancelled by capture id', w.calls.map(([c]) => c).join(',') === 'pause,resume' && !!created?.captureId && cancel?.payload?.captureId === created.captureId, { calls: w.calls, cancel });
  handedOver.push({ scenario: 'lost-answer/downloads-api', name: created?.name, source: created?.source, captureId: created?.captureId });
}

// --- F23: an old request answering late is not the new source ---------------
async function lateResponse(startedBeforeSwitchMs) {
  const w = world(); await settle();
  const sender = { tab: { id: 9 }, frameId: 0, documentId: 'doc-1' };
  const blob = 'blob:http://127.0.0.1/new-video';
  await w.message({ type: 'media-player-state', payload: { playerKey: 'player-1', currentSrc: blob, mediaIdentity: 'm-1', playing: true, visible: true, active: true, hovered: true } }, sender);
  const switchedAt = Date.now();
  const manifest = `${base}/hls/vod.m3u8`;
  w.emit('beforeRequest', { tabId: 9, frameId: 0, documentId: 'doc-1', method: 'GET', type: 'xmlhttprequest', url: manifest, requestId: 'm', timeStamp: switchedAt - startedBeforeSwitchMs });
  await settle(20);
  w.emit('headers', { tabId: 9, frameId: 0, documentId: 'doc-1', type: 'xmlhttprequest', url: manifest, requestId: 'm', statusCode: 200, responseHeaders: [{ name: 'content-type', value: 'application/vnd.apple.mpegurl' }] });
  w.emit('responseStarted', { tabId: 9, frameId: 0, documentId: 'doc-1', type: 'xmlhttprequest', url: manifest, requestId: 'm' });
  const reply = await w.message({ type: 'media-capture', payload: { source: '', currentSrc: blob, mediaIdentity: 'm-1', pageUrl: `${base}/page`, playerKind: 'video', playerKey: 'player-1', name: 'video.mp4' } }, sender);
  return { w, reply, sent: w.outbound.filter((m) => m.type === 'media-capture') };
}
{
  const { reply, sent } = await lateResponse(60_000);
  check('epoch/late-old-response', 'a manifest requested 60 s before the source switch is not handed over as the new video', reply.ok !== true && sent.length === 0, { reply, sent: sent.map((m) => m.payload.source) });
}
{
  const { w, reply, sent } = await lateResponse(-500);
  check('epoch/fresh-request', 'a manifest requested after the source switch is still captured (control)', reply.ok === true && sent[0]?.payload?.source?.endsWith('/hls/vod.m3u8'), { reply, sent: sent.map((m) => m.payload.source) });
  handedOver.push(...sent.map((m) => ({ scenario: 'epoch/fresh-request', source: m.payload.source, captureId: m.payload.captureId, jobId: reply.id })));
  void w;
}

// --- A1.4: the browser policy side changed last wins ---------------------------
{
  // Interception was turned off in the popup while the resident was stopped.
  const w = world({ stored: { 'dm-policy': { interceptDownloads: false, showMediaButtons: true, excludedSites: [], updatedAt: Date.now() } } }); await settle();
  await w.determine({ id: 21, url: `${base}/file/range.bin?offline-off`, finalUrl: `${base}/file/range.bin?offline-off`, filename: 'x-offline-off.bin', referrer: `${base}/page` });
  const resident = await askResident({ type: 'get-policy' });
  check('policy/browser-change-while-stopped', 'interception turned off in the browser while the resident was stopped stays off, and the resident takes the change', w.calls.length === 0 && captures(w).length === 0 && resident.policy?.interceptDownloads === false, { calls: w.calls, residentPolicy: resident.policy });
  await w.message({ type: 'update-policy', patch: { interceptDownloads: true } }, {});
  handedOver.push(...captures(w).map((m) => ({ name: m.payload.name, scenario: 'policy/browser-change-while-stopped', source: m.payload.source, captureId: m.payload.captureId })));
}
{
  // A change in the manager after the browser's last change wins over it.
  const w = world({ stored: { 'dm-policy': { interceptDownloads: false, showMediaButtons: true, excludedSites: [], updatedAt: 1 } } }); await settle();
  await w.determine({ id: 22, url: `${base}/file/range.bin?manager-newer`, finalUrl: `${base}/file/range.bin?manager-newer`, filename: 'x-manager-newer.bin', referrer: `${base}/page` });
  check('policy/resident-change-newer', "an older browser-side setting gives way to the resident's newer one (control)", w.calls.map(([c]) => c).join(',') === 'pause,cancel', { calls: w.calls });
  handedOver.push(...captures(w).map((m) => ({ name: m.payload.name, scenario: 'policy/resident-change-newer', source: m.payload.source, captureId: m.payload.captureId, expectJob: true })));
}

// --- cookies: only the download's own, never the jar --------------------------
{
  const browserCookies = [
    { name: 'session', value: 'v1', domain: '127.0.0.1', hostOnly: true, path: '/', secure: false, httpOnly: true, sameSite: 'lax' },
    { name: 'tracker', value: 'v2', domain: '.unrelated.example', hostOnly: false, path: '/', secure: true, httpOnly: false, sameSite: 'no_restriction' },
  ];
  const w = world({ browserCookies }); await settle();
  await w.determine({ id: 41, url: `${base}/file/range.bin?cookies`, finalUrl: `${base}/file/range.bin?cookies`, filename: 'x-cookies.bin', referrer: `${base}/page` });
  const sent = captures(w)[0]?.payload?.cookies ?? [];
  const asked = w.cookieQueries.map((q) => q.url);
  check('cookies/only-the-downloads-own', "the capture carries the cookies for the download's own URL and nothing else from the browser's jar", sent.length === 1 && sent[0].name === 'session' && asked.every((url) => url.includes('/file/range.bin?cookies')), { sent: sent.map((c) => c.name), asked });
  handedOver.push(...captures(w).map((m) => ({ name: m.payload.name, scenario: 'cookies/only-the-downloads-own', source: m.payload.source, captureId: m.payload.captureId, expectJob: true })));
}

// --- pairing: nothing is handed to an app that is not the paired one ---------
{
  // Something else answers on the port: it says "ok", unsealed.
  const w = world({ answer: async () => new Response(JSON.stringify({ ok: true, id: 'impostor-job' }), { headers: { 'Content-Type': 'application/json' } }) }); await settle();
  await w.determine({ id: 31, url: `${base}/file/range.bin?impostor`, finalUrl: `${base}/file/range.bin?impostor`, filename: 'x-impostor.bin', referrer: `${base}/page` });
  check('pairing/impostor-answer', "an answer that is not sealed by the paired app counts as no answer: the browser's copy resumes", w.calls.map(([c]) => c).join(',') === 'pause,resume', { calls: w.calls });
}
{
  const w = world({ paired: false }); await settle();
  await w.determine({ id: 32, url: `${base}/file/range.bin?unpaired`, finalUrl: `${base}/file/range.bin?unpaired`, filename: 'x-unpaired.bin', referrer: `${base}/page` });
  await settle(300);
  check('pairing/unpaired', 'an unpaired extension leaves the download untouched and sends nothing but a pairing request', w.calls.length === 0 && w.outbound.every((m) => m.request), { calls: w.calls, sent: w.outbound });
}

// --- the in-page script must not claim names in the page's global scope -----
{
  // Run the built page script in a fresh realm (it may stop early on missing
  // browser APIs; its top-level declarations are made before it runs), then
  // compile a page script declaring each short name, as minified sites do.
  const { readFileSync } = await import('node:fs');
  const pageScript = readFileSync(process.env.DM_PAGE_SCRIPT ?? path.join(root, 'extension/dist/page-media.js'), 'utf8');
  const realm = vm.createContext({});
  try { vm.runInContext(pageScript, realm); } catch { /* browser APIs are absent here */ }
  const letters = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_$';
  const names = [...letters, ...[...letters].flatMap((first) => [...letters, ...'0123456789'].map((second) => first + second))];
  const clashes = names.filter((name) => { try { vm.runInContext(`let ${name} = 0;`, realm); return false; } catch (error) { return /already been declared/.test(String(error)); } });
  check('page-script/no-global-names', "a site's own scripts can declare any short top-level name next to the in-page script", clashes.length === 0, { clashes: clashes.slice(0, 20), count: clashes.length });
}

console.log(JSON.stringify({ scenarios, handedOver }));
