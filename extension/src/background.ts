import { DEFAULT_POLICY, NATIVE_HOST, isHttp, siteOf, type BrowserPolicy } from './shared';
import { choosePlayerEvidence, chooseWorkerMediaSelection, isMediaCandidate, mediaKindFor, roleFor, type MediaCandidate, type MediaKind, type MediaPlayerEvidence } from './media-candidates';

const POLICY_KEY = 'dm-policy';

// Bounded ring of recent media-ish traffic per tab. M0 proof vehicle for the
// generic current-media mechanism (SPEC §6): content scripts report the
// element the user interacts with, this buffer supplies the real network
// source behind blob:/MSE players. URLs only, no bodies, no cookies.
const recentMedia: MediaCandidate[] = [];
const recentPlayers: MediaPlayerEvidence[] = [];
const MEDIA_BUFFER_MAX = 60;
const MEDIA_BUFFER_MS = 90_000;
const PLAYER_BUFFER_MAX = 40;
const PLAYER_BUFFER_MS = 15_000;

let policy: BrowserPolicy = { ...DEFAULT_POLICY };
let policyLoadError = '';
let policyReady: Promise<void> = Promise.resolve();

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

async function sendNative(message: unknown): Promise<unknown> {
  try {
    return await chrome.runtime.sendNativeMessage(NATIVE_HOST, message as object);
  } catch {
    return { ok: false, error: 'native host unreachable' };
  }
}

function pruneMedia(now = Date.now()): void {
  while (recentMedia.length && now - recentMedia[0].at > MEDIA_BUFFER_MS) recentMedia.shift();
  while (recentMedia.length > MEDIA_BUFFER_MAX) recentMedia.shift();
}

function prunePlayers(now = Date.now()): void {
  while (recentPlayers.length && now - recentPlayers[0].at > PLAYER_BUFFER_MS) recentPlayers.shift();
  while (recentPlayers.length > PLAYER_BUFFER_MAX) recentPlayers.shift();
}

function cleanUserAgent(value: unknown): string | undefined {
  if (typeof value !== 'string') return undefined;
  const candidate = value.trim();
  if (!candidate || candidate.length > 512 || /[\r\n]/.test(candidate)) return undefined;
  return candidate;
}

function rememberPlayer(payload: Record<string, unknown>, tabId: number, frameId: number, documentId?: string): void {
  const playerKey = typeof payload.playerKey === 'string' ? payload.playerKey.trim() : '';
  if (!playerKey) return;
  const now = Date.now();
  prunePlayers(now);
  const evidence: MediaPlayerEvidence = {
    playerKey,
    tabId,
    frameId,
    at: now,
    documentId,
    active: payload.active === true,
    hovered: payload.hovered === true,
    playing: payload.playing === true,
    visible: payload.visible === true,
  };
  const existing = recentPlayers.find((item) => item.playerKey === playerKey && item.tabId === tabId && item.frameId === frameId && item.documentId === documentId);
  if (existing) Object.assign(existing, evidence);
  else recentPlayers.push(evidence);
}

function activePlayerKey(tabId: number, frameId: number, documentId?: string): string | undefined {
  prunePlayers();
  return choosePlayerEvidence(recentPlayers, tabId, frameId, Date.now(), documentId)?.playerKey;
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
  const captureError = ordinaryCaptureError(source, pageUrl);
  if (captureError) {
    return { ok: false, error: captureError };
  }
  const response = (await sendNative({
    type: 'capture-acquisition',
    payload: {
      source,
      name: cleanFilename(payload.name),
      pageUrl: typeof payload.pageUrl === 'string' ? payload.pageUrl : undefined,
      referrer: typeof payload.pageUrl === 'string' ? payload.pageUrl : undefined,
      userAgent: cleanUserAgent(payload.userAgent),
    },
  })) as { ok?: boolean };
  if (response?.ok) return { ok: true };
  // The pre-browser path consumed the anchor event. If native messaging is
  // unavailable, preserve the user's download through the extension API;
  // the onCreated listener ignores downloads started by this extension.
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
    return { ok: false, error: 'native host and browser fallback unavailable' };
  }
}

