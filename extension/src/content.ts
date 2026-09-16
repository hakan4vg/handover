import { captureNeedsBrowserRestore, restoreBrowserDownload } from './download-fallback';
import type { MediaEvidence } from './media-candidates';

// Local copies (not imported): MV3 content scripts must be classic scripts,
// so they cannot share an ES module chunk with the service worker.
import type { BrowserPolicy, MediaFilterSettings } from './shared';

function siteOf(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, '').toLowerCase();
  } catch {
    return '';
  }
}

function siteOfDocument(url: string, referrer: string, ancestorOrigin = ''): string {
  return siteOf(url) || siteOf(referrer) || siteOf(ancestorOrigin);
}

function isHttp(url: string): boolean {
  try {
    const scheme = new URL(url).protocol;
    return scheme === 'http:' || scheme === 'https:';
  } catch {
    return false;
  }
}

function mediaSourceFromValues(
  currentSrc: string | null | undefined,
  elementSrc: string | null | undefined,
  childSrc: string | null | undefined,
  baseUrl: string,
): string {
  const candidates = [currentSrc, elementSrc, childSrc]
    .map((value) => value?.trim() || '')
    .filter(Boolean)
    .map((raw) => {
      try {
        return new URL(raw, baseUrl).href;
      } catch {
        return raw;
      }
    });
  return candidates.find((candidate) => isHttp(candidate)) || candidates[0] || '';
}

const BUTTON_ID = 'dm-media-download-button';
const BUTTON_STYLE_ID = 'dm-media-download-style';
const MIN_SIZE = 120;
const DEFAULT_MEDIA_FILTERS: MediaFilterSettings = { minimumSizeBytes: 0, excludedFileTypes: ['gif'] };
const MEDIA_FILTER_MIME_TYPES: Record<string, string> = {
  'audio/aac': 'aac',
  'audio/flac': 'flac',
  'audio/mpeg': 'mp3',
  'audio/mp4': 'm4a',
  'audio/ogg': 'ogg',
  'audio/wav': 'wav',
  'audio/webm': 'webm',
  'image/gif': 'gif',
  'image/webp': 'webp',
  'video/mp4': 'mp4',
  'video/ogg': 'ogv',
  'video/quicktime': 'mov',
  'video/webm': 'webm',
  'video/x-m4v': 'm4v',
};

type MediaElement = HTMLVideoElement | HTMLAudioElement;

let policy: BrowserPolicy | null = null;
let mediaFilters: MediaFilterSettings = { ...DEFAULT_MEDIA_FILTERS, excludedFileTypes: [...DEFAULT_MEDIA_FILTERS.excludedFileTypes] };
let mediaFilterGeneration = 0;
const mediaFilterCache = new Map<string, { allowed: boolean; expires: number }>();
let current: HTMLVideoElement | HTMLAudioElement | null = null;
let button: HTMLButtonElement | null = null;
let frame: number | null = null;
let nextPlayerKey = 1;
let captureInFlight = false;
let lastPointerX: number | null = null;
let lastPointerY: number | null = null;
let pointerRAF: number | null = null;
let buttonStyle: HTMLStyleElement | null = null;
const playerKeys = new WeakMap<HTMLMediaElement, string>();
const mediaIdentities = new WeakMap<HTMLMediaElement, { source: string; identity: string }>();
const mediaFilterKeys = new WeakMap<HTMLMediaElement, string>();
const observedPlayers = new WeakSet<HTMLMediaElement>();
const lastPlayerReports = new WeakMap<HTMLMediaElement, number>();
const lastReportedState = new WeakMap<HTMLMediaElement, string>();

const PAGE_MEDIA_MARKER = 'download-manager-media-v1';
const PAGE_MEDIA_QUERY = 'dm-media-evidence-query';
const PAGE_MEDIA_RESPONSE = 'dm-media-evidence-response';
const PAGE_MEDIA_MAX_URL = 4096;
const pendingPageEvidence = new Map<string, { resolve: (value: MediaEvidence | undefined) => void; timer: number }>();
let nextPageEvidenceRequest = 1;
let nextMediaIdentity = 1;

function pageMediaUrl(value: unknown): string | undefined {
  if (typeof value !== 'string' || !value || value.length > PAGE_MEDIA_MAX_URL) return undefined;
  try {
    const parsed = new URL(value, document.baseURI);
    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:' && parsed.protocol !== 'blob:') return undefined;
    if (parsed.protocol === 'blob:' && parsed.origin !== location.origin) return undefined;
    parsed.hash = '';
    return parsed.href;
  } catch {
    return undefined;
  }
}

