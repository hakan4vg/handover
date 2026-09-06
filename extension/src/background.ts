import { DEFAULT_POLICY, NATIVE_HOST, isHttp, type BrowserPolicy } from './shared';
import { chooseMediaSelection, choosePlayerEvidence, roleFor, type MediaCandidate, type MediaPlayerEvidence } from './media-candidates';

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

type PendingBrowserFallback = { source: string; name?: string; at: number };
const pendingBrowserFallbacks: PendingBrowserFallback[] = [];
const BROWSER_FALLBACK_TTL_MS = 30_000;
const BROWSER_FALLBACK_MAX = 20;

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
  } catch {
    policy = { ...DEFAULT_POLICY };
  }
}

async function savePolicy(): Promise<void> {
  try {
    await chrome.storage.local.set({ [POLICY_KEY]: policy });
  } catch {
    // Storage failure must not break acquisition paths.
  }
}

function sendNative(message: unknown): Promise<unknown> {
  return chrome.runtime.sendNativeMessage(NATIVE_HOST, message as object).catch(() => ({ ok: false, error: 'native host unreachable' }));
}

function pruneMedia(now = Date.now()): void {
  while (recentMedia.length && now - recentMedia[0].at > MEDIA_BUFFER_MS) recentMedia.shift();
  while (recentMedia.length > MEDIA_BUFFER_MAX) recentMedia.shift();
}

function prunePlayers(now = Date.now()): void {
  while (recentPlayers.length && now - recentPlayers[0].at > PLAYER_BUFFER_MS) recentPlayers.shift();
  while (recentPlayers.length > PLAYER_BUFFER_MAX) recentPlayers.shift();
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

async function captureOrdinary(payload: Record<string, unknown>): Promise<{ ok: boolean; error?: string }> {
  const source = typeof payload.source === 'string' ? payload.source.trim() : '';
  if (!policy.interceptDownloads || !isHttp(source)) {
    return { ok: false, error: 'ordinary interception disabled or invalid source' };
  }
  const response = (await sendNative({
    type: 'capture-acquisition',
    payload: {
      source,
      name: cleanFilename(payload.name),
      pageUrl: typeof payload.pageUrl === 'string' ? payload.pageUrl : undefined,
      referrer: typeof payload.pageUrl === 'string' ? payload.pageUrl : undefined,
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

function rememberMedia(url: string, tabId: number, frameId: number, role = roleFor(url), documentId?: string, playerKey = activePlayerKey(tabId, frameId, documentId)): void {
  if (!isHttp(url)) return;
  pruneMedia();
  const existing = recentMedia.find((item) => item.url === url && item.tabId === tabId && item.frameId === frameId && item.documentId === documentId);
  if (existing) {
    if (role === 'manifest' || existing.role === 'unknown') existing.role = role;
    if (playerKey && !existing.playerKey) existing.playerKey = playerKey;
    existing.at = Date.now();
    return;
  }
  recentMedia.push({ url, tabId, frameId, at: Date.now(), role, documentId, playerKey });
}

// Observe (never block) response traffic that feeds media elements.
chrome.webRequest.onResponseStarted.addListener(
  (details) => {
    if (details.tabId < 0) return;
    const type = details.type;
    if (type !== 'media' && type !== 'xmlhttprequest' && type !== 'other') return;
    rememberMedia(details.url, details.tabId, details.frameId, undefined, details.documentId);
  },
  { urls: ['<all_urls>'] },
);

chrome.webRequest.onHeadersReceived.addListener(
  (details) => {
    if (details.tabId < 0) return undefined;
    const contentType = details.responseHeaders?.find((header) => header.name.toLowerCase() === 'content-type')?.value ?? '';
    const role = roleFor(details.url, contentType);
    if (role === 'manifest') rememberMedia(details.url, details.tabId, details.frameId, role, details.documentId);
    return undefined;
  },
  { urls: ['<all_urls>'] },
  ['responseHeaders'],
);

// INTERIM fallback (SPEC §5.1.1): observe-only. Forwards intent so the
// resident app opens an Add Download window, but never cancels the browser
// download — destroying a one-use/tokenized transaction to pretend takeover
// succeeded is worse than a duplicate. `onCreated` exposes only a tentative
// URL basename for redirected downloads; `onDeterminingFilename` supplies the
// header-resolved name while still allowing the browser transaction to proceed.
chrome.downloads.onDeterminingFilename.addListener((item, suggest) => {
  if (!policy.interceptDownloads || consumeBrowserFallback(item) || item.byExtensionId === chrome.runtime.id || !item.url || !isHttp(item.url)) {
    suggest();
    return;
  }
  void sendNative({
    type: 'capture-acquisition',
    payload: {
      source: item.finalUrl || item.url,
      name: cleanFilename(item.filename),
      pageUrl: item.referrer,
      referrer: item.referrer,
    },
  }).finally(() => suggest());
  return true;
});

chrome.runtime.onMessage.addListener((message, sender, reply) => {
  void (async () => {
    const type = (message as { type?: string })?.type;
    if (type === 'get-policy') {
      reply({ ok: true, policy });
    } else if (type === 'update-policy') {
      const patch = (message as { patch?: Partial<BrowserPolicy> }).patch ?? {};
      if (typeof patch.interceptDownloads === 'boolean') policy.interceptDownloads = patch.interceptDownloads;
      if (typeof patch.showMediaButtons === 'boolean') policy.showMediaButtons = patch.showMediaButtons;
      if (Array.isArray(patch.excludedSites)) {
        policy.excludedSites = patch.excludedSites.filter((site): site is string => typeof site === 'string');
      }
      await savePolicy();
      reply({ ok: true, policy });
      // Best-effort push so the resident app (when running) stays coherent.
      void sendNative({ type: 'update-policy', payload: policy });
    } else if (type === 'ordinary-capture') {
      const payload = (message as { payload?: Record<string, unknown> }).payload ?? {};
      reply(await captureOrdinary(payload));
    } else if (type === 'media-player-state') {
      const tabId = sender.tab?.id;
      if (tabId !== undefined) rememberPlayer((message as { payload?: Record<string, unknown> }).payload ?? {}, tabId, sender.frameId ?? 0, sender.documentId);
      reply({ ok: true });
    } else if (type === 'media-capture') {
      const payload = (message as { payload?: Record<string, unknown> }).payload ?? {};
      const documentId = sender.documentId;
      const playerKey = typeof payload.playerKey === 'string' ? payload.playerKey : (sender.tab?.id === undefined ? undefined : activePlayerKey(sender.tab.id, sender.frameId ?? 0, documentId));
      let source = typeof payload.source === 'string' ? payload.source : '';
      let selectedSegments: string[] = [];
      if (!isHttp(source) && sender.tab?.id !== undefined) {
        // blob:/MSE player — resolve to the real traffic behind the element.
        const selection = chooseMediaSelection(recentMedia, sender.tab.id, sender.frameId ?? 0, playerKey, documentId);
        source = selection?.source ?? '';
        selectedSegments = selection?.selectedSegments ?? [];
      }
      if (!isHttp(source)) {
        reply({ ok: false, error: 'no acquirable source for this media' });
        return;
      }
      const pageUrl = typeof payload.pageUrl === 'string' ? payload.pageUrl : undefined;
      reply(await sendNative({ type: 'media-capture', payload: { ...payload, source, selectedSegments, referrer: pageUrl } }));
    } else if (type === 'open-manager') {
      reply(await sendNative({ type: 'open-manager' }));
    } else {
      reply({ ok: false, error: 'unsupported message' });
    }
  })();
  return true;
});

void loadPolicy()
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
