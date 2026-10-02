import { APP_BRIDGE_ORIGIN, APP_BRIDGE_TIMEOUT_MS, APP_CAPTURE_TIMEOUT_MS, APP_MEDIA_CAPTURE_TIMEOUT_MS, DEFAULT_MEDIA_FILTERS, DEFAULT_POLICY, isHttp, mediaFileTypeFor, normalizeMediaFilterSettings, siteOf, type BrowserPolicy, type MediaFilterSettings } from './shared';
import { cleanPlayerTracks, isManifest, normalizeChunkUrl, playedTracks, responseLengths, type ObservedResponse } from './player-evidence';

const POLICY_KEY = 'dm-policy';
const MEDIA_FILTERS_KEY = 'dm-media-filters';

// The tab's recent responses, by tab: what a player's appends are matched
// against (player-evidence.ts). URLs and lengths only; no bodies, no cookies.
const responsesByTab = new Map<number, ObservedResponse[]>();
const RESPONSES_PER_TAB = 1500;
const RESPONSE_TTL_MS = 10 * 60_000;
// The playlists each tab loaded, most recent last. A player keeps playing
// long after its playlist was fetched, and Chrome may restart this worker in
// between, so they are kept longer and in session storage.
const MANIFESTS_KEY = 'dm-manifests';
const MANIFESTS_PER_TAB = 16;
const MANIFEST_TTL_MS = 60 * 60_000;
type SeenManifest = { url: string; at: number };
const manifestsByTab = new Map<number, SeenManifest[]>();

function rememberResponse(tabId: number, response: ObservedResponse): void {
  const now = Date.now();
  const responses = (responsesByTab.get(tabId) ?? []).filter((item) => now - item.at <= RESPONSE_TTL_MS);
  responses.push(response);
  if (responses.length > RESPONSES_PER_TAB) responses.splice(0, responses.length - RESPONSES_PER_TAB);
  responsesByTab.set(tabId, responses);
  if (!response.manifest) return;
  const manifests = (manifestsByTab.get(tabId) ?? []).filter((item) => now - item.at <= MANIFEST_TTL_MS);
  const known = manifests.findIndex((item) => item.url === response.url);
  if (known >= 0) manifests.splice(known, 1);
  manifests.push({ url: response.url, at: response.at });
  if (manifests.length > MANIFESTS_PER_TAB) manifests.shift();
  manifestsByTab.set(tabId, manifests);
  if (known < 0) persistManifests();
}

/** The playlists a tab loaded, most recent first. */
function manifestsOf(tabId: number): string[] {
  const now = Date.now();
  return (manifestsByTab.get(tabId) ?? []).filter((item) => now - item.at <= MANIFEST_TTL_MS).map((item) => item.url).reverse();
}

function persistManifests(): void {
  try {
    void chrome.storage.session?.set({ [MANIFESTS_KEY]: [...manifestsByTab.entries()] }).catch(() => undefined);
  } catch {
    // Best effort: the in-memory record still serves this worker.
  }
}

async function hydrateManifests(): Promise<void> {
  try {
    const stored = (await chrome.storage.session?.get(MANIFESTS_KEY))?.[MANIFESTS_KEY];
    if (!Array.isArray(stored)) return;
    for (const entry of stored) {
      if (!Array.isArray(entry) || typeof entry[0] !== 'number' || !Array.isArray(entry[1])) continue;
      const manifests = (entry[1] as unknown[]).filter((item): item is SeenManifest =>
        !!item && typeof (item as SeenManifest).url === 'string' && isHttp((item as SeenManifest).url) && typeof (item as SeenManifest).at === 'number');
      if (manifests.length && !manifestsByTab.has(entry[0])) manifestsByTab.set(entry[0], manifests.slice(-MANIFESTS_PER_TAB));
    }
  } catch {
    // Start empty when session storage is unavailable.
  }
}

chrome.tabs?.onRemoved?.addListener((tabId) => {
  responsesByTab.delete(tabId);
  if (manifestsByTab.delete(tabId)) persistManifests();
});

