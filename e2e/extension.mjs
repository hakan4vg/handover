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

// --- no script runs in the page's own JavaScript world -----------------------
{
  // A script in the page's world shares its globals and can break its scripts
  // (an unwrapped one once broke Google's account menu); the extension needs
  // none: the background sees media traffic from every realm.
  const { readFileSync } = await import('node:fs');
  const manifest = JSON.parse(readFileSync(path.join(root, 'extension/manifest.json'), 'utf8'));
  const inPage = (manifest.content_scripts ?? []).filter((script) => script.world === 'MAIN');
  check('page-script/none-in-page-world', "no content script runs in the page's own JavaScript world", inPage.length === 0, { mainWorldScripts: inPage.map((script) => script.js) });
}

// --- media attribution: the clicked player's stream, not the page's latest ---
// A Vimeo page: the film's manifest, then a muted preview's manifest 260 ms
// later. A worker fetches the film's variant playlists and segments while it
// plays; the preview fetches nothing. Both are blob/MSE players.
const vimeo = (asset, session, tail) => `${base}/vimeo/exp=1790900895~acl=%2F${asset}%2F~hmac=00ff/${asset}/psid=${session}/v2/${tail}`;
const FILM = ['9c6d66af-d530-4333-9d02-07a74fb89b25', '2194ea3a7339e5a45b55a080d770698c'];
const PREVIEW = ['d8acfedf-8469-460f-a029-4e4f432e8b81', '4cfc27005a28f11d72859ac2536d6eeb'];
let mediaRequest = 0;
function fetched(w, url, at, contentType) {
  const requestId = `m${++mediaRequest}`;
  const where = { tabId: 9, frameId: 0, documentId: 'doc-v', type: 'xmlhttprequest', url, requestId };
  w.emit('beforeRequest', { ...where, method: 'GET', timeStamp: at });
  w.emit('headers', { ...where, statusCode: 200, responseHeaders: [{ name: 'content-type', value: contentType }] });
  w.emit('responseStarted', where);
}
const sender = { tab: { id: 9 }, frameId: 0, documentId: 'doc-v' };
const playerState = (w, playerKey, currentSrc, playing) => w.message({ type: 'media-player-state', payload: { playerKey, currentSrc, mediaIdentity: playerKey, playing, visible: true, active: playing, hovered: playing } }, sender);
const captureFor = (w, playerKey, currentSrc, pageEvidence) => w.message({ type: 'media-capture', payload: { source: '', currentSrc, mediaIdentity: playerKey, pageUrl: `${base}/vimeo-page`, playerKind: 'video', playerKey, name: 'film.mp4', ...(pageEvidence ? { pageEvidence } : {}) } }, sender);
const filmBlob = 'blob:http://127.0.0.1/film', previewBlob = 'blob:http://127.0.0.1/preview';
{
  const w = world(); await settle();
  await playerState(w, 'film', filmBlob, false);
  await playerState(w, 'preview', previewBlob, false);
  const start = Date.now();
  fetched(w, vimeo(...FILM, 'playlist/av/primary/prot/cXNyPTE/playlist.m3u8'), start, 'application/vnd.apple.mpegurl');
  fetched(w, vimeo(...PREVIEW, 'playlist/av/primary/playlist.m3u8'), start + 260, 'application/vnd.apple.mpegurl');
  await playerState(w, 'film', filmBlob, true);
  for (const [i, variant] of ['230c5f2f', 'c378f2e2'].entries()) {
    fetched(w, vimeo(...FILM, `playlist/av/793d529c/avf/${variant}/media.m3u8`), start + 400 + i, 'application/vnd.apple.mpegurl');
    for (let n = 0; n < 4; n += 1) fetched(w, vimeo(...FILM, `range/prot/cmFuZ2U9${n}${i}MC02OTU/avf/${variant}-a2f4-47e4-826f-c382d1e14f5b.mp4`) + `?range=${n}`, start + 500 + n * 10 + i, 'video/mp4');
  }
  // What the old in-page script answered for this player: every manifest on
  // the page, the preview's first.
  const reply = await captureFor(w, 'film', filmBlob, { currentSrc: filmBlob, sourceIdentity: 'source-1', playerKind: 'video', selectedSegments: [vimeo(...PREVIEW, 'playlist/av/primary/playlist.m3u8'), vimeo(...FILM, 'playlist/av/primary/prot/cXNyPTE/playlist.m3u8')] });
  const sent = w.outbound.filter((m) => m.type === 'media-capture').map((m) => m.payload);
  const sentAssets = sent.map((p) => [p.source, ...(p.candidates ?? [])].map((url) => (url.match(/[0-9a-f]{8}-[0-9a-f-]{27}/) ?? ['?'])[0].slice(0, 8)));
  check('media/clicked-players-stream', "with a preview's manifest loaded last, the button on the playing film captures the film's stream, and no fallback names the preview", sent.length === 1 && sent[0].source.includes(FILM[0]) && sent[0].source.endsWith('playlist.m3u8') && !JSON.stringify(sent[0]).includes(PREVIEW[0]), { reply: { ok: reply.ok, error: reply.error }, sentAssets });
  handedOver.push(...sent.map((p) => ({ scenario: 'media/clicked-players-stream', source: p.source, captureId: p.captureId, jobId: reply.id })));
}
{
  const w = world(); await settle();
  await playerState(w, 'film', filmBlob, false);
  await playerState(w, 'preview', previewBlob, false);
  const start = Date.now();
  fetched(w, vimeo(...FILM, 'playlist/av/primary/prot/cXNyPTE/playlist.m3u8'), start, 'application/vnd.apple.mpegurl');
  fetched(w, vimeo(...PREVIEW, 'playlist/av/primary/playlist.m3u8'), start + 260, 'application/vnd.apple.mpegurl');
  const reply = await captureFor(w, 'film', filmBlob);
  const sent = w.outbound.filter((m) => m.type === 'media-capture');
  check('media/play-first', 'before anything plays, two presentations on the page are not guessed between: the button asks to play first', reply.ok === false && reply.reason === 'not-played' && sent.length === 0, { reply, sent: sent.length });
}
{
  const w = world(); await settle();
  await playerState(w, 'film', filmBlob, false);
  fetched(w, vimeo(...FILM, 'playlist/av/primary/prot/cXNyPTE/playlist.m3u8'), Date.now(), 'application/vnd.apple.mpegurl');
  const reply = await captureFor(w, 'film', filmBlob);
  const sent = w.outbound.filter((m) => m.type === 'media-capture').map((m) => m.payload);
  check('media/single-presentation', 'a page with one presentation resolves even before playback', sent.length === 1 && sent[0].source.includes(FILM[0]), { reply: { ok: reply.ok, error: reply.error }, sent: sent.map((p) => p.source.slice(-40)) });
  handedOver.push(...sent.map((p) => ({ scenario: 'media/single-presentation', source: p.source, captureId: p.captureId, jobId: reply.id })));
}