function rememberMedia(url: string, tabId: number, frameId: number, role = roleFor(url), documentId?: string, playerKey = activePlayerKey(tabId, frameId, documentId), kind: MediaKind = mediaKindFor(url)): void {
  if (!isHttp(url)) return;
  pruneMedia();
  const existing = recentMedia.find((item) => item.url === url && item.tabId === tabId && item.frameId === frameId && item.documentId === documentId);
  if (existing) {
    if (role === 'manifest' || existing.role === 'unknown') existing.role = role;
    if (playerKey && !existing.playerKey) existing.playerKey = playerKey;
    if (kind !== 'unknown' || !existing.kind) existing.kind = kind;
    existing.at = Date.now();
    return;
  }
  recentMedia.push({ url, tabId, frameId, at: Date.now(), role, kind, documentId, playerKey });
}

// Observe (never block) response traffic that feeds media elements.
chrome.webRequest.onResponseStarted.addListener(
  (details) => {
    if (details.tabId < 0) return;
    const type = details.type;
    if (type !== 'media' && type !== 'xmlhttprequest' && type !== 'other') return;
    const role = roleFor(details.url);
    const kind = mediaKindFor(details.url);
    if (type !== 'media' && !isMediaCandidate({ url: details.url, role, kind })) return;
    rememberMedia(details.url, details.tabId, details.frameId, role, details.documentId, undefined, kind);
  },
  { urls: ['<all_urls>'] },
);

chrome.webRequest.onHeadersReceived.addListener(
  (details) => {
    if (details.tabId < 0) return undefined;
    const type = details.type;
    if (type !== 'media' && type !== 'xmlhttprequest' && type !== 'other') return undefined;
    const contentType = details.responseHeaders?.find((header) => header.name.toLowerCase() === 'content-type')?.value ?? '';
    const role = roleFor(details.url, contentType);
    const kind = mediaKindFor(details.url, contentType);
    if (type !== 'media' && !isMediaCandidate({ url: details.url, role, kind })) return undefined;
    rememberMedia(details.url, details.tabId, details.frameId, role, details.documentId, activePlayerKey(details.tabId, details.frameId, details.documentId), kind);
    return undefined;
  },
  { urls: ['<all_urls>'] },
  ['responseHeaders'],
);

// POST-body observation for form-originated downloads (SPEC §5.1: method/
// body when safely reproducible). Bounded one-shot ring: urlencoded form
// bodies up to 64 KiB, 60 s TTL, consumed on first matching capture.
// Multipart/file uploads, raw bodies, and larger forms are left out — the
// native side replays a safe GET for those, exactly as before. Observe-only:
// no blocking, no modification.
const FORM_BODY_MAX = 64 * 1024;
const FORM_BODY_TTL_MS = 60_000;
const recentFormBodies: Array<{ url: string; body: string; at: number }> = [];

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
    const existing = recentFormBodies.find((item) => item.url === url);
    if (existing) {
      existing.body = body;
      existing.at = Date.now();
      return undefined;
    }
    recentFormBodies.push({ url, body, at: Date.now() });
    return undefined;
  },
  { urls: ['<all_urls>'] },
  ['requestBody'],
);

function takeFormBody(url: string): string | undefined {
  pruneFormBodies();
  const now = Date.now();
  const index = recentFormBodies.findIndex((item) => item.url === url);
  if (index < 0) return undefined;
  const [found] = recentFormBodies.splice(index, 1);
  return now - found.at <= FORM_BODY_TTL_MS ? found.body : undefined;
}

// INTERIM fallback (SPEC §5.1.1): observe-only. Forwards intent so the
// resident app opens an Add Download window, but never cancels the browser
// download — destroying a one-use/tokenized transaction to pretend takeover
// succeeded is worse than a duplicate. `onCreated` exposes only a tentative
// URL basename for redirected downloads; `onDeterminingFilename` supplies the
// header-resolved name while still allowing the browser transaction to proceed.
chrome.downloads.onDeterminingFilename.addListener((item, suggest) => {
  const source = item.finalUrl || item.url || '';
  if (consumeBrowserFallback(item) || consumeBrowserOwnedDownload(item) || item.byExtensionId === chrome.runtime.id || !isHttp(source)) {
    suggest();
    return;
  }
  void (async () => {
    try {
      await policyReady;
      if (ordinaryCaptureError(source, item.referrer ?? '')) {
        return;
      }
      await sendNative({
        type: 'capture-acquisition',
        payload: {
          source,
          name: cleanFilename(item.filename),
          pageUrl: item.referrer,
          referrer: item.referrer,
          // The Downloads API exposes no tab/frame identifier, so do not guess a
          // User-Agent from another document on this fallback path.
          // One-shot POST replay: a form body observed for this URL rides along;
          // absent (or already consumed) means the native side replays a safe GET.
          postBody: takeFormBody(source),
        },
      });
    } finally {
      suggest();
    }
  })();
  return true;
});