let policy: BrowserPolicy = { ...DEFAULT_POLICY };
let policyLoadError = '';
let policyReady: Promise<void> = Promise.resolve();
let mediaFilters: MediaFilterSettings = { ...DEFAULT_MEDIA_FILTERS, excludedFileTypes: [...DEFAULT_MEDIA_FILTERS.excludedFileTypes] };
let mediaFiltersReady: Promise<void> = Promise.resolve();

type PendingBrowserFallback = { source: string; name?: string; at: number };
const pendingBrowserFallbacks: PendingBrowserFallback[] = [];
const pendingBrowserOwnedDownloads: PendingBrowserFallback[] = [];
const BROWSER_FALLBACK_TTL_MS = 30_000;
const BROWSER_OWNED_DOWNLOAD_TTL_MS = 10_000;
const BROWSER_FALLBACK_MAX = 20;
const BROWSER_OWNED_DOWNLOAD_MAX = 20;

function pruneBrowserFallbacks(now = Date.now()): void {
  while (pendingBrowserFallbacks.length && now - pendingBrowserFallbacks[0].at > BROWSER_FALLBACK_TTL_MS) pendingBrowserFallbacks.shift();
  while (pendingBrowserFallbacks.length > BROWSER_FALLBACK_MAX) pendingBrowserFallbacks.shift();
}

function rememberBrowserFallback(source: string, name?: string): void {
  pruneBrowserFallbacks();
  pendingBrowserFallbacks.push({ source, name, at: Date.now() });
}

function consumeBrowserFallback(item: chrome.downloads.DownloadItem): boolean {
  pruneBrowserFallbacks();
  const name = cleanFilename(item.filename);
  const index = pendingBrowserFallbacks.findIndex((pending) =>
    (pending.source === item.url || pending.source === item.finalUrl) &&
    (!pending.name || !name || pending.name === name),
  );
  if (index < 0) return false;
  pendingBrowserFallbacks.splice(index, 1);
  return true;
}

function rememberBrowserOwnedDownload(source: string, name?: string): void {
  const now = Date.now();
  while (pendingBrowserOwnedDownloads.length && now - pendingBrowserOwnedDownloads[0].at > BROWSER_OWNED_DOWNLOAD_TTL_MS) pendingBrowserOwnedDownloads.shift();
  while (pendingBrowserOwnedDownloads.length >= BROWSER_OWNED_DOWNLOAD_MAX) pendingBrowserOwnedDownloads.shift();
  pendingBrowserOwnedDownloads.push({ source, name, at: now });
}

function consumeBrowserOwnedDownload(item: chrome.downloads.DownloadItem): boolean {
  const now = Date.now();
  while (pendingBrowserOwnedDownloads.length && now - pendingBrowserOwnedDownloads[0].at > BROWSER_OWNED_DOWNLOAD_TTL_MS) pendingBrowserOwnedDownloads.shift();
  const name = cleanFilename(item.filename);
  const index = pendingBrowserOwnedDownloads.findIndex((pending) =>
    (pending.source === item.url || pending.source === item.finalUrl) &&
    (!pending.name || !name || pending.name === name),
  );
  if (index < 0) return false;
  pendingBrowserOwnedDownloads.splice(index, 1);
  return true;
}

async function loadPolicy(): Promise<void> {
  try {
    const stored = await chrome.storage.local.get(POLICY_KEY);
    const saved = stored[POLICY_KEY] as Partial<BrowserPolicy> | undefined;
    if (saved) {
      policy = {
        interceptDownloads: saved.interceptDownloads ?? DEFAULT_POLICY.interceptDownloads,
        showMediaButtons: saved.showMediaButtons ?? DEFAULT_POLICY.showMediaButtons,
        excludedSites: Array.isArray(saved.excludedSites) ? saved.excludedSites : [],
        updatedAt: typeof saved.updatedAt === 'number' ? saved.updatedAt : 0,
      };
    }
    policyLoadError = '';
  } catch (reason) {
    policy = { ...DEFAULT_POLICY };
    policyLoadError = reason instanceof Error && reason.message ? reason.message : 'Could not load browser integration settings.';
  }
}