{
  // The player is noticed late (it sits in a shadow root, or is only noticed
  // on hover): the page loaded its manifest seconds before the extension saw
  // the player's source. Its segments keep coming while it plays.
  const w = world(); await settle();
  const start = Date.now();
  fetched(w, vimeo(...FILM, 'playlist/av/primary/prot/cXNyPTE/playlist.m3u8'), start - 8_000, 'application/vnd.apple.mpegurl');
  fetched(w, vimeo(...FILM, 'playlist/av/793d529c/avf/230c5f2f/media.m3u8'), start - 7_500, 'application/vnd.apple.mpegurl');
  await playerState(w, 'film', filmBlob, true);
  for (let n = 0; n < 3; n += 1) fetched(w, vimeo(...FILM, `range/prot/cmFuZ2U9${n}MC02OTU/avf/230c5f2f-a2f4-47e4-826f-c382d1e14f5b.mp4`) + `?range=${n}`, Date.now() + n, 'video/mp4');
  const reply = await captureFor(w, 'film', filmBlob);
  const sent = w.outbound.filter((m) => m.type === 'media-capture').map((m) => m.payload);
  check('media/noticed-late', "a player noticed seconds after its manifest loaded still captures that manifest, not the segments it is fetching", sent.length === 1 && sent[0].source.includes(FILM[0]) && sent[0].source.endsWith('playlist.m3u8'), { reply: { ok: reply.ok, error: reply.error, reason: reply.reason }, sent: sent.map((p) => p.source.slice(-60)) });
  handedOver.push(...sent.map((p) => ({ scenario: 'media/noticed-late', source: p.source, captureId: p.captureId, jobId: reply.id })));
}

console.log(JSON.stringify({ scenarios, handedOver }));