chrome.runtime.onMessage.addListener((message, sender, reply) => {
  void (async () => {
    const type = (message as { type?: string })?.type;
    if (type === 'get-policy') {
      await policyReady;
      reply(policyLoadError ? { ok: false, error: policyLoadError, policy } : { ok: true, policy });
    } else if (type === 'update-policy') {
      const patch = (message as { patch?: Partial<BrowserPolicy> }).patch ?? {};
      const previous = policy;
      const next: BrowserPolicy = { ...policy, excludedSites: [...policy.excludedSites] };
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
      reply({ ok: true, policy });
      // Best-effort push so the resident app (when running) stays coherent.
      void sendNative({ type: 'update-policy', payload: policy });
    } else if (type === 'ordinary-capture') {
      const payload = (message as { payload?: Record<string, unknown> }).payload ?? {};
      reply(await captureOrdinary(payload));
    } else if (type === 'browser-owned-download') {
      const payload = (message as { payload?: Record<string, unknown> }).payload ?? {};
      const source = typeof payload.source === 'string' ? payload.source.trim() : '';
      if (isHttp(source)) rememberBrowserOwnedDownload(source, cleanFilename(payload.name));
      reply({ ok: isHttp(source) });
    } else if (type === 'media-player-state') {
      const tabId = sender.tab?.id;
      if (tabId !== undefined) rememberPlayer((message as { payload?: Record<string, unknown> }).payload ?? {}, tabId, sender.frameId ?? 0, sender.documentId);
      reply({ ok: true });
    } else if (type === 'media-capture') {
      await policyReady;
      const payload = (message as { payload?: Record<string, unknown> }).payload ?? {};
      const pageUrl = typeof payload.pageUrl === 'string' ? payload.pageUrl : undefined;
      const policyError = mediaCapturePolicyError(pageUrl ?? '');
      if (policyError) {
        reply({ ok: false, error: policyError });
        return;
      }
      const documentId = sender.documentId;
      const playerKey = typeof payload.playerKey === 'string' ? payload.playerKey : (sender.tab?.id === undefined ? undefined : activePlayerKey(sender.tab.id, sender.frameId ?? 0, documentId));
      let source = typeof payload.source === 'string' ? payload.source : '';
      let selectedSegments: string[] = [];
      if (!isHttp(source) && sender.tab?.id !== undefined) {
        // blob:/MSE player — resolve to the real traffic behind the element.
        const selection = chooseWorkerMediaSelection(recentMedia, recentPlayers, sender.tab.id, sender.frameId ?? 0, playerKey, documentId);
        source = selection?.source ?? '';
        selectedSegments = selection?.selectedSegments ?? [];
      }
      if (!isHttp(source)) {
        reply({ ok: false, error: 'no acquirable source for this media' });
        return;
      }
      const userAgent = cleanUserAgent(payload.userAgent);
      reply(await sendNative({ type: 'media-capture', payload: { ...payload, source, selectedSegments, referrer: pageUrl, userAgent } }));
    } else if (type === 'open-manager') {
      reply(await sendNative({ type: 'open-manager' }));
    } else {
      reply({ ok: false, error: 'unsupported message' });
    }
  })();
  return true;
});

policyReady = loadPolicy();
void policyReady
  .then(() => sendNative({ type: 'get-policy' }))
  .then((response) => {
    const remote = (response as { policy?: Partial<BrowserPolicy> } | undefined)?.policy;
    if (remote && typeof remote.interceptDownloads === 'boolean') {
      policy = {
        interceptDownloads: remote.interceptDownloads,
        showMediaButtons: remote.showMediaButtons ?? policy.showMediaButtons,
        excludedSites: Array.isArray(remote.excludedSites) ? (remote.excludedSites as string[]) : policy.excludedSites,
      };
      void savePolicy();
    }
  })
  .catch(() => undefined);