function pageEvidenceFromValue(value: unknown, expectedCurrentSrc?: string): MediaEvidence | undefined {
  if (!value || typeof value !== 'object') return undefined;
  const item = value as Partial<MediaEvidence>;
  const currentSrc = pageMediaUrl(item.currentSrc);
  const sourceIdentity = typeof item.sourceIdentity === 'string' && item.sourceIdentity.length <= 256 ? item.sourceIdentity : '';
  const source = item.source === undefined ? undefined : pageMediaUrl(item.source);
  const playerKind = item.playerKind === 'audio' || item.playerKind === 'video' ? item.playerKind : undefined;
  const companionAudio = item.companionAudio === undefined ? undefined : pageMediaUrl(item.companionAudio);
  const selectedValues = Array.isArray(item.selectedSegments) ? item.selectedSegments : [];
  const selectedSegments = selectedValues.map((candidate) => pageMediaUrl(candidate)).filter((candidate): candidate is string => !!candidate);
  if (!currentSrc || !sourceIdentity || selectedValues.length > 8 || selectedSegments.length !== selectedValues.length || (expectedCurrentSrc && currentSrc !== expectedCurrentSrc) || (source !== undefined && currentSrc.startsWith('http') && source !== currentSrc) || (companionAudio !== undefined && playerKind !== 'video')) return undefined;
  return { currentSrc, sourceIdentity, ...(source ? { source } : {}), ...(playerKind ? { playerKind } : {}), ...(companionAudio ? { companionAudio } : {}), selectedSegments };
}

window.addEventListener('message', (event) => {
  if (event.source !== window || event.origin !== location.origin || !event.data || typeof event.data !== 'object') return;
  const data = event.data as Record<string, unknown>;
  if (data.marker !== PAGE_MEDIA_MARKER) return;
  if (data.type !== PAGE_MEDIA_RESPONSE || typeof data.requestId !== 'string') return;
  const pending = pendingPageEvidence.get(data.requestId);
  if (!pending) return;
  pendingPageEvidence.delete(data.requestId);
  window.clearTimeout(pending.timer);
  pending.resolve(pageEvidenceFromValue(data.evidence));
});

function requestPageEvidence(currentSrc: string, playerKind: 'audio' | 'video'): Promise<MediaEvidence | undefined> {
  const requestId = `evidence-${nextPageEvidenceRequest++}`;
  return new Promise((resolve) => {
    const timer = window.setTimeout(() => {
      pendingPageEvidence.delete(requestId);
      resolve(undefined);
    }, 200);
    pendingPageEvidence.set(requestId, { resolve, timer });
    window.postMessage({ marker: PAGE_MEDIA_MARKER, type: PAGE_MEDIA_QUERY, requestId, currentSrc, playerKind }, location.origin);
  });
}

function cleanFilename(value: string | null | undefined): string | undefined {
  const leaf = value?.trim().split('/').pop()?.split('\\').pop()?.trim();
  return leaf || undefined;
}

function basenameFromUrl(url: string): string | undefined {
  try {
    return cleanFilename(new URL(url).pathname);
  } catch {
    return undefined;
  }
}

// Explicit <a download> clicks are the generic pre-browser action Chromium
// exposes safely. Capture in the document's capture phase, before page
// handlers/default navigation can consume a one-use URL. Other browser-owned
// downloads retain the observe-only downloads.onCreated fallback.
function policySite(): string {
  return siteOfDocument(window.location.href, document.referrer, window.location.ancestorOrigins?.item(0) ?? '');
}

function siteAllowed(): boolean {
  if (!policy) return false;
  const site = policySite();
  return !!site && !policy.excludedSites.includes(site);
}

function rememberBrowserOwnedClick(event: MouseEvent): void {
  if (event.defaultPrevented || event.button !== 0 || !(event.metaKey || event.ctrlKey || event.shiftKey || event.altKey)) return;
  if (!policy?.interceptDownloads || !siteAllowed()) return;
  const target = event.target;
  if (!(target instanceof Element)) return;
  const anchor = target.closest('a');
  if (!(anchor instanceof HTMLAnchorElement) || !anchor.hasAttribute('download') || anchor.hasAttribute('data-dm-browser-fallback')) return;
  const source = anchor.href;
  if (!isHttp(source)) return;
  let name: string | undefined;
  try {
    name = new URL(source).origin === new URL(window.location.href).origin
      ? cleanFilename(anchor.getAttribute('download'))
      : basenameFromUrl(source);
  } catch {
    name = basenameFromUrl(source);
  }
  void chrome.runtime.sendMessage({ type: 'browser-owned-download', payload: { source, name } });
}