async function savePolicy(next: BrowserPolicy = policy): Promise<void> {
  await chrome.storage.local.set({ [POLICY_KEY]: next });
}

async function loadMediaFilters(): Promise<void> {
  try {
    const stored = await chrome.storage.local.get(MEDIA_FILTERS_KEY);
    mediaFilters = normalizeMediaFilterSettings(stored[MEDIA_FILTERS_KEY]);
  } catch {
    mediaFilters = { ...DEFAULT_MEDIA_FILTERS, excludedFileTypes: [...DEFAULT_MEDIA_FILTERS.excludedFileTypes] };
  }
}

async function saveMediaFilters(next: MediaFilterSettings = mediaFilters): Promise<void> {
  await chrome.storage.local.set({ [MEDIA_FILTERS_KEY]: next });
}

function residentPolicy(response: unknown): BrowserPolicy | null {
  const remote = (response as { policy?: Partial<BrowserPolicy> } | undefined)?.policy;
  if (!remote || typeof remote.interceptDownloads !== 'boolean') return null;
  return {
    interceptDownloads: remote.interceptDownloads,
    showMediaButtons: remote.showMediaButtons ?? policy.showMediaButtons,
    excludedSites: Array.isArray(remote.excludedSites)
      ? remote.excludedSites.filter((site): site is string => typeof site === 'string')
      : policy.excludedSites,
    updatedAt: typeof remote.updatedAt === 'number' ? remote.updatedAt : 0,
  };
}

async function adoptPolicy(next: BrowserPolicy): Promise<void> {
  const changed = next.interceptDownloads !== policy.interceptDownloads
    || next.showMediaButtons !== policy.showMediaButtons
    || next.updatedAt !== policy.updatedAt
    || next.excludedSites.length !== policy.excludedSites.length
    || next.excludedSites.some((site, index) => site !== policy.excludedSites[index]);
  policy = next;
  if (changed) await savePolicy();
}

/** Both sides keep the policy; the one changed last wins. A change made here
 *  while the resident was not running is sent to it now. */
async function syncPolicyWithResident(): Promise<void> {
  const remote = residentPolicy(await sendApp({ type: 'get-policy' }));
  if (!remote) return;
  if (remote.updatedAt >= policy.updatedAt) {
    await adoptPolicy(remote);
    return;
  }
  const answer = residentPolicy(await sendApp({ type: 'update-policy', payload: policy }));
  if (answer) await adoptPolicy(answer);
}

let residentPolicySync: Promise<void> | null = null;
let residentPolicySyncedAt = 0;

async function refreshResidentPolicy(): Promise<void> {
  if (Date.now() - residentPolicySyncedAt < 1000) return;
  if (!residentPolicySync) {
    residentPolicySync = syncPolicyWithResident()
      .then(() => { residentPolicySyncedAt = Date.now(); })
      .finally(() => { residentPolicySync = null; });
  }
  await residentPolicySync;
}

/** The policy an interception decision should use: the resident's current
 *  one when it answers, else the last known. Pages no longer poll the worker
 *  every second, so freshness is established here, where it matters. */
async function decisionPolicyReady(): Promise<void> {
  await policyReady;
  try {
    await refreshResidentPolicy();
  } catch {
    // The resident is stopped: the stored browser-owned policy stands.
  }
}

async function sendApp(message: unknown, timeoutMs = APP_BRIDGE_TIMEOUT_MS): Promise<unknown> {
  const type = (message as { type?: string })?.type;
  const route = type === 'get-policy' ? '/v1/policy' : type === 'open-manager' ? '/v1/manager' : type === 'update-policy' ? '/v1/policy' : '/v1/capture';
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(`${APP_BRIDGE_ORIGIN}${route}`, {
      method: type === 'get-policy' ? 'GET' : 'POST',
      headers: type === 'get-policy' ? undefined : { 'Content-Type': 'application/json' },
      body: type === 'get-policy' ? undefined : JSON.stringify(message),
      signal: controller.signal,
    });
    const payload = await response.json() as unknown;
    if (!response.ok && typeof payload === 'object' && payload !== null && 'error' in payload) return payload;
    return payload;
  } catch {
    return { ok: false, unanswered: true, error: 'Download Manager is not running' };
  } finally {
    clearTimeout(timer);
  }
}

