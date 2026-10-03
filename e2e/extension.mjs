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

function world({ loseCaptureAnswers = false, excludedSites = null, activeTabUrl = undefined, stored = {}, session = {}, paired = true, answer = null, pairApp = null, browserCookies = [] } = {}) {
  if (paired && PAIRING && !stored['dm-pairing']) stored['dm-pairing'] = PAIRING;
  const listeners = {};
  const calls = [];
  const cookieQueries = [];
  const outbound = [];
  const on = (name) => ({ addListener: (fn) => { (listeners[name] ??= []).push(fn); } });
  const emit = (name, ...args) => (listeners[name] ?? []).map((fn) => fn(...args));
  const fetchThrough = async (url, options = {}) => {
    // `answer` stands in for the app; returning nothing lets the real one answer.
    const stood = answer && url.includes('/v1/') && !url.includes('/v1/pair') ? await answer(url, options) : undefined;
    if (stood) return stood;
    if (pairApp && url.includes('/v1/pair')) return pairApp(url, options);
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
    storage: { local: { get: async (key) => (key in stored ? { [key]: stored[key] } : {}), set: async (items) => { Object.assign(stored, items); }, remove: async (key) => { delete stored[key]; } }, session: { get: async (key) => (key in session ? { [key]: session[key] } : {}), set: async (items) => { Object.assign(session, items); }, remove: async (key) => { delete session[key]; } }, onChanged: on('storage') },
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

{
  // Another program holds the port for a while (a second copy of the app, a
  // test build) and answers "not paired". The answer is unsealed: it must not
  // cost the extension its key, which works again once the real app is back.
  let impostor = true;
  const stored = {};
  const w = world({ stored, answer: async () => (impostor ? new Response(JSON.stringify({ ok: false, paired: false }), { status: 401, headers: { 'Content-Type': 'application/json' } }) : undefined) }); await settle();
  await w.determine({ id: 33, url: `${base}/file/range.bin?other-app`, finalUrl: `${base}/file/range.bin?other-app`, filename: 'x-other-app.bin', referrer: `${base}/page` });
  const kept = stored['dm-pairing']?.keyId === PAIRING.keyId;
  impostor = false;
  await w.determine({ id: 34, url: `${base}/file/range.bin?app-back`, finalUrl: `${base}/file/range.bin?app-back`, filename: 'x-app-back.bin', referrer: `${base}/page` });
  const sent = captures(w).filter((m) => m.payload.source.endsWith('?app-back'));
  check('pairing/other-app-on-port', 'an unsealed "not paired" answer from whatever holds the port does not erase the key: once the real app is back, downloads reach it again', kept && sent.length === 1, { kept, calls: w.calls, sentAfter: sent.length });
  handedOver.push(...sent.map((m) => ({ name: m.payload.name, scenario: 'pairing/other-app-on-port', source: m.payload.source, captureId: m.payload.captureId, expectJob: true })));
}
{
  // The user takes a while to answer in the app. Chrome stops an idle worker
  // after ~30 s, and the next one must still collect the answer.
  // As the app does: the Allow answers the request its window showed; a new
  // request is a new window with a new code, which nobody answers here.
  let approved = false;
  const requests = [];
  const pairApp = async (url, options) => {
    const { request } = JSON.parse(options.body);
    if (url.endsWith('/v1/pair')) requests.push(request);
    const reply = url.endsWith('/v1/pair') ? { ok: true, code: requests.length === 1 ? '123 456' : '654 321' }
      : request !== requests.at(-1) ? { state: 'unknown' }
      : approved && request === requests[0] ? { state: 'approved', ...PAIRING } : { state: 'waiting' };
    return new Response(JSON.stringify(reply), { headers: { 'Content-Type': 'application/json' } });
  };
  const session = {};
  let stopped = false;
  const first = world({ paired: false, session, pairApp: (url, options) => (stopped ? new Promise(() => {}) : pairApp(url, options)) }); await settle();
  await first.message({ type: 'pair' }, {});
  await settle(1500);
  stopped = true; // the worker is gone: no more requests, no cleanup
  const stored = {};
  const second = world({ paired: false, stored, session, pairApp }); await settle();
  const waiting = await second.message({ type: 'get-pairing' }, {});
  approved = true;
  await settle(2500);
  const after = await second.message({ type: 'get-pairing' }, {});
  check('pairing/worker-restarted', "an Allow given after Chrome restarted the extension's worker still pairs it", waiting.pairing?.state === 'waiting' && after.pairing?.state === 'paired' && stored['dm-pairing']?.keyId === PAIRING.keyId, { waiting: waiting.pairing, after: after.pairing, pairingRequests: requests.length });
}

// --- the page probe: what a player is fed, and nothing claimed in the page ---
{
  const { readFileSync } = await import('node:fs');
  const probe = readFileSync(process.env.DM_PAGE_SCRIPT ?? path.join(root, 'extension/dist/page-probe.js'), 'utf8');
  const manifest = JSON.parse(readFileSync(path.join(root, 'extension/manifest.json'), 'utf8'));
  const inPage = (manifest.content_scripts ?? []).filter((script) => script.world === 'MAIN').flatMap((script) => script.js);

  // A minimal page: one element, a MediaSource player feeding it.
  class Target {
    constructor() { this.listeners = {}; }
    addEventListener(type, listener) { (this.listeners[type] ??= []).push(listener); }
    removeEventListener(type, listener) { this.listeners[type] = (this.listeners[type] ?? []).filter((item) => item !== listener); }
  }
  const document = new Target();
  class Element extends Target {}
  class HTMLMediaElement extends Element {
    constructor() { super(); this.src = ''; this.currentSrc = ''; }
    dispatchEvent(event) {
      event.target = this;
      event.composedPath = () => [this, document];
      for (const listener of document.listeners[event.type] ?? []) listener(event);
      for (const listener of this.listeners[event.type] ?? []) listener(event);
      return true;
    }
  }
  class CustomEvent { constructor(type, init = {}) { this.type = type; this.detail = init.detail; } }
  class MediaSource {}
  class SourceBuffer { constructor() { this.received = []; } }
  MediaSource.prototype.addSourceBuffer = function addSourceBuffer() { return new SourceBuffer(); };
  SourceBuffer.prototype.appendBuffer = function appendBuffer(data) { this.received.push(data.byteLength); };
  SourceBuffer.prototype.changeType = function changeType() {};
  let blobs = 0;
  const URLish = { createObjectURL: function createObjectURL() { blobs += 1; return `blob:https://page.example/${blobs}`; } };
  const realm = vm.createContext({ document, Element, HTMLMediaElement, CustomEvent, MediaSource, SourceBuffer, URL: URLish, ArrayBuffer, WeakRef, WeakMap, Map, Proxy, Reflect, JSON, Date, String });
  vm.runInContext(probe, realm);

  const player = new MediaSource();
  const element = new HTMLMediaElement();
  element.src = element.currentSrc = URLish.createObjectURL(player);
  const video = player.addSourceBuffer('video/mp4; codecs="avc1.64001f"');
  const audio = player.addSourceBuffer('audio/mp4; codecs="mp4a.40.2"');
  video.appendBuffer(new Uint8Array(1234));
  video.appendBuffer(new Uint8Array(56789));
  audio.appendBuffer(new ArrayBuffer(4321));
  const other = new HTMLMediaElement();
  other.src = other.currentSrc = URLish.createObjectURL(new MediaSource());
  const ask = (target) => {
    let answer;
    target.addEventListener('dm-player-evidence', (event) => { answer ??= event.detail; });
    target.dispatchEvent(new CustomEvent('dm-player-evidence-request'));
    return JSON.parse(answer ?? 'null');
  };
  const unseen = new HTMLMediaElement();
  unseen.src = unseen.currentSrc = 'blob:https://page.example/made-elsewhere';
  const answered = ask(element);
  const sizes = answered?.map((track) => [track.mime.split(';')[0], track.appends.map(([bytes]) => bytes)]);
  check('probe/what-the-player-is-fed', "the page probe reports each of the clicked element's tracks and the sizes it was given, and only that element's", JSON.stringify(sizes) === JSON.stringify([['video/mp4', [1234, 56789]], ['audio/mp4', [4321]]]) && JSON.stringify(ask(other)) === '[]' && ask(unseen) === null && JSON.stringify(video.received) === '[1234,56789]', { sizes, otherElement: ask(other), unseenPlayer: ask(unseen), stillAppended: video.received });
  check('probe/wrappers-keep-names', 'the wrapped functions keep their names', SourceBuffer.prototype.appendBuffer.name === 'appendBuffer' && MediaSource.prototype.addSourceBuffer.name === 'addSourceBuffer' && URLish.createObjectURL.name === 'createObjectURL', { names: [SourceBuffer.prototype.appendBuffer.name, MediaSource.prototype.addSourceBuffer.name, URLish.createObjectURL.name] });

  // A classic script in the page's world shares its top-level names with the
  // page's scripts (an unwrapped one once broke Google's account menu).
  const fresh = vm.createContext({});
  try { vm.runInContext(probe, fresh); } catch { /* browser APIs are absent here */ }
  const letters = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_$';
  const names = [...letters, ...[...letters].flatMap((first) => [...letters, ...'0123456789'].map((second) => first + second))];
  const clashes = names.filter((name) => { try { vm.runInContext(`let ${name} = 0;`, fresh); return false; } catch (error) { return /already been declared/.test(String(error)); } });
  check('probe/no-global-names', "the page's own scripts can declare any short top-level name next to the probe, which is the only script in the page's world", clashes.length === 0 && JSON.stringify(inPage) === '["page-probe.js"]', { clashes: clashes.slice(0, 20), mainWorldScripts: inPage });
}

// --- what the player played decides, not what the URLs look like ------------
// Fixture presentations (native.py): /pe/a/ lists segments under /pe/seg/7f3a
// (video) and /pe/seg/91c2 (audio); /pe/b/ lists /pe/seg/2d0e. Playlist and
// segment URLs share nothing.
const sizeOf = async (url) => (await (await fetch(url)).arrayBuffer()).byteLength;
const PE_A = { video: ['init.mp4', '0.m4s', '1.m4s', '2.m4s'].map((file) => `${base}/pe/seg/7f3a/${file}`), audio: ['init.mp4', '0.m4s'].map((file) => `${base}/pe/seg/91c2/${file}`) };
const sizes = new Map();
for (const url of [...PE_A.video, ...PE_A.audio]) sizes.set(url, await sizeOf(url));
let responseNumber = 0;
/** The tab received `url`: its headers, as webRequest reports them. */
function received(w, tabId, url, { at = Date.now(), bytes, contentType = 'video/mp4', status = 200, range } = {}) {
  const headers = [{ name: 'content-type', value: contentType }];
  if (bytes !== undefined) headers.push({ name: 'content-length', value: String(bytes) });
  if (range) headers.push({ name: 'content-range', value: range });
  w.emit('headers', { tabId, frameId: 0, documentId: `doc-${tabId}`, type: 'xmlhttprequest', url, requestId: `r${++responseNumber}`, statusCode: status, timeStamp: at, responseHeaders: headers });
}
const playlist = (w, tabId, url, at) => received(w, tabId, url, { at, contentType: 'application/vnd.apple.mpegurl' });
/** The player was fed these files' bytes, in order, a moment after each arrived. */
const appendsOf = (urls, at) => urls.map((url, index) => [sizes.get(url), at + 50 + index * 10]);
const captureMedia = (w, tabId, player, name) => w.message({ type: 'media-capture', payload: { source: '', player, pageUrl: `${base}/pe-page`, playerKind: 'video', name } }, { tab: { id: tabId }, frameId: 0, documentId: `doc-${tabId}` });
const mediaCaptures = (w) => w.outbound.filter((m) => m.type === 'media-capture').map((m) => m.payload);
{
  // The film plays; a preview's playlist is the last one the page loaded.
  const w = world(); await settle();
  const at = Date.now() - 4_000;
  for (const url of ['/pe/a/master.m3u8', '/pe/a/video.m3u8', '/pe/a/audio.m3u8']) playlist(w, 21, base + url, at);
  [...PE_A.video, ...PE_A.audio].forEach((url, index) => received(w, 21, url, { at: at + 100 + index * 10, bytes: sizes.get(url) }));
  for (const url of ['/pe/b/master.m3u8', '/pe/b/video.m3u8']) playlist(w, 21, base + url, at + 900);
  const reply = await captureMedia(w, 21, [{ mime: 'video/mp4; codecs="avc1.64001e"', appends: appendsOf(PE_A.video, at + 100) }, { mime: 'audio/mp4; codecs="mp4a.40.2"', appends: appendsOf(PE_A.audio, at + 140) }], 'pe-film.mp4');
  const sent = mediaCaptures(w);
  const named = sent[0]?.selectedSegments ?? [];
  check('player/played-files', "the files handed over are exactly the ones the player's appends came from, with every playlist the page loaded", reply.ok === true && JSON.stringify(named) === JSON.stringify([...PE_A.video, ...PE_A.audio]) && [sent[0]?.source, ...(sent[0]?.candidates ?? [])].includes(`${base}/pe/a/master.m3u8`), { reply: { ok: reply.ok, error: reply.error }, source: sent[0]?.source, candidates: sent[0]?.candidates, selected: named.map((url) => url.slice(base.length)) });
  handedOver.push(...sent.map((p) => ({ scenario: 'player/played-files', name: p.name, source: p.source, captureId: p.captureId, jobId: reply.id, expect: { states: ['ready', 'completed'], source: '/pe/a/master.m3u8', notRequested: ['/pe/seg/2d0e/'], guards: "the app takes the playlist that lists what the player played (the film's), not the one loaded last (the preview's), and downloads it" } })));
}
{
  // A thread of videos: the clicked one's playlists were loaded first, and
  // twenty other videos' playlists after them.
  const w = world(); await settle();
  const at = Date.now() - 6_000;
  for (const url of ['/pe/a/master.m3u8', '/pe/a/video.m3u8', '/pe/a/audio.m3u8']) playlist(w, 26, base + url, at);
  [...PE_A.video, ...PE_A.audio].forEach((url, index) => received(w, 26, url, { at: at + 100 + index * 10, bytes: sizes.get(url) }));
  for (let other = 0; other < 20; other += 1) {
    playlist(w, 26, `${base}/pe/b/master.m3u8?video=${other}`, at + 1000 + other * 100);
    playlist(w, 26, `${base}/pe/b/video.m3u8?video=${other}`, at + 1050 + other * 100);
  }
  const reply = await captureMedia(w, 26, [{ mime: 'video/mp4; codecs="avc1.64001e"', appends: appendsOf(PE_A.video, at + 100) }, { mime: 'audio/mp4; codecs="mp4a.40.2"', appends: appendsOf(PE_A.audio, at + 140) }], 'pe-thread.mp4');
  const sent = mediaCaptures(w);
  const offered = [sent[0]?.source, ...(sent[0]?.candidates ?? [])];
  check('player/thread-of-videos', "with forty newer playlists in the tab, the clicked video's own playlist is still handed over", reply.ok === true && offered.includes(`${base}/pe/a/master.m3u8`), { reply: { ok: reply.ok, error: reply.error }, playlists: offered.length });
  handedOver.push(...sent.map((p) => ({ scenario: 'player/thread-of-videos', name: p.name, source: p.source, captureId: p.captureId, jobId: reply.id, expect: { states: ['ready', 'completed'], source: '/pe/a/master.m3u8', notRequested: ['/pe/seg/2d0e/'], guards: "in a thread of videos the app takes the clicked video's playlist, video and audio, however many others the page loaded after it" } })));
}
{
  // The same, with a player that converts what it downloads: its appends
  // have other lengths, each made right after a download arrived.
  const w = world(); await settle();
  const at = Date.now() - 4_000;
  playlist(w, 22, `${base}/pe/a/master.m3u8`, at);
  PE_A.video.forEach((url, index) => received(w, 22, url, { at: at + index * 400, bytes: sizes.get(url) }));
  const reply = await captureMedia(w, 22, [{ mime: 'video/mp4; codecs="avc1.64001e,mp4a.40.2"', appends: PE_A.video.map((url, index) => [sizes.get(url) + 188, at + index * 400 + 30]) }], 'pe-converted.mp4');
  const named = mediaCaptures(w)[0]?.selectedSegments ?? [];
  check('player/converted-stream', 'a player that converts its downloads (appended lengths differ) is matched by when each download arrived', JSON.stringify(named) === JSON.stringify(PE_A.video), { reply: { ok: reply.ok, error: reply.error }, selected: named.map((url) => url.slice(base.length)) });
  handedOver.push(...mediaCaptures(w).map((p) => ({ scenario: 'player/converted-stream', name: p.name, source: p.source, captureId: p.captureId, jobId: reply.id })));
}
{
  // A progressive player (dash.js on one file per track, YouTube-style
  // ranges): no playlist, the same two files read in ranges.
  const w = world(); await settle();
  const at = Date.now() - 2_000;
  received(w, 23, `${base}/progressive/video.mp4`, { at, status: 206, bytes: 10000, range: 'bytes 0-9999/32058' });
  received(w, 23, `${base}/progressive/video.mp4`, { at: at + 200, status: 206, bytes: 22058, range: 'bytes 10000-32057/32058' });
  received(w, 23, `${base}/progressive/audio.mp4`, { at: at + 100, status: 206, bytes: 55177, range: 'bytes 0-55176/55177', contentType: 'video/mp4' });
  const reply = await captureMedia(w, 23, [{ mime: 'video/mp4; codecs="avc1.64001e"', appends: [[10000, at + 50], [22058, at + 250]] }, { mime: 'audio/mp4; codecs="mp4a.40.2"', appends: [[55177, at + 150]] }], 'pe-progressive.mp4');
  const sent = mediaCaptures(w);
  check('player/one-file-per-track', "a player reading one file per track hands over that file and its audio file, whatever the audio is labelled", reply.ok === true && sent[0]?.source === `${base}/progressive/video.mp4` && sent[0]?.companionAudio === `${base}/progressive/audio.mp4`, { reply: { ok: reply.ok, error: reply.error }, source: sent[0]?.source, companionAudio: sent[0]?.companionAudio });
  handedOver.push(...sent.map((p) => ({ scenario: 'player/one-file-per-track', name: p.name, source: p.source, captureId: p.captureId, jobId: reply.id, expect: { states: ['ready', 'completed'], source: '/progressive/video.mp4', guards: 'the app downloads the file the player read from, with its audio' } })));
}
{
  // Segments played, but the page's playlist was never seen: no guess.
  const w = world(); await settle();
  const at = Date.now() - 2_000;
  PE_A.video.forEach((url, index) => received(w, 24, url, { at: at + index * 10, bytes: sizes.get(url) }));
  const reply = await captureMedia(w, 24, [{ mime: 'video/mp4', appends: appendsOf(PE_A.video, at) }], 'pe-unlisted.mp4');
  check('player/segments-without-playlist', 'segments whose playlist was never seen are not handed over as if one of them were the video', reply.ok === false && mediaCaptures(w).length === 0, { reply });
}
{
  // A player whose MediaSource the probe never saw (another extension's
  // player, as RES plays v.redd.it): said so, not "play first".
  const w = world(); await settle();
  const reply = await captureMedia(w, 27, null, 'pe-not-visible.mp4');
  check('player/not-visible', 'a player the page probe cannot see is reported as not visible, not as unplayed', reply.ok === false && reply.reason === 'not-visible' && mediaCaptures(w).length === 0, { reply });
}
{
  const w = world(); await settle();
  const reply = await captureMedia(w, 25, [{ mime: 'video/mp4', appends: [] }], 'pe-not-played.mp4');
  check('player/play-first', 'a player that has loaded nothing yet asks to be played first', reply.ok === false && reply.reason === 'not-played' && mediaCaptures(w).length === 0, { reply });
}

console.log(JSON.stringify({ scenarios, handedOver }));