function interceptDownloadClick(event: MouseEvent): void {
  if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  if (policy && (!policy.interceptDownloads || !siteAllowed())) return;
  const target = event.target;
  if (!(target instanceof Element)) return;
  const anchor = target.closest('a');
  if (!(anchor instanceof HTMLAnchorElement) || !anchor.hasAttribute('download') || anchor.hasAttribute('data-dm-browser-fallback')) return;
  const source = anchor.href;
  if (!isHttp(source)) return;
  event.preventDefault();
  event.stopImmediatePropagation();
  // Chromium drops the author-supplied filename for cross-origin targets
  // and saves under the server basename instead. Mirror that: honor the
  // download attribute only when the target is same-origin with the page.
  let authorName: string | undefined;
  try {
    authorName = new URL(source).origin === new URL(window.location.href).origin
      ? cleanFilename(anchor.getAttribute('download'))
      : undefined;
  } catch {
    authorName = undefined;
  }
  const name = authorName ?? basenameFromUrl(source);
  void chrome.runtime.sendMessage({
    type: 'ordinary-capture',
    payload: {
      source,
      name,
      pageUrl: window.location.href,
      userAgent: navigator.userAgent,
    },
  }).then((response) => {
    // Background answers ok:false only after its own downloads-API fallback
    // failed; the synthetic anchor is the last resort, not a duplicate.
    if (captureNeedsBrowserRestore(response)) restoreBrowserDownload(source, name);
  }).catch(() => {
    restoreBrowserDownload(source, name);
  });
}

let policyTimer: number | null = null;

function applyMediaFilters(value: unknown): void {
  if (!value || typeof value !== 'object') return;
  const record = value as Partial<MediaFilterSettings>;
  const minimumSizeBytes = typeof record.minimumSizeBytes === 'number' && Number.isFinite(record.minimumSizeBytes) && record.minimumSizeBytes >= 0
    ? Math.min(Math.floor(record.minimumSizeBytes), Number.MAX_SAFE_INTEGER)
    : mediaFilters.minimumSizeBytes;
  const excludedFileTypes = Array.isArray(record.excludedFileTypes)
    ? [...new Set(record.excludedFileTypes.filter((item): item is string => typeof item === 'string').map((item) => {
      const token = item.trim().toLowerCase();
      return token.includes('/') ? mediaFileTypeForLocal('', token) : token.replace(/^\./, '');
    }).filter((item): item is string => !!item && /^[a-z0-9]{1,12}$/.test(item)))]
    : mediaFilters.excludedFileTypes;
  const changed = minimumSizeBytes !== mediaFilters.minimumSizeBytes
    || excludedFileTypes.length !== mediaFilters.excludedFileTypes.length
    || excludedFileTypes.some((item, index) => item !== mediaFilters.excludedFileTypes[index]);
  if (!changed) return;
  mediaFilters = { minimumSizeBytes, excludedFileTypes };
  mediaFilterGeneration += 1;
  mediaFilterCache.clear();
}

async function refreshPolicy(): Promise<void> {
  try {
    const response = (await chrome.runtime.sendMessage({ type: 'get-policy', includeMediaFilters: true })) as {
      ok?: boolean;
      policy?: BrowserPolicy;
      mediaFilters?: MediaFilterSettings;
    };
    if (response?.policy) {
      policy = response.policy;
      applyMediaFilters(response.mediaFilters);
      // Back off once the worker answers; it pushes nothing on its own.
      if (policyTimer !== null) {
        window.clearInterval(policyTimer);
        policyTimer = null;
      }
      return;
    }
  } catch {
    policy = null;
  }
  // The service worker may still be waking (install/first message); retry
  // fast until the first answer instead of assuming the first attempt works.
  if (policyTimer === null) policyTimer = window.setInterval(refreshPolicy, 1000);
}

function active(): boolean {
  return !!policy?.showMediaButtons && siteAllowed();
}

function visible(el: HTMLMediaElement): boolean {
  const rect = anchorRect(el);
  return (
    rect.width >= MIN_SIZE &&
    rect.height >= MIN_SIZE / 3 &&
    rect.bottom > 0 &&
    rect.right > 0 &&
    rect.top < window.innerHeight &&
    rect.left < window.innerWidth
  );
}