type HandOverReply = { ok?: boolean; id?: string; error?: string; handback?: boolean; unanswered?: boolean };

/** Hands one capture to the resident under a fresh capture id. When no answer
 *  arrives, the resident may still have created the job before the answer was
 *  lost: it is cancelled by id, so the browser fallback that follows is the
 *  only owner. The resident honours a cancel that overtakes its create. */
async function handOver(type: 'capture-acquisition' | 'media-capture', payload: Record<string, unknown>, timeoutMs: number): Promise<HandOverReply> {
  const captureId = crypto.randomUUID();
  const reply = ((await sendApp({ type, payload: { ...payload, captureId } }, timeoutMs)) ?? {}) as HandOverReply;
  if (reply.unanswered) void sendApp({ type: 'cancel-acquisition', payload: { captureId } });
  return reply;
}

function cleanUserAgent(value: unknown): string | undefined {
  if (typeof value !== 'string') return undefined;
  const candidate = value.trim();
  if (!candidate || candidate.length > 512 || /[\r\n]/.test(candidate)) return undefined;
  return candidate;
}

const MEDIA_EVIDENCE_URL_MAX = 4096;

function cleanMediaUrl(value: unknown): string | undefined {
  if (typeof value !== 'string' || value.length === 0 || value.length > MEDIA_EVIDENCE_URL_MAX) return undefined;
  try {
    const parsed = new URL(value);
    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return undefined;
    parsed.hash = '';
    return parsed.href;
  } catch {
    return undefined;
  }
}

function cleanFilename(value: unknown): string | undefined {
  if (typeof value !== 'string') return undefined;
  const leaf = value.trim().split('/').pop()?.split('\\').pop()?.trim();
  return leaf || undefined;
}

function ordinaryCaptureError(source: string, pageUrl: string): string | undefined {
  const pageSite = siteOf(pageUrl);
  if (!policy.interceptDownloads) return 'ordinary interception disabled';
  if (pageSite && policy.excludedSites.includes(pageSite)) return 'site excluded';
  if (!isHttp(source)) return 'invalid source';
  return undefined;
}

/** A download the Downloads API reports with no referrer (a rel=noreferrer
 *  link, a no-referrer policy) still comes from some page, and an excluded
 *  site must not be bypassed that way. The page is unknown, so the
 *  download's own site and the focused tab's site stand in for it; a false
 *  match only leaves the download with the browser. */
async function excludedWithoutReferrer(source: string): Promise<boolean> {
  if (!policy.excludedSites.length) return false;
  const sites = [siteOf(source)];
  try {
    const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
    if (tab?.url) sites.push(siteOf(tab.url));
  } catch {
    // No tab to consult: the download's own site still applies.
  }
  return sites.some((site) => !!site && policy.excludedSites.includes(site));
}

function mediaCapturePolicyError(pageUrl: string): string | undefined {
  if (!policy.showMediaButtons) return 'media buttons disabled';
  const pageSite = siteOf(pageUrl);
  if (pageSite && policy.excludedSites.includes(pageSite)) return 'site excluded';
  return undefined;
}

async function captureOrdinary(payload: Record<string, unknown>): Promise<{ ok: boolean; error?: string }> {
  await policyReady;
  const source = typeof payload.source === 'string' ? payload.source.trim() : '';
  const pageUrl = typeof payload.pageUrl === 'string' ? payload.pageUrl : '';
  await decisionPolicyReady();
  const captureError = ordinaryCaptureError(source, pageUrl);
  if (captureError) {
    return { ok: false, error: captureError };
  }
  const response = await handOver('capture-acquisition', {
    source,
    name: cleanFilename(payload.name),
    nameIsHint: true,
    pageUrl: typeof payload.pageUrl === 'string' ? payload.pageUrl : undefined,
    referrer: typeof payload.pageUrl === 'string' ? payload.pageUrl : undefined,
    userAgent: cleanUserAgent(payload.userAgent),
    requireViable: true,
  }, APP_CAPTURE_TIMEOUT_MS);
  if (response.ok) return { ok: true };
  // The pre-browser path consumed the anchor event. If the resident is
  // unavailable, or handed the capture back because it cannot fetch the file
  // with the context it has (session cookies, one-use URL), the browser does
  // the download with its own context. The interception listener ignores
  // downloads started by this extension.
  try {
    rememberBrowserFallback(source, cleanFilename(payload.name));
    const id = await chrome.downloads.download({
      url: source,
      filename: cleanFilename(payload.name),
      saveAs: false,
    });
    return { ok: typeof id === 'number' };
  } catch {
    // No safe fallback remains. Do not navigate the page or invent success.
    return { ok: false, error: 'Download Manager and browser fallback are unavailable' };
  }
}

