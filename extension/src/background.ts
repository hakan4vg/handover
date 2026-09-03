import { DEFAULT_POLICY, NATIVE_HOST, isHttp, siteOf, type BrowserPolicy } from './shared';

const POLICY_KEY = 'dm-policy';

interface MediaCandidate {
  url: string;
  tabId: number;
  frameId: number;
  at: number;
}

// Bounded ring of recent media-ish traffic per tab. M0 proof vehicle for the
// generic current-media mechanism (SPEC §6): content scripts report the
// element the user interacts with, this buffer supplies the real network
// source behind blob:/MSE players. URLs only, no bodies, no cookies.
const recentMedia: MediaCandidate[] = [];
const MEDIA_BUFFER_MAX = 60;
const MEDIA_BUFFER_MS = 90_000;

let policy: BrowserPolicy = { ...DEFAULT_POLICY };

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

function rememberMedia(url: string, tabId: number, frameId: number): void {
  if (!isHttp(url)) return;
  pruneMedia();
  if (recentMedia.some((item) => item.url === url && item.tabId === tabId)) return;
  recentMedia.push({ url, tabId, frameId, at: Date.now() });
}

function candidateFor(tabId: number, frameId: number): string | undefined {
  pruneMedia();
  for (let index = recentMedia.length - 1; index >= 0; index--) {
    const item = recentMedia[index];
    if (item.tabId === tabId && (item.frameId === frameId || item.frameId === 0)) return item.url;
  }
  for (let index = recentMedia.length - 1; index >= 0; index--) {
    if (recentMedia[index].tabId === tabId) return recentMedia[index].url;
  }
  return undefined;
}

// Observe (never block) response traffic that feeds media elements.
chrome.webRequest.onResponseStarted.addListener(
  (details) => {
    if (details.tabId < 0) return;
    const type = details.type;
    if (type !== 'media' && type !== 'xmlhttprequest' && type !== 'other') return;
    rememberMedia(details.url, details.tabId, details.frameId);
  },
  { urls: ['<all_urls>'] },
);

// INTERIM fallback (SPEC §5.1.1): observe-only. Forwards intent so the
// resident app opens an Add Download window, but never cancels the browser
// download — destroying a one-use/tokenized transaction to pretend takeover
// succeeded is worse than a duplicate. Replaced by the pre-browser M0 proof.
chrome.downloads.onCreated.addListener((item) => {
  if (!policy.interceptDownloads || item.byExtensionId === chrome.runtime.id) return;
  if (!item.url || !isHttp(item.url)) return;
  void sendNative({
    type: 'capture-acquisition',
    payload: {
      source: item.finalUrl || item.url,
      name: item.filename?.split('/').pop()?.split('\\').pop(),
    },
  });
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
    } else if (type === 'media-capture') {
      const payload = (message as { payload?: Record<string, unknown> }).payload ?? {};
      let source = typeof payload.source === 'string' ? payload.source : '';
      if (!isHttp(source) && sender.tab?.id !== undefined) {
        // blob:/MSE player — resolve to the real traffic behind the element.
        source = candidateFor(sender.tab.id, sender.frameId ?? 0) ?? '';
      }
      if (!isHttp(source)) {
        reply({ ok: false, error: 'no acquirable source for this media' });
        return;
      }
      reply(await sendNative({ type: 'media-capture', payload: { ...payload, source } }));
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