function anchorRect(el: HTMLMediaElement): DOMRect {
  const own = el.getBoundingClientRect();
  if (own.width > 0 && own.height > 0) return own;
  // Custom audio players commonly hide the native <audio> box while keeping
  // their rendered controls in a visible wrapper. Keep the generic policy
  // narrow: only a playing, enabled audio element may borrow a non-root
  // ancestor's visible rectangle. Hidden/paused media remains ineligible.
  if (!(el instanceof HTMLAudioElement) || el.paused || el.ended) return own;
  let parent = el.parentElement;
  for (let depth = 0; parent && depth < 5; depth += 1, parent = parent.parentElement) {
    if (parent === document.body || parent === document.documentElement) continue;
    const style = getComputedStyle(parent);
    const rect = parent.getBoundingClientRect();
    if (
      style.display !== 'none' &&
      style.visibility !== 'hidden' &&
      rect.width >= MIN_SIZE &&
      rect.height >= MIN_SIZE / 3 &&
      rect.bottom > 0 &&
      rect.right > 0 &&
      rect.top < window.innerHeight &&
      rect.left < window.innerWidth
    ) return rect;
  }
  return own;
}

function keyFor(el: HTMLMediaElement): string {
  const existing = playerKeys.get(el);
  if (existing) return existing;
  const key = `player-${nextPlayerKey++}`;
  playerKeys.set(el, key);
  return key;
}

function currentSrcFor(el: HTMLMediaElement): string {
  const raw = el.currentSrc || el.getAttribute('src') || el.querySelector('source[src]')?.getAttribute('src') || '';
  if (!raw.trim()) return '';
  try {
    return new URL(raw, document.baseURI).href;
  } catch {
    return raw.trim();
  }
}

function mediaIdentityFor(el: HTMLMediaElement, source = currentSrcFor(el)): string {
  const existing = mediaIdentities.get(el);
  if (existing?.source === source) return existing.identity;
  const identity = `media-${nextMediaIdentity++}:${source}`;
  mediaIdentities.set(el, { source, identity });
  return identity;
}

function sourceFor(el: HTMLMediaElement): string {
  const child = el.querySelector('source[src]')?.getAttribute('src');
  return mediaSourceFromValues(el.currentSrc, el.src, child, document.baseURI);
}

function mediaFileTypeForLocal(url: string, contentType = ''): string | undefined {
  const mime = contentType.toLowerCase().split(';', 1)[0].trim();
  if (MEDIA_FILTER_MIME_TYPES[mime]) return MEDIA_FILTER_MIME_TYPES[mime];
  if (mime.startsWith('audio/') || mime.startsWith('video/') || mime.startsWith('image/')) {
    const subtype = mime.slice(mime.indexOf('/') + 1).replace(/^x-/, '').split('+', 1)[0].trim();
    if (subtype) return subtype === 'mpeg' ? 'mp3' : subtype;
  }
  try {
    const parsed = new URL(url, document.baseURI);
    const queryMime = (parsed.searchParams.get('mime') || '').toLowerCase().split(';', 1)[0].trim();
    if (MEDIA_FILTER_MIME_TYPES[queryMime]) return MEDIA_FILTER_MIME_TYPES[queryMime];
    const extension = parsed.pathname.toLowerCase().match(/\.([a-z0-9]{1,12})$/)?.[1];
    return extension || undefined;
  } catch {
    return undefined;
  }
}

function sourceTypeHint(el: HTMLMediaElement, source: string): string {
  const sourceElement = [...el.querySelectorAll('source')].find((item) => item.src === source || item.getAttribute('src') === source);
  return el.getAttribute('type') || sourceElement?.getAttribute('type') || '';
}

function filterCacheKey(el: HTMLMediaElement, source: string): string {
  const identity = mediaIdentityFor(el, currentSrcFor(el) || source);
  const key = `${identity}:${mediaFilterGeneration}`;
  const previous = mediaFilterKeys.get(el);
  if (previous && previous !== key) mediaFilterCache.delete(previous);
  mediaFilterKeys.set(el, key);
  return key;
}