function responseHeader(details: chrome.webRequest.OnHeadersReceivedDetails, name: string): string {
  return details.responseHeaders?.find((header) => header.name.toLowerCase() === name)?.value?.trim() ?? '';
}

/** The latest response seen for `url` (in `tabId`, when given). */
function seenResponse(url: string, tabId?: number): ObservedResponse | undefined {
  const normalized = normalizeChunkUrl(url);
  const pool = tabId === undefined ? [...responsesByTab.values()].flat() : responsesByTab.get(tabId) ?? [];
  return pool.filter((item) => item.url === normalized).sort((left, right) => right.at - left.at)[0];
}

type MediaFilterDecision = { allowed: boolean; type?: string; totalBytes?: number; reason?: 'excluded-type' | 'below-minimum' };

function mediaFilterDecision(source: string, tabId?: number, contentType = '', companionAudio?: string): MediaFilterDecision {
  const seen = seenResponse(source, tabId);
  const observedContentType = seen?.contentType || contentType;
  if (isManifest(source, observedContentType)) return { allowed: true };
  const type = mediaFileTypeFor(source, observedContentType);
  const totalBytes = seen?.total;
  if (type && mediaFilters.excludedFileTypes.includes(type)) return { allowed: false, type, reason: 'excluded-type', ...(totalBytes === undefined ? {} : { totalBytes }) };
  if (mediaFilters.minimumSizeBytes > 0 && totalBytes !== undefined) {
    const companionTotal = companionAudio === undefined ? undefined : seenResponse(companionAudio, tabId)?.total;
    const aggregate = companionTotal === undefined ? undefined : totalBytes + companionTotal;
    if ((aggregate === undefined && companionAudio === undefined && totalBytes < mediaFilters.minimumSizeBytes) || (aggregate !== undefined && aggregate < mediaFilters.minimumSizeBytes)) {
      return { allowed: false, ...(type ? { type } : {}), totalBytes, reason: 'below-minimum' };
    }
  }
  return { allowed: true, ...(type ? { type } : {}), ...(totalBytes === undefined ? {} : { totalBytes }) };
}

// Observe (never block) the tab's responses: their URL, lengths and type.
// Page documents, scripts, styles and JSON are not media and are skipped.
chrome.webRequest.onHeadersReceived.addListener(
  (details) => {
    if (details.tabId < 0) return undefined;
    if (details.type !== 'media' && details.type !== 'xmlhttprequest' && details.type !== 'other') return undefined;
    const contentType = responseHeader(details, 'content-type');
    const manifest = isManifest(details.url, contentType);
    if (!manifest && /^(?:text\/|application\/(?:json|javascript|x-javascript|xml)\b)/i.test(contentType)) return undefined;
    rememberResponse(details.tabId, {
      url: normalizeChunkUrl(details.url),
      at: details.timeStamp,
      manifest,
      ...(contentType ? { contentType } : {}),
      ...responseLengths(details.statusCode, responseHeader(details, 'content-range'), responseHeader(details, 'content-length'), responseHeader(details, 'content-encoding').toLowerCase()),
    });
    return undefined;
  },
  { urls: ['<all_urls>'] },
  ['responseHeaders'],
);

// Bounded observation of reproducible URL-encoded POST bodies.
const FORM_BODY_MAX = 64 * 1024;
const FORM_BODY_TTL_MS = 60_000;
const FORM_BODY_PER_URL = 4;
const recentFormBodies: Array<{ url: string; body: string; at: number; tabId: number; frameId: number }> = [];

function pruneFormBodies(now = Date.now()): void {
  while (recentFormBodies.length && now - recentFormBodies[0].at > FORM_BODY_TTL_MS) recentFormBodies.shift();
  while (recentFormBodies.length > 64) recentFormBodies.shift();
}

function formBodyFromDetails(details: chrome.webRequest.OnBeforeRequestDetails): string | undefined {
  const formData = details.requestBody?.formData;
  if (!formData) return undefined;
  const params = new URLSearchParams();
  for (const [key, values] of Object.entries(formData)) {
    for (const value of values) {
      if (typeof value !== 'string') return undefined;
      params.append(key, value);
    }
  }
  const text = params.toString();
  return text.length > 0 && text.length <= FORM_BODY_MAX ? text : undefined;
}

chrome.webRequest.onBeforeRequest.addListener(
  (details): undefined => {
    if (details.tabId < 0 || details.method !== 'POST') return undefined;
    const body = formBodyFromDetails(details);
    if (body === undefined) return undefined;
    pruneFormBodies();
    const url = details.url.split('#')[0];
    const queued = recentFormBodies.filter((item) => item.url === url);
    if (queued.length >= FORM_BODY_PER_URL) {
      const oldest = recentFormBodies.findIndex((item) => item.url === url);
      if (oldest >= 0) recentFormBodies.splice(oldest, 1);
    }
    recentFormBodies.push({ url, body, at: Date.now(), tabId: details.tabId, frameId: details.frameId });
    return undefined;
  },
  { urls: ['<all_urls>'] },
  ['requestBody'],
);

/** The form body behind a download the Downloads API reports without a tab.
 *  URL alone cannot say which submission a download belongs to: when recent
 *  submissions to that URL disagree, replaying any one of them could fetch
 *  another tab's export, so the answer is 'ambiguous' and the browser keeps
 *  the download. */
function takeFormBody(url: string): string | 'ambiguous' | undefined {
  pruneFormBodies();
  const matches = recentFormBodies.filter((item) => item.url === url);
  if (!matches.length) return undefined;
  if (new Set(matches.map((item) => item.body)).size > 1) return 'ambiguous';
  const found = matches[matches.length - 1];
  recentFormBodies.splice(recentFormBodies.indexOf(found), 1);
  return found.body;
}

// Downloads without an interceptable page anchor are handed over
// transactionally: pause Chromium, let the resident prove its own first
// response is the file, then cancel Chromium. A hand-back (the resident cannot
// fetch it: session cookies, one-use URL, login page), a lost answer, or any
// failed step resumes the browser's copy and cancels the resident's job, so
// exactly one owner remains and the user never loses the download (SPEC §5.1.1).
chrome.downloads.onDeterminingFilename.addListener((item, suggest) => {
  const source = item.finalUrl || item.url || '';
  if (consumeBrowserFallback(item) || consumeBrowserOwnedDownload(item) || item.byExtensionId === chrome.runtime.id || !isHttp(source)) {
    suggest();
    return;
  }
  void (async () => {
    let paused = false;
    let nativeId: string | undefined;
    try {
      await decisionPolicyReady();
      if (ordinaryCaptureError(source, item.referrer ?? '')) {
        return;
      }
      if (!item.referrer && await excludedWithoutReferrer(source)) {
        return;
      }
      const postBody = takeFormBody(source);
      if (postBody === 'ambiguous') return;
      try {
        await chrome.downloads.pause(item.id);
        paused = true;
      } catch {
        return;
      }
      // The Downloads API exposes no tab/frame identifier, so do not guess a
      // User-Agent from another document on this fallback path.
      // A matching observed POST is replayed once; otherwise acquisition uses
      // GET. The browser's copy stays paused, not cancelled, until the
      // resident proves it can fetch the file; a hand-back resumes it.
      const response = await handOver('capture-acquisition', {
        source,
        name: cleanFilename(item.filename),
        pageUrl: item.referrer,
        referrer: item.referrer,
        requireViable: true,
        ...(postBody === undefined ? {} : { method: 'POST', postBody }),
      }, APP_CAPTURE_TIMEOUT_MS);
      if (!response?.ok || typeof response.id !== 'string') {
        await chrome.downloads.resume(item.id).catch(() => undefined);
        paused = false;
        return;
      }
      nativeId = response.id;
      try {
        await chrome.downloads.cancel(item.id);
        paused = false;
      } catch {
        await sendApp({ type: 'cancel-acquisition', payload: { id: nativeId } });
        nativeId = undefined;
        await chrome.downloads.resume(item.id).catch(() => undefined);
        paused = false;
      }
    } finally {
      if (paused) await chrome.downloads.resume(item.id).catch(() => undefined);
      suggest();
    }
  })();
  return true;
});