function requestMediaFilter(el: HTMLMediaElement, source: string, key: string): void {
  const cached = mediaFilterCache.get(key);
  if (cached && cached.expires > Date.now()) return;
  if (mediaFilterCache.size >= 128 && !mediaFilterCache.has(key)) {
    const oldest = mediaFilterCache.keys().next().value;
    if (oldest) mediaFilterCache.delete(oldest);
  }
  mediaFilterCache.set(key, { allowed: cached?.allowed ?? true, expires: Infinity });
  const generation = mediaFilterGeneration;
  void (async () => {
    let allowed = true;
    let expires = Date.now() + 3000;
    try {
      const playerKind = el instanceof HTMLAudioElement ? 'audio' : 'video';
      const evidence = isHttp(source) ? undefined : await requestPageEvidence(source, playerKind);
      const resolved = isHttp(source) ? source : evidence?.currentSrc === source ? evidence.source : undefined;
      if (resolved && isHttp(resolved)) {
        const response = await chrome.runtime.sendMessage({
          type: 'check-media-filters',
          payload: { source: resolved, companionAudio: evidence?.companionAudio, playerKind },
        }) as { ok?: boolean; allowed?: boolean; totalBytes?: number; reason?: string } | undefined;
        allowed = response?.allowed !== false;
        if (isHttp(source) && response?.ok && (response.totalBytes !== undefined || response.reason === 'excluded-type')) expires = Infinity;
      }
    } catch {
      // Retry unknown metadata without blocking playback or source requests.
    }
    if (generation !== mediaFilterGeneration || mediaFilterKeys.get(el) !== key) return;
    mediaFilterCache.set(key, { allowed, expires });
    if (allowed !== (cached?.allowed ?? true)) track();
  })();
}

function mediaFilterAllowed(el: HTMLMediaElement, source: string): boolean {
  const localType = mediaFileTypeForLocal(source, sourceTypeHint(el, source));
  if (localType && mediaFilters.excludedFileTypes.includes(localType)) return false;
  const key = filterCacheKey(el, source);
  requestMediaFilter(el, source, key);
  return mediaFilterCache.get(key)?.allowed !== false;
}

function usable(el: HTMLMediaElement): boolean {
  const source = sourceFor(el);
  return !el.hasAttribute('disabled') && !!source && mediaFilterAllowed(el, source);
}

// Media inside web-component players (Media Chrome / mux-video and similar)
// lives in open shadow roots; document.querySelectorAll('video, audio') cannot
// see it. Keep light and shadow results incrementally fresh, and pierce open
// shadow roots at most once per second to bound cost on heavy pages. Closed
// shadow roots stay invisible.
let lastShadowScan = 0;
let lightMediaCache: MediaElement[] | null = null;
let shadowMediaCache: MediaElement[] = [];
let mediaCache: MediaElement[] | null = null;
let mediaObserver: MutationObserver | null = null;

const mediaMutationOptions: MutationObserverInit = {
  childList: true,
  subtree: true,
  attributes: true,
  attributeFilter: ['src', 'disabled', 'type'],
};

function mediaInNode(node: Node): MediaElement[] {
  const found: MediaElement[] = [];
  if (node instanceof HTMLVideoElement || node instanceof HTMLAudioElement) found.push(node);
  if (node instanceof Element || node instanceof DocumentFragment || node instanceof Document) {
    node.querySelectorAll('video, audio').forEach((media) => {
      if (media instanceof HTMLVideoElement || media instanceof HTMLAudioElement) found.push(media);
    });
  }
  return found;
}

function appendMedia(cache: MediaElement[], values: MediaElement[]): void {
  const known = new Set(cache);
  values.forEach((media) => {
    if (!known.has(media)) {
      known.add(media);
      cache.push(media);
    }
  });
}

function mediaRootIsConnected(media: MediaElement, root: Document | ShadowRoot): boolean {
  return media.isConnected && media.getRootNode() === root;
}

function rebuildMediaCache(): MediaElement[] {
  const light = lightMediaCache ?? [];
  shadowMediaCache = shadowMediaCache.filter((media) => {
    const root = media.getRootNode();
    return typeof ShadowRoot !== 'undefined' && root instanceof ShadowRoot && mediaRootIsConnected(media, root);
  });
  mediaCache = [...light, ...shadowMediaCache];
  return mediaCache;
}

function updateMediaCache(records: MutationRecord[]): void {
  if (lightMediaCache === null) return;
  let lightChanged = false;
  let shadowChanged = false;
  for (const record of records) {
    if (record.type !== 'childList') continue;
    const root = record.target.getRootNode();
    const isShadow = typeof ShadowRoot !== 'undefined' && root instanceof ShadowRoot;
    const cache = isShadow ? shadowMediaCache : lightMediaCache;
    record.addedNodes.forEach((node) => appendMedia(cache, mediaInNode(node)));
    if (isShadow) shadowChanged = true;
    else lightChanged = true;
  }
  if (lightChanged) {
    lightMediaCache = lightMediaCache.filter((media) => mediaRootIsConnected(media, document));
  }
  if (shadowChanged || lightChanged) rebuildMediaCache();
}