chrome.runtime.onMessage.addListener((message, sender, reply) => {
  void (async () => {
    const type = (message as { type?: string })?.type;
    if (type === 'get-policy') {
      await Promise.all([policyReady, mediaFiltersReady]);
      try {
        await refreshResidentPolicy();
      } catch {
        // Keep the last browser-owned policy when the resident app is stopped.
      }
      const includeMediaFilters = (message as { includeMediaFilters?: boolean })?.includeMediaFilters === true;
      const extra = includeMediaFilters ? { mediaFilters } : {};
      reply(policyLoadError ? { ok: false, error: policyLoadError, policy, ...extra } : { ok: true, policy, ...extra });
    } else if (type === 'get-media-filters') {
      await mediaFiltersReady;
      reply({ ok: true, mediaFilters });
    } else if (type === 'update-media-filters') {
      await mediaFiltersReady;
      const previous = mediaFilters;
      const requested = (message as { filters?: unknown; patch?: unknown }).filters ?? (message as { patch?: unknown }).patch;
      const next = requested && typeof requested === 'object'
        ? normalizeMediaFilterSettings({ ...mediaFilters, ...(requested as Record<string, unknown>) })
        : mediaFilters;
      try {
        await saveMediaFilters(next);
      } catch (reason) {
        reply({ ok: false, error: reason instanceof Error && reason.message ? reason.message : 'Could not save media filter settings.', mediaFilters: previous });
        return;
      }
      mediaFilters = next;
      reply({ ok: true, mediaFilters });
    } else if (type === 'check-media-filters') {
      await mediaFiltersReady;
      const payload = (message as { payload?: Record<string, unknown> }).payload ?? {};
      const source = typeof payload.source === 'string' ? cleanMediaUrl(payload.source) : undefined;
      if (!source) {
        reply({ ok: true, allowed: true });
        return;
      }
      const companionAudio = typeof payload.companionAudio === 'string' ? cleanMediaUrl(payload.companionAudio) : undefined;
      const result = mediaFilterDecision(source, sender.tab?.id, typeof payload.contentType === 'string' ? payload.contentType : '', companionAudio);
      reply({ ok: true, ...result });
    } else if (type === 'update-policy') {
      const patch = (message as { patch?: Partial<BrowserPolicy> }).patch ?? {};
      const previous = policy;
      const next: BrowserPolicy = { ...policy, excludedSites: [...policy.excludedSites], updatedAt: Date.now() };
      if (typeof patch.interceptDownloads === 'boolean') next.interceptDownloads = patch.interceptDownloads;
      if (typeof patch.showMediaButtons === 'boolean') next.showMediaButtons = patch.showMediaButtons;
      if (Array.isArray(patch.excludedSites)) {
        next.excludedSites = patch.excludedSites.filter((site): site is string => typeof site === 'string');
      }
      try {
        await savePolicy(next);
      } catch (reason) {
        reply({ ok: false, error: reason instanceof Error && reason.message ? reason.message : 'Could not save browser integration settings.', policy: previous });
        return;
      }
      policy = next;
      policyLoadError = '';
      try {
        const answer = residentPolicy(await sendApp({ type: 'update-policy', payload: policy }));
        if (answer) await adoptPolicy(answer);
        residentPolicySyncedAt = Date.now();
      } catch {
        // The resident is not running: it takes this policy when it next answers.
      }
      reply({ ok: true, policy });
    } else if (type === 'ordinary-capture') {
      const payload = (message as { payload?: Record<string, unknown> }).payload ?? {};
      reply(await captureOrdinary(payload));
    } else if (type === 'browser-owned-download') {
      const payload = (message as { payload?: Record<string, unknown> }).payload ?? {};
      const source = typeof payload.source === 'string' ? payload.source.trim() : '';
      if (isHttp(source)) rememberBrowserOwnedDownload(source, cleanFilename(payload.name));
      reply({ ok: isHttp(source) });
    } else if (type === 'media-capture') {
      await Promise.all([policyReady, mediaFiltersReady]);
      const payload = (message as { payload?: Record<string, unknown> }).payload ?? {};
      const pageUrl = typeof payload.pageUrl === 'string' ? payload.pageUrl : undefined;
      const policyError = mediaCapturePolicyError(pageUrl ?? '');
      if (policyError) {
        reply({ ok: false, error: policyError });
        return;
      }
      const tabId = sender.tab?.id;
      const expectedKind = payload.playerKind === 'audio' || payload.playerKind === 'video' ? payload.playerKind : undefined;
      // A player with an http(s) source names its file.
      let source = typeof payload.source === 'string' ? cleanMediaUrl(payload.source) ?? '' : '';
      if (isHttp(source) && !isManifest(source)) source = normalizeChunkUrl(source);
      let selectedSegments: string[] = [];
      let candidates: string[] = [];
      let companionAudio: string | undefined;
      if (!isHttp(source)) {
        // A blob/MSE player: what it played, from its own appends. The app
        // picks, among the page's playlists, the one that lists these files;
        // with none, a single played file is itself the source.
        const tracks = cleanPlayerTracks(payload.player);
        if (!tracks.some((track) => track.appends.length > 0)) {
          reply({ ok: false, reason: 'not-played', error: 'Start playback first: the player has not loaded anything yet' });
          return;
        }
        const played = tabId === undefined ? [] : playedTracks(tracks, responsesByTab.get(tabId) ?? []);
        const video = played.find((track) => track.kind === 'video')?.files ?? [];
        const audio = played.find((track) => track.kind === 'audio')?.files ?? [];
        const single = (files: string[]) => (files.length > 0 && new Set(files).size === 1 ? files[0] : undefined);
        const manifests = tabId === undefined ? [] : manifestsOf(tabId);
        selectedSegments = [...video, ...audio];
        companionAudio = video.length > 0 ? single(audio) : undefined;
        source = manifests[0] ?? single(video) ?? single(audio) ?? '';
        candidates = manifests.slice(1);
        if (selectedSegments.length === 0 || !isHttp(source)) {
          reply({ ok: false, error: "The player's downloads were not seen; reload the page and play it again" });
          return;
        }
      }
      const filterResult = mediaFilterDecision(source, tabId, '', companionAudio);
      if (!filterResult.allowed) {
        reply({ ok: false, error: filterResult.reason === 'excluded-type' ? `media type ${filterResult.type ?? 'unknown'} is excluded` : 'media is below the minimum size' });
        return;
      }
      const userAgent = cleanUserAgent(payload.userAgent);
      const outboundPayload: Record<string, unknown> = {
        source,
        selectedSegments,
        candidates,
        pageUrl,
        referrer: pageUrl,
        userAgent,
        media: true,
        playerKind: expectedKind,
        ...(cleanFilename(payload.name) ? { name: cleanFilename(payload.name) } : {}),
        ...(companionAudio ? { companionAudio } : {}),
      };
      reply(await handOver('media-capture', outboundPayload, APP_MEDIA_CAPTURE_TIMEOUT_MS));
    } else if (type === 'open-manager') {
      reply(await sendApp({ type: 'open-manager' }));
    } else {
      reply({ ok: false, error: 'unsupported message' });
    }
  })();
  return true;
});

policyReady = loadPolicy();
mediaFiltersReady = loadMediaFilters();
void policyReady
  .then(() => refreshResidentPolicy())
  .catch(() => undefined);
void hydrateManifests();