function scanShadowMedia(): void {
  if (typeof ShadowRoot === 'undefined') return;
  const found: MediaElement[] = [];
  const known = new Set<MediaElement>();
  const scan = (root: ParentNode): void => {
    root.querySelectorAll('*').forEach((el) => {
      const shadow = (el as HTMLElement).shadowRoot;
      if (!shadow) return;
      mediaObserver?.observe(shadow, mediaMutationOptions);
      shadow.querySelectorAll('video, audio').forEach((media) => {
        if ((media instanceof HTMLVideoElement || media instanceof HTMLAudioElement) && !known.has(media)) {
          known.add(media);
          found.push(media);
        }
      });
      scan(shadow);
    });
  };
  scan(document);
  shadowMediaCache = found;
  lastShadowScan = performance.now();
  rebuildMediaCache();
}

function collectMedia(scanShadow = true): MediaElement[] {
  if (lightMediaCache === null) lightMediaCache = Array.from(document.querySelectorAll('video, audio')) as MediaElement[];
  if (!active()) {
    mediaCache = null;
    return lightMediaCache;
  }
  const now = performance.now();
  if (scanShadow && (mediaCache === null || now - lastShadowScan >= 1000)) scanShadowMedia();
  return mediaCache ?? rebuildMediaCache();
}

function isPointerOver(el: HTMLMediaElement, x: number | null, y: number | null): boolean {
  if (x === null || y === null) return el.matches(':hover');
  const rect = anchorRect(el);
  if (x >= rect.left && x <= rect.right && y >= rect.top && y <= rect.bottom) {
    return true;
  }
  try {
    const container = el.closest?.(
      '.html5-video-player, [data-testid*="video"], [class*="player"], figure, .video-js, .plyr'
    ) || el.parentElement;
    if (container && container !== document.body && container !== document.documentElement) {
      const cRect = container.getBoundingClientRect();
      if (
        cRect.width >= MIN_SIZE &&
        cRect.height >= MIN_SIZE / 3 &&
        x >= cRect.left &&
        x <= cRect.right &&
        y >= cRect.top &&
        y <= cRect.bottom
      ) {
        return true;
      }
    }
  } catch {
    // Ignore invalid selector queries
  }
  return el.matches(':hover');
}

function reportPlayer(el: HTMLMediaElement, force = false): void {
  if (!active()) return;
  const now = Date.now();
  const source = sourceFor(el);
  const currentSrc = currentSrcFor(el);
  const hovered = isPointerOver(el, lastPointerX, lastPointerY);
  const playing = !el.paused && !el.ended;
  const isVis = visible(el);
  const stateKey = `${source}|${el === current}|${hovered}|${playing}|${isVis}`;
  if (!force && lastReportedState.get(el) === stateKey && now - (lastPlayerReports.get(el) ?? 0) < 2000) return;
  lastReportedState.set(el, stateKey);
  lastPlayerReports.set(el, now);
  void chrome.runtime.sendMessage({
    type: 'media-player-state',
    payload: {
      playerKey: keyFor(el),
      source: isHttp(source) ? source : '',
      currentSrc,
      mediaIdentity: mediaIdentityFor(el, currentSrc),
      active: el === current,
      hovered,
      playing,
      visible: isVis,
    },
  }).catch(() => undefined);
}

function observePlayer(el: HTMLMediaElement): void {
  keyFor(el);
  if (observedPlayers.has(el)) return;
  observedPlayers.add(el);
  const update = () => {
    track();
    reportPlayer(el, true);
  };
  for (const event of ['play', 'playing', 'pause', 'ended', 'loadedmetadata', 'emptied', 'mouseenter', 'mouseleave']) el.addEventListener(event, update, { passive: true });
}

function isPointerOverButton(btn: HTMLButtonElement, x: number | null, y: number | null): boolean {
  if (x === null || y === null) return btn.matches(':hover');
  const rect = btn.getBoundingClientRect();
  return x >= rect.left && x <= rect.right && y >= rect.top && y <= rect.bottom;
}

function pick(): HTMLVideoElement | HTMLAudioElement | null {
  // Hovering the button itself must keep the current player: the button is a
  // separate fixed element, so pointer on the button must not dismiss it.
  if (current?.isConnected && button?.isConnected && (document.activeElement === button || isPointerOverButton(button, lastPointerX, lastPointerY))) {
    return current;
  }
  const media = collectMedia(false);
  // 1. Hovered media (playing OR paused)
  for (const el of media) {
    const item = el as HTMLVideoElement | HTMLAudioElement;
    if (isPointerOver(item, lastPointerX, lastPointerY) && visible(item) && usable(item)) return item;
  }
  // 2. Best playing media
  let best: HTMLVideoElement | HTMLAudioElement | null = null;
  let bestArea = 0;
  media.forEach((el) => {
    const media = el as HTMLVideoElement | HTMLAudioElement;
    if (media.paused || media.ended || !visible(media) || !usable(media)) return;
    const rect = anchorRect(media);
    const area = rect.width * rect.height;
    if (area > bestArea) {
      bestArea = area;
      best = media;
    }
  });
  return best;
}

function ensureButton(): HTMLButtonElement {
  if (!buttonStyle?.isConnected) {
    const existing = document.getElementById(BUTTON_STYLE_ID);
    buttonStyle = existing instanceof HTMLStyleElement ? existing : document.createElement('style');
    buttonStyle.id = BUTTON_STYLE_ID;
    buttonStyle.textContent = `
#${BUTTON_ID}{all:initial;box-sizing:border-box;position:fixed;z-index:2147483647;display:inline-flex;align-items:center;justify-content:center;height:26px;min-width:26px;padding:5px;border:1px solid rgba(255,255,255,.18);border-radius:7px;background:rgba(20,24,30,.32);color:#fff;font:500 11px/1.2 system-ui,sans-serif;cursor:pointer;opacity:.38;backdrop-filter:blur(4px);transition:opacity .16s,background .16s;overflow:hidden}
#${BUTTON_ID} svg{display:block;flex:0 0 14px;width:14px;height:14px;pointer-events:none}
#${BUTTON_ID}::after{content:attr(data-label);display:block;max-width:0;margin-left:0;opacity:0;white-space:nowrap;overflow:hidden;transition:max-width .16s,margin-left .16s,opacity .16s}
#${BUTTON_ID}:hover,#${BUTTON_ID}:focus-visible{opacity:1;background:rgba(20,24,30,.82)}
#${BUTTON_ID}:hover::after,#${BUTTON_ID}:focus-visible::after{max-width:96px;margin-left:6px;opacity:1}
#${BUTTON_ID}:focus-visible{outline:2px solid #fff;outline-offset:2px}
#${BUTTON_ID}:disabled{cursor:wait}
@media(prefers-reduced-motion:reduce){#${BUTTON_ID},#${BUTTON_ID}::after{transition:none}}
`;
    document.documentElement.appendChild(buttonStyle);
  }
  if (button?.isConnected) return button;
  button = document.createElement('button');
  button.id = BUTTON_ID;
  button.type = 'button';
  button.dataset.label = 'Download';
  button.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3v12m-4-4 4 4 4-4M5 17v4h14v-4"/></svg>';
  button.setAttribute('aria-label', 'Download this media');
  button.addEventListener('click', (event) => {
    event.stopPropagation();
    event.preventDefault();
    void capture();
  });
  document.documentElement.appendChild(button);
  return button;
}

let loopCount = 0;

function positionButton(): boolean {
  if (!current || !active() || !current.isConnected || !visible(current) || !usable(current)) {
    button?.remove();
    button = null;
    return false;
  }
  const el = ensureButton();
  const rect = anchorRect(current);
  el.style.top = `${Math.max(6, rect.top + 6)}px`;
  el.style.left = `${Math.max(32, Math.min(document.documentElement.clientWidth - 6, rect.right - 6))}px`;
  el.style.transform = 'translateX(-100%)';
  return true;
}

function loop(): void {
  frame = null;
  loopCount++;
  // rAF gives smooth following during scroll/resize, but it can be throttled
  // in backgrounded pages — track() positions directly too, so the control
  // never depends on rAF alone to exist.
  if (!positionButton()) {
    current = null;
    return;
  }
  frame = requestAnimationFrame(loop);
}

function track(): void {
  // Discipline: every DOM reaction below must be idempotent (guarded appends,
  // same-value style writes). track() runs on a timer AND on a whole-document
  // MutationObserver; any non-idempotent write here (e.g. rewriting
  // document.title every tick) re-triggers the observer into a
  // self-perpetuating loop that starves the page's main thread. Proven live.
  const media = collectMedia();
  media.forEach((el) => observePlayer(el as HTMLVideoElement | HTMLAudioElement));
  const next = active() ? pick() : null;
  if (next !== current) {
    const previous = current;
    current = next;
    if (frame !== null) {
      cancelAnimationFrame(frame);
      frame = null;
    }
    button?.remove();
    button = null;
    if (previous) reportPlayer(previous, true);
  }
  if (current) reportPlayer(current);
  if (current && frame === null) {
    if (!positionButton()) current = null;
    else frame = requestAnimationFrame(loop);
  }
}

function mediaExtension(media: HTMLMediaElement, source: string): string {
  const type = media.getAttribute('type') || [...media.querySelectorAll('source')].find(item => item.src === source || item.getAttribute('src') === source)?.getAttribute('type') || '';
  const typeExtension: Record<string, string> = {
    'audio/mpeg': 'mp3',
    'audio/mp4': 'm4a',
    'audio/ogg': 'ogg',
    'audio/wav': 'wav',
    'audio/webm': 'webm',
    'video/mp4': 'mp4',
    'video/ogg': 'ogv',
    'video/webm': 'webm',
  };
  if (typeExtension[type.toLowerCase()]) return typeExtension[type.toLowerCase()];
  try {
    const extension = new URL(source).pathname.split('/').pop()?.split('.').pop()?.toLowerCase() || '';
    if (['m4a', 'mp3', 'ogg', 'ogv', 'wav', 'webm', 'mp4'].includes(extension)) return extension;
  } catch {
    // Use the media kind fallback below for non-URL sources.
  }
  return media instanceof HTMLAudioElement ? 'mp3' : 'mp4';
}

function captureName(media: HTMLMediaElement, source: string): string | undefined {
  return document.title ? `${document.title.slice(0, 80)}.${mediaExtension(media, source)}` : undefined;
}

async function capture(): Promise<void> {
  if (!current || captureInFlight) return;
  const el = current;
  const currentSrc = currentSrcFor(el);
  const playerKind = el instanceof HTMLAudioElement ? 'audio' : 'video';
  captureInFlight = true;
  if (button) {
    button.disabled = true;
    button.dataset.label = 'Download';
    button.title = '';
    button.setAttribute('aria-label', 'Download this media');
  }
  try {
    const pageEvidence = currentSrc ? await requestPageEvidence(currentSrc, playerKind) : undefined;
    const directSource = currentSrc.startsWith('http:') || currentSrc.startsWith('https:') ? currentSrc : '';
    const source = pageEvidence?.source || directSource;
    const response = (await chrome.runtime.sendMessage({
      type: 'media-capture',
      payload: {
        source: isHttp(source) ? source : '',
        currentSrc,
        mediaIdentity: pageEvidence?.sourceIdentity ?? mediaIdentityFor(el, currentSrc),
        ...(pageEvidence ? { pageEvidence } : {}),
        pageUrl: window.location.href,
        userAgent: navigator.userAgent,
        media: true,
        playerKind,
        playerKey: keyFor(el),
        name: captureName(el, source || currentSrc),
      },
    })) as { ok?: boolean; error?: string };
    if (!response?.ok) flashError(response?.error);
  } catch {
    flashError();
  } finally {
    captureInFlight = false;
    if (button) button.disabled = false;
  }
}

function flashError(error?: string): void {
  if (!button) return;
  button.dataset.label = 'Unavailable';
  button.title = error || 'Media unavailable';
  button.setAttribute('aria-label', `${button.title}. Retry download`);
}

function onPointerMove(event: PointerEvent | MouseEvent): void {
  lastPointerX = event.clientX;
  lastPointerY = event.clientY;
  if (pointerRAF !== null) return;
  pointerRAF = window.requestAnimationFrame(() => {
    pointerRAF = null;
    if (!active()) return;
    const next = pick();
    if (next !== current) {
      track();
    } else if (current && button?.isConnected) {
      positionButton();
    }
  });
}

document.addEventListener('click', rememberBrowserOwnedClick, true);
document.addEventListener('click', interceptDownloadClick, true);
window.addEventListener('pointermove', onPointerMove, { passive: true });
window.addEventListener('mouseleave', () => {
  lastPointerX = null;
  lastPointerY = null;
  track();
}, { passive: true });
document.addEventListener('mouseenter', track, true);
document.addEventListener('scroll', track, { capture: true, passive: true });
chrome.storage.onChanged.addListener((changes, areaName) => {
  if (areaName === 'local' && (changes['dm-policy'] || changes['dm-media-filters'])) void refreshPolicy();
});

void refreshPolicy().then(() => {
  window.setInterval(track, 500);
  if (window.top === window) window.setInterval(refreshPolicy, 1000);
  let queuedTrack: number | null = null;
  const scheduleTrack = () => {
    if (queuedTrack !== null) return;
    queuedTrack = window.setTimeout(() => {
      queuedTrack = null;
      track();
    }, 100);
  };
  mediaObserver = new MutationObserver((records) => {
    updateMediaCache(records);
    scheduleTrack();
  });
  mediaObserver.observe(document.documentElement, mediaMutationOptions);
  track();
});
